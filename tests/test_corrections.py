import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError
from backend.investigation.agent import ModelFailure
from backend.investigation.async_investigation import (
    investigate_window_async, investigate_hypotheses_async, InvestigationCancelled,
)
from backend.investigation.async_model import AsyncWindowChatModel
from backend.investigation.budget import InvestigationBudget
from backend.investigation.model import ModelConfig
from backend.investigation.window_graph import WindowGraphRun
from backend.investigation.window_cli import main as cli
from backend.tasks.models import TaskRequest
from test_async_hypotheses import setup_snapshot
from test_async_investigation import AsyncScripted, envelope
from test_hypotheses import query, update
from test_window_investigation import finish


class RecordingModel(AsyncScripted):
    last_usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}

    def __init__(self, *actions):
        super().__init__(*actions)
        self.contexts = []

    async def decide(self, context):
        self.contexts.append(context.model_dump(mode="json"))
        return await super().decide(context)


class CorrectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store, self.key = setup_snapshot(self.enterContext(tempfile.TemporaryDirectory()))
        self.tool = dict(kind="tool", tool="get_snapshot_trace", trace_id="a" * 32)
        self.ref = dict(snapshot_id=self.key, trace_id="a" * 32, span_id="3", field="duration_ms")
        self.end = finish(self.key, "inventory_latency", [self.ref])
        self.abstain = finish(self.key, "insufficient_evidence", [])

    async def test_schema_repair_uses_existing_budget_and_safe_feedback(self):
        model = RecordingModel(self.tool | {"private-field": "private-value"}, self.tool, self.end)
        report = await investigate_window_async(self.store, self.key, model, max_corrections=1)
        self.assertEqual(report.termination, "completed")
        self.assertEqual(report.correction.used, 1)
        self.assertEqual(len(report.correction.failures), 1)
        self.assertEqual(report.usage_totals["model_calls"], 3)
        self.assertEqual(report.usage_totals["tool_calls"], 2)
        self.assertEqual(report.usage_totals["total_tokens"], 9)
        self.assertNotIn("correction_feedback", model.contexts[0])
        self.assertNotIn("correction_feedback", model.contexts[2])
        feedback = model.contexts[1]["correction_feedback"]
        self.assertEqual(feedback["diagnostic"]["turn"], 1)
        self.assertIn("<unknown>", feedback["issue_paths"][0])
        self.assertIsNone(feedback["candidate"])
        self.assertNotIn("private", json.dumps(feedback))
        self.assertEqual(len(model.contexts[1]["evidence"]), 1)

    async def test_only_one_opportunity_per_investigation(self):
        model = RecordingModel({"kind": "bad"}, {"kind": "bad"}, self.abstain)
        report = await investigate_window_async(self.store, self.key, model, max_corrections=1)
        self.assertEqual(report.termination, "invalid_action")
        self.assertEqual(model.calls, 2)
        self.assertEqual(report.correction.used, 1)
        self.assertEqual([f.diagnostic.turn for f in report.correction.failures], [1, 2])
        self.assertEqual(report.usage_totals["drilldown_calls"], 0)

    async def test_disabled_and_last_model_round_do_not_extend_budget(self):
        for options in ({}, {"max_corrections": 1, "max_tool_calls": 1}):
            model = RecordingModel({"kind": "bad"}, self.abstain)
            report = await investigate_window_async(self.store, self.key, model, **options)
            self.assertEqual(model.calls, 1)
            self.assertEqual(report.termination, "invalid_action")
            self.assertTrue(report.correction is None or report.correction.used == 0)

    async def test_correction_token_limit_stops_before_accepting_repaired_action(self):
        model = RecordingModel({"kind": "bad"}, self.tool)
        report = await investigate_window_async(self.store, self.key, model,
            max_corrections=1, max_total_tokens=6)
        self.assertEqual(report.termination, "token_budget_exhausted")
        self.assertEqual(report.usage_totals["total_tokens"], 6)
        self.assertEqual(report.usage_totals["drilldown_calls"], 0)
        self.assertEqual(len(report.correction.failures), 1)

    async def test_network_parse_state_and_scope_failures_do_not_retry(self):
        cases = [(investigate_window_async, ModelFailure("timeout"), "model_error"),
                 (investigate_window_async, ModelFailure("invalid_response", detail="action_json_invalid"), "model_error"),
                 (investigate_window_async, self.tool | {"trace_id": "f" * 32}, "invalid_action"),
                 (investigate_hypotheses_async, query([update(status="supported")]), "invalid_action")]
        for runner, action, termination in cases:
            model = RecordingModel(action)
            report = await runner(self.store, self.key, model, max_corrections=1)
            self.assertEqual(model.calls, 1)
            self.assertEqual(report.termination, termination)
            self.assertEqual(report.correction.used, 0)

    async def test_reference_repair_can_only_use_previously_observed_fields(self):
        bad = finish(self.key, "inventory_latency", [self.ref | {"field": "tags.missing"}])
        model = RecordingModel(self.tool, bad, self.end)
        report = await investigate_window_async(self.store, self.key, model, max_corrections=1)
        self.assertEqual(report.termination, "completed")
        self.assertEqual(report.correction.failures[0].diagnostic.code, "invalid_reference")
        self.assertEqual(report.usage_totals["drilldown_calls"], 1)

    async def test_hypothesis_repair_does_not_commit_rejected_candidate(self):
        candidate = [{k: v for k, v in update(version=1, status="supported", support=[self.ref]).items()
                      if k != "expected_version"}]
        bad = {**self.end, "updates": candidate, "hypothesis_ids": []}
        good = {**bad, "hypothesis_ids": ["slow"]}
        model = RecordingModel(query([update()]), bad, good)
        report = await investigate_hypotheses_async(self.store, self.key, model, max_corrections=1)
        self.assertEqual(report.termination, "completed")
        self.assertEqual([h.version for h in report.hypothesis_history], [1, 2])
        self.assertEqual(model.contexts[2]["hypotheses"][0]["status"], "open")
        self.assertEqual(report.correction.failures[0].diagnostic.category, "hypothesis_link")

    async def test_cancellation_and_deadline_keep_failure_history(self):
        model = RecordingModel({"kind": "bad"}, "wait")
        task = asyncio.create_task(investigate_window_async(self.store, self.key, model, max_corrections=1))
        await asyncio.wait_for(model.entered.wait(), 3)
        task.cancel()
        with self.assertRaises(InvestigationCancelled) as caught:
            await task
        self.assertEqual(caught.exception.report.correction.used, 1)
        self.assertEqual(len(caught.exception.report.correction.failures), 1)
        now = [0]
        original = WindowGraphRun.validation_output
        def expire(run, report, state):
            result = original(run, report, state)
            now[0] = 20
            return result
        with patch.object(WindowGraphRun, "validation_output", expire), \
                patch("backend.investigation.async_investigation.InvestigationBudget",
                      return_value=InvestigationBudget(10, clock=lambda: now[0])):
            model = RecordingModel({"kind": "bad"}, self.abstain)
            report = await investigate_window_async(self.store, self.key, model, max_corrections=1)
        self.assertEqual(report.termination, "deadline_exceeded")
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(report.correction.failures), 1)

    async def test_real_adapter_payload_includes_correction_feedback(self):
        requests = []
        actions = iter([{"kind": "bad"}, self.abstain])
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=envelope(next(actions)))
        config = ModelConfig(base_url="https://example.test", model="test", api_key="test")
        async with AsyncWindowChatModel(config, transport=httpx.MockTransport(handle)) as model:
            report = await investigate_window_async(self.store, self.key, model, max_corrections=1)
        self.assertEqual(report.termination, "completed")
        self.assertIn("correction_feedback", json.loads(requests[1]["messages"][1]["content"]))

    async def test_invalid_limits_rejected_before_io(self):
        for limit in (True, -1, 2, 0.5, "1"):
            with self.assertRaises(ValueError):
                await investigate_window_async(None, self.key, None, max_corrections=limit)
            with self.assertRaises(ValidationError):
                TaskRequest(snapshot_id=self.key, strategy="agent", max_corrections=limit)
        with self.assertRaises(ValidationError):
            TaskRequest(snapshot_id=self.key, max_corrections=1)


class CorrectionCliTests(unittest.TestCase):
    def test_cli_option_is_forwarded_and_fixed_is_rejected(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        with tempfile.TemporaryDirectory() as directory:
            store, key = setup_snapshot(directory)
            model = RecordingModel({"kind": "bad"}, finish(key, "insufficient_evidence", []))
            class Adapter:
                async def __aenter__(self): return model
                async def __aexit__(self, *args): pass
            args = ["window", "--store", directory, "investigate", key, "--strategy", "agent", "--max-corrections", "1"]
            with patch("sys.argv", args), patch("backend.investigation.window_cli.ModelConfig.from_env"), \
                    patch("backend.investigation.window_cli.AsyncWindowChatModel", return_value=Adapter()), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli(), 0)
            self.assertEqual(json.loads(output.getvalue())["correction"]["used"], 1)
            args[args.index("agent")] = "fixed"
            with patch("sys.argv", args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli()
