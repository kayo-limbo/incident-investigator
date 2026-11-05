"""Synthetic regressions for observed failure classes, not recovered model responses."""

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

import httpx

import test_bundle_investigation as fixtures
from backend.investigation.bundle_agent import (
    BundleChatModel, BundleReport, HYPOTHESIS_ACTION, investigate_bundle,
)
from backend.investigation.bundle_diagnostics import rejected_candidate
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore


class BundleDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SnapshotStore(Path(temp.name))
        self.key = self.store.freeze(fixtures.BundleTests.payload(self))

    def query(self):
        return dict(kind="tool", tool="trace_window", purpose="查询分段",
                    updates=[fixtures.hypothesis()], hypothesis_ids=["slow"])

    async def test_schema_paths_are_safe_and_invalid_values_are_not_saved(self):
        action = self.query()
        action["updates"][0]["status"] = "PRIVATE_VALUE"
        action["updates"][0]["PRIVATE_FIELD"] = "PRIVATE_VALUE"
        report = await investigate_bundle(self.store, self.key, fixtures.Scripted(action), "hypotheses")
        self.assertEqual(report.validation_error, "invalid_schema")
        self.assertEqual(report.diagnostic.category, "action_schema")
        self.assertEqual(report.diagnostic.turn, 1)
        paths = [x.path for x in report.schema_issues]
        self.assertIn(["tool", "updates", 0, "status"], paths)
        self.assertIn(["tool", "updates", 0, "<unknown>"], paths)
        self.assertNotIn("PRIVATE_", report.model_dump_json())
        self.assertEqual(report.rejected_candidate.status, "schema_invalid")
        self.assertIsNone(report.rejected_candidate.action)
        self.assertEqual(report.hypothesis_history, [])
        self.assertEqual(report.usage_totals["tool_calls"], 1)

    async def test_schema_issue_count_is_bounded(self):
        action = self.query()
        action.update({f"secret{i}": "value" for i in range(30)})
        report = await investigate_bundle(self.store, self.key, fixtures.Scripted(action), "hypotheses")
        self.assertEqual(len(report.schema_issues), 24)
        self.assertTrue(report.schema_issues_truncated)
        self.assertNotIn("secret", report.model_dump_json())

    async def test_invalid_reference_preserves_candidate_not_state(self):
        action = fixtures.finish(self.key, hypotheses=True)
        action["finding"]["references"][0]["snapshot_id"] = "0" * 64
        original = copy.deepcopy(action)
        report = await investigate_bundle(self.store, self.key, fixtures.Scripted(self.query(), action), "hypotheses")
        self.assertEqual(report.validation_error, "invalid_reference")
        self.assertEqual(report.diagnostic.category, "evidence_reference")
        self.assertEqual(report.diagnostic.turn, 2)
        self.assertEqual(report.rejected_candidate.action, original)
        self.assertEqual(report.rejected_finding.code, "inventory_latency")
        self.assertEqual(report.finding.code, "insufficient_evidence")
        self.assertEqual(len(report.hypothesis_history), 1)
        self.assertEqual(report.hypothesis_history[0].status, "open")
        action["finding"]["summary"] = "changed after return"
        self.assertEqual(report.rejected_candidate.action, original)
        self.assertEqual(report.usage_totals["total_tokens"], 40)
        self.assertEqual(report.usage_totals["tool_calls"], 2)

    async def test_unlinked_claim_is_retained_and_corrected_fixture_can_pass(self):
        action = fixtures.finish(self.key, hypotheses=True)
        action["finding"]["references"].append(dict(snapshot_id=self.key, source="trace_window",
                                                    field="requests.0.pre_send_ms"))
        report = await investigate_bundle(self.store, self.key, fixtures.Scripted(self.query(), action), "hypotheses")
        self.assertEqual(report.validation_error, "claim_reference_not_linked")
        self.assertEqual(report.diagnostic.category, "hypothesis_link")
        self.assertEqual(report.rejected_candidate.action, action)
        self.assertEqual(len(report.hypothesis_history), 1)
        self.assertEqual(len(report.query_tasks), 1)
        # Separate scripted investigation, not an automatic correction or model improvement.
        repaired = fixtures.finish(self.key, hypotheses=True)
        good = await investigate_bundle(self.store, self.key, fixtures.Scripted(self.query(), repaired), "hypotheses")
        self.assertEqual(good.termination, "completed")
        self.assertIsNone(good.diagnostic)
        self.assertIsNone(good.rejected_candidate)

    async def test_oversized_candidate_is_omitted_without_mutating_validation(self):
        action = self.query()
        action["hypothesis_ids"] = ["a" * 20000]
        validated = HYPOTHESIS_ACTION.validate_python(action)
        self.assertEqual(rejected_candidate(validated).status, "too_large")
        report = await investigate_bundle(self.store, self.key, fixtures.Scripted(action), "hypotheses")
        self.assertEqual(report.validation_error, "unknown_or_duplicate_hypothesis")
        self.assertEqual(report.rejected_candidate.status, "too_large")
        self.assertGreater(report.rejected_candidate.serialized_bytes, 16384)
        self.assertIsNone(report.rejected_candidate.action)
        self.assertIsNone(report.rejected_finding)
        self.assertEqual(report.hypothesis_history, [])

    async def test_repeat_records_candidate_but_not_attached_updates(self):
        action = self.query()
        ref = dict(snapshot_id=self.key, source="trace_window", field="requests.0.inventory_ms")
        action["updates"] = [fixtures.hypothesis(ref)]
        report = await investigate_bundle(self.store, self.key, fixtures.Scripted(self.query(), action), "hypotheses")
        self.assertEqual(report.termination, "repeated_query")
        self.assertEqual(report.rejected_candidate.action, action)
        self.assertIsNone(report.diagnostic)  # Intentional stopping rule, not a schema error.
        self.assertEqual(report.hypothesis_history[0].status, "open")
        self.assertEqual(len(report.hypothesis_history), 1)

    async def test_response_parse_failure_preserves_usage_without_fabricating_candidate(self):
        config = ModelConfig("http://localhost", "test", "test-key")
        def handler(request):
            return httpx.Response(200, json=dict(choices=[dict(finish_reason="stop",
                message=dict(content="not-json PRIVATE_VALUE"))],
                usage=dict(prompt_tokens=10, completion_tokens=2, total_tokens=12)))
        async with BundleChatModel(config, transport=httpx.MockTransport(handler)) as model:
            report = await investigate_bundle(self.store, self.key, model)
        self.assertEqual(report.termination, "model_error")
        self.assertEqual(report.diagnostic.code, "action_json_invalid")
        self.assertEqual(report.diagnostic.stage, "decide")
        self.assertEqual(report.usage_totals["total_tokens"], 12)
        self.assertIsNone(report.rejected_candidate)
        self.assertNotIn("PRIVATE_VALUE", report.model_dump_json())

    async def test_budget_preempts_capture_and_diagnostics_do_not_enter_context(self):
        model = fixtures.Scripted(self.query())
        report = await investigate_bundle(self.store, self.key, model, "hypotheses", max_total_tokens=20)
        self.assertEqual(report.termination, "token_budget_exhausted")
        self.assertIsNone(report.rejected_candidate)
        self.assertEqual(report.hypothesis_history, [])
        self.assertNotIn("schema_issues", model.contexts[0].model_dump())
        self.assertNotIn("rejected_candidate", model.contexts[0].model_dump())

    async def test_old_report_defaults_and_source_failure(self):
        result = await investigate_bundle(self.store, self.key,
            fixtures.Scripted(fixtures.finish(self.key, "insufficient_evidence")))
        old = result.model_dump(mode="json")
        for field in ("schema_issues", "schema_issues_truncated", "rejected_candidate"):
            old.pop(field)
        parsed = BundleReport.model_validate_json(json.dumps(old))
        self.assertEqual(parsed.schema_issues, [])
        self.assertIsNone(parsed.rejected_candidate)
        failure = await investigate_bundle(self.store, "0" * 64, fixtures.Scripted())
        self.assertEqual(failure.diagnostic.stage, "source")
        self.assertEqual(failure.usage_totals["model_calls"], 0)
