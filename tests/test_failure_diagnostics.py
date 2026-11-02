import json
import tempfile
import unittest
from unittest.mock import patch

import httpx

from backend.investigation.agent import ModelFailure
from backend.investigation.async_investigation import investigate_window_async, investigate_hypotheses_async
from backend.investigation.async_model import AsyncWindowChatModel, AsyncHypothesisChatModel
from backend.investigation.diagnostics import diagnostic
from backend.investigation.model import ModelConfig
from backend.investigation.window_investigation import WindowReport
from test_async_hypotheses import setup_snapshot
from test_async_investigation import AsyncScripted, envelope
from test_hypotheses import query, update
from test_window_investigation import finish
from evals.run_window_comparison import score


class FailureDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store, self.key = setup_snapshot(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = ModelConfig(base_url="https://example.test", model="test", api_key="test-secret")
        self.tool = dict(kind="tool", tool="get_snapshot_trace", trace_id="a" * 32)
        self.ref = dict(snapshot_id=self.key, trace_id="a" * 32, span_id="3", field="duration_ms")

    async def test_response_failures_have_precise_causes_and_keep_usage(self):
        def body(content):
            value = envelope({})
            value["choices"][0]["message"]["content"] = content
            return json.dumps(value).encode()
        cases = [(b"secret-not-json", "response_json_invalid", None),
                 (b'{"choices":[]}', "response_shape_invalid", None),
                 (body(None), "action_content_invalid", 3),
                 (body("private-server-message"), "action_json_invalid", 3),
                 (body("[]"), "action_not_object", 3),
                 (json.dumps(envelope({}, "content_filter")).encode(), "response_finish_invalid", 3)]
        for adapter, runner in ((AsyncWindowChatModel, investigate_window_async),
                                (AsyncHypothesisChatModel, investigate_hypotheses_async)):
            for payload, expected, tokens in cases:
                with self.subTest(adapter=adapter, expected=expected):
                    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
                    async with adapter(self.config, transport=transport) as model:
                        report = await runner(self.store, self.key, model)
                    self.assertEqual(report.termination, "model_error")
                    self.assertEqual(report.diagnostic.category, "model_response")
                    self.assertEqual(report.diagnostic.stage, "decide")
                    self.assertEqual(report.diagnostic.code, expected)
                    self.assertEqual(report.diagnostic.turn, 1)
                    self.assertEqual(report.usage.calls[-1].error_code, "invalid_response")
                    self.assertEqual(report.usage_totals["total_tokens"], tokens)
                    self.assertEqual(report.usage_totals["drilldown_calls"], 0)
                    for secret in ("private-server-message", "test-secret", "secret-not-json"):
                        self.assertNotIn(secret, report.model_dump_json())

    async def test_request_failure_and_unclassified_adapter_error(self):
        for failure, category, code in ((ModelFailure("timeout"), "model_request", "timeout"),
                                        (RuntimeError("secret"), "internal", "unclassified_error")):
            report = await investigate_window_async(self.store, self.key, AsyncScripted(failure))
            self.assertEqual((report.diagnostic.category, report.diagnostic.code), (category, code))
            self.assertNotIn("secret", report.model_dump_json())
        self.assertIsNone(ModelFailure("invalid_response", detail="secret").detail)
        self.assertEqual(diagnostic("secret", 1).code, "unclassified_error")

    async def test_schema_failure_is_sanitized_and_never_executes(self):
        report = await investigate_window_async(self.store, self.key,
            AsyncScripted(self.tool | {"secret-field": "secret-value"}))
        self.assertEqual(report.diagnostic.category, "action_schema")
        self.assertEqual(report.diagnostic.issue_types, ["extra_forbidden"])
        self.assertEqual(report.usage_totals["drilldown_calls"], 0)
        self.assertNotIn("secret", report.model_dump_json())

    async def test_reference_and_tool_scope_failures_are_distinct(self):
        report = await investigate_window_async(self.store, self.key,
            AsyncScripted(finish(self.key, "inventory_latency", [self.ref])))
        self.assertEqual(report.diagnostic.category, "evidence_reference")
        report = await investigate_window_async(self.store, self.key,
            AsyncScripted(self.tool | {"trace_id": "f" * 32}))
        self.assertEqual(report.diagnostic.category, "tool_arguments")
        self.assertEqual(report.diagnostic.code, "trace_outside_snapshot")
        with patch("backend.investigation.window_investigation.FrozenWindowSession.drilldown",
                   side_effect=ValueError("trace_outside_snapshot")):
            report = await investigate_window_async(self.store, self.key, AsyncScripted(self.tool))
        self.assertEqual(report.diagnostic.stage, "tool")
        self.assertEqual(report.usage_totals["drilldown_calls"], 0)

    async def test_hypothesis_failures_preserve_last_accepted_state(self):
        linked = {**finish(self.key, "inventory_latency", [self.ref]), "updates": [], "hypothesis_ids": []}
        cases = [(query([update(status="supported")]), "hypothesis_state", "support_required"),
                 (query([update(support=[self.ref | {"span_id": "missing"}])]),
                  "evidence_reference", "invalid_reference"),
                 (linked, "hypothesis_link", "claim_hypothesis_required"),
                 ({"kind": "unknown"}, "action_schema", "invalid_schema")]
        for action, category, code in cases:
            with self.subTest(code=code):
                report = await investigate_hypotheses_async(self.store, self.key,
                    AsyncScripted(query([update()]), action))
                self.assertEqual(report.diagnostic.category, category)
                self.assertEqual(report.diagnostic.code, code)
                self.assertEqual(report.diagnostic.turn, 2)
                self.assertEqual(report.usage_totals["drilldown_calls"], 1)
                self.assertEqual(len(report.hypothesis_history), 1)
                self.assertEqual(report.hypotheses[0].status, "open")

    async def test_valid_but_wrong_diagnosis_is_only_rejected_by_independent_evaluator(self):
        report = await investigate_window_async(self.store, self.key,
            AsyncScripted(self.tool, finish(self.key, "no_anomaly_observed", [self.ref])))
        self.assertEqual(report.termination, "completed")
        self.assertIsNone(report.diagnostic)
        self.assertFalse(score(report, "inventory_latency", self.key, self.store))
        data = report.model_dump(mode="json")
        data.pop("diagnostic")
        for call in data["usage"]["calls"]:
            call.pop("failure_detail")
        self.assertIsNone(WindowReport.model_validate_json(json.dumps(data)).diagnostic)

    async def test_source_failure_is_before_first_model_call(self):
        report = await investigate_window_async(self.store, "f" * 64, AsyncScripted())
        self.assertEqual(report.diagnostic.stage, "source")
        self.assertEqual(report.diagnostic.turn, 0)
