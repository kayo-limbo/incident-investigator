import asyncio
import io
import json
import signal
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.investigation.async_investigation import InvestigationCancelled, investigate_hypotheses_async
from backend.investigation.budget import InvestigationBudget
from backend.investigation.evidence_models import WindowQuery
from backend.investigation.hypotheses import HypothesisLedger
from backend.investigation.hypothesis_agent import HypothesisReport, investigate_hypotheses
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_cli import main
from backend.investigation.window_investigation import FrozenWindowSession
from backend.investigation.window_queries import WindowEvidenceTools
from evals.run_hypothesis_comparison import audit_history
from test_async_investigation import AsyncScripted
from test_hypotheses import query, update
from test_window_investigation import Scripted, finish, observed


def setup_snapshot(directory):
    store = SnapshotStore(Path(directory))
    tools = WindowEvidenceTools(store)
    with patch.object(tools, "_fetch_window", return_value={"data": [observed(slow=True), observed("b", failed=True)]}):
        key = tools.query_window(WindowQuery(start_us=100, end_us=200)).data["snapshot_id"]
    return store, key


class AsyncHypothesisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store, self.key = setup_snapshot(temp.name)
        self.slow = dict(snapshot_id=self.key, trace_id="a" * 32, span_id="3", field="duration_ms")
        self.pool = dict(snapshot_id=self.key, trace_id="b" * 32, span_id="2", field="tags.error.type")

    def end(self, updates, ids=None, refs=None, code="inventory_latency"):
        return {**finish(self.key, code, [self.slow] if refs is None else refs),
                "updates": [{k: v for k, v in item.items() if k != "expected_version"} for item in updates],
                "hypothesis_ids": ["slow"] if ids is None else ids}

    async def test_joint_success_preserves_sync_protocol_and_independent_audit(self):
        actions = [query([update(), update("pool")], ids=["slow", "pool"]),
                   query([update(version=1, status="supported", support=[self.slow])], "b", ["pool"]),
                   self.end([update("pool", 1, "supported", [self.pool])], ["slow", "pool"],
                            [self.slow, self.pool], "inventory_latency_with_pool_contention")]
        sync = investigate_hypotheses(self.store, self.key, Scripted(*actions))
        result = await investigate_hypotheses_async(self.store, self.key, AsyncScripted(*actions))
        self.assertEqual(result.termination, "completed")
        self.assertEqual(result.strategy, "window_hypotheses_async_v1")
        self.assertEqual(result.finding, sync.finding)
        self.assertEqual(result.hypothesis_history, sync.hypothesis_history)
        self.assertEqual(result.claim_hypothesis_versions, {"slow": 2, "pool": 2})
        self.assertEqual([q.model_dump(exclude={"evidence_id"}) for q in result.query_tasks],
                         [q.model_dump(exclude={"evidence_id"}) for q in sync.query_tasks])
        self.assertEqual(result.usage_totals["model_calls"], 3)
        self.assertTrue(audit_history(result, self.store))

    async def test_cancel_preserves_full_last_committed_state(self):
        model = AsyncScripted(query([update()]), "wait")
        task = asyncio.create_task(investigate_hypotheses_async(self.store, self.key, model))
        await asyncio.wait_for(model.entered.wait(), 2)
        task.cancel()
        with self.assertRaises(InvestigationCancelled) as caught:
            await task
        result = caught.exception.report
        self.assertIsInstance(result, HypothesisReport)
        self.assertTrue(task.cancelled())
        self.assertTrue(model.cleaned)
        self.assertEqual(result.termination, "cancelled")
        self.assertEqual([(h.id, h.version, h.status) for h in result.hypotheses], [("slow", 1, "open")])
        self.assertEqual(result.query_tasks[0].hypothesis_versions, {"slow": 1})
        self.assertEqual(result.query_tasks[0].status, "completed")
        self.assertEqual(result.claim_hypothesis_versions, {})
        self.assertEqual(result.usage.calls[-1].status, "cancelled")
        self.assertTrue(audit_history(result, self.store))

    async def test_deadline_preserves_state_and_unknown_usage(self):
        model = AsyncScripted(query([update()]), "wait")
        result = await asyncio.wait_for(investigate_hypotheses_async(
            self.store, self.key, model, timeout_seconds=0.05), 2)
        self.assertEqual(result.termination, "deadline_exceeded")
        self.assertEqual(result.hypotheses[0].version, 1)
        self.assertEqual(result.usage_totals["model_calls"], 2)
        self.assertIsNone(result.usage_totals["total_tokens"])
        self.assertTrue(audit_history(result, self.store))

    async def test_expiry_during_validation_does_not_commit_candidate(self):
        now = [0]
        budget = InvestigationBudget(10, clock=lambda: now[0])
        original = HypothesisLedger.propose
        def expires(ledger, updates, session, turn):
            candidate = original(ledger, updates, session, turn)
            if turn == 2:
                now[0] = 10
            return candidate
        model = AsyncScripted(query([update()]), self.end([update(version=1, status="supported", support=[self.slow])]))
        with patch("backend.investigation.async_investigation.InvestigationBudget", return_value=budget), \
                patch.object(HypothesisLedger, "propose", expires):
            result = await investigate_hypotheses_async(self.store, self.key, model)
        self.assertEqual(result.termination, "deadline_exceeded")
        self.assertEqual(len(result.hypothesis_history), 1)
        self.assertEqual(result.hypotheses[0].status, "open")
        self.assertEqual(result.claim_hypothesis_versions, {})

    async def test_late_model_response_does_not_create_hypotheses(self):
        now = [0]
        budget = InvestigationBudget(10, clock=lambda: now[0])
        class LateModel:
            async def decide(inner, context):
                now[0] = 10
                return query([update()])
        with patch("backend.investigation.async_investigation.InvestigationBudget", return_value=budget):
            result = await investigate_hypotheses_async(self.store, self.key, LateModel())
        self.assertEqual(result.termination, "deadline_exceeded")
        self.assertEqual(result.hypothesis_history, [])
        self.assertEqual(result.query_tasks, [])
        self.assertEqual(result.usage_totals["drilldown_calls"], 0)

    async def test_invalid_claim_and_batch_update_keep_previous_versions(self):
        for action in (
            self.end([update(version=1, status="supported", support=[self.slow])], ids=["unknown"]),
            self.end([update(version=1, status="supported", support=[self.slow]),
                      update("pool", status="supported", support=[self.pool])]),
        ):
            model = AsyncScripted(query([update()]), action)
            result = await investigate_hypotheses_async(self.store, self.key, model)
            self.assertEqual(result.termination, "invalid_finding")
            self.assertEqual([(h.id, h.version) for h in result.hypotheses], [("slow", 1)])
            self.assertIsNotNone(result.rejected_action)

    async def test_schema_failure_preserves_diagnostics_without_raw_values(self):
        action = query([update()])
        action["purpose"] = {"private": "never_copy_this"}
        result = await investigate_hypotheses_async(self.store, self.key, AsyncScripted(action))
        self.assertEqual(result.termination, "invalid_action")
        self.assertIn("tool.purpose:string_type", result.validation_issues)
        self.assertNotIn("never_copy_this", result.model_dump_json())
        self.assertEqual(result.hypothesis_history, [])

    async def test_tool_budget_rejects_updates_but_allows_final_finish(self):
        for action, expected in ((query([update("pool")]), "budget_exhausted"),
                                 (self.end([], ids=[], refs=[], code="insufficient_evidence"), "completed")):
            result = await investigate_hypotheses_async(
                self.store, self.key, AsyncScripted(query([update()]), action), max_tool_calls=2)
            self.assertEqual(result.termination, expected)
            self.assertEqual([h.id for h in result.hypotheses], ["slow"])

    async def test_source_failure_returns_hypothesis_report_without_model_calls(self):
        model = AsyncScripted()
        result = await investigate_hypotheses_async(self.store, "f" * 64, model)
        self.assertIsInstance(result, HypothesisReport)
        self.assertEqual(result.termination, "source_error")
        self.assertEqual(result.hypothesis_history, [])
        self.assertEqual(model.calls, 0)

    async def test_completed_local_drilldown_remains_recorded_after_expiry(self):
        now = [0]
        budget = InvestigationBudget(10, clock=lambda: now[0])
        original = FrozenWindowSession.drilldown
        def slow(session, trace_id):
            evidence = original(session, trace_id)
            now[0] = 10
            return evidence
        with patch("backend.investigation.async_investigation.InvestigationBudget", return_value=budget), \
                patch.object(FrozenWindowSession, "drilldown", slow):
            result = await investigate_hypotheses_async(self.store, self.key, AsyncScripted(query([update()])))
        self.assertEqual(result.termination, "deadline_exceeded")
        self.assertEqual(result.query_tasks[0].status, "completed")
        self.assertEqual(result.query_tasks[0].cost_tool_calls, 1)
        self.assertTrue(audit_history(result, self.store))


class HypothesisCliTests(unittest.TestCase):
    def test_sigint_saves_hypothesis_history_and_returns_130(self):
        with tempfile.TemporaryDirectory() as directory:
            store, key = setup_snapshot(directory)
            output = Path(directory) / "cancelled.private.json"
            class Interrupted:
                calls = 0
                closed = False
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    self.closed = True
                async def decide(self, context):
                    self.calls += 1
                    if self.calls == 1:
                        return query([update()])
                    signal.raise_signal(signal.SIGINT)
                    await asyncio.sleep(0)
            model = Interrupted()
            with patch("sys.argv", ["window", "--store", directory, "investigate", key,
                                    "--strategy", "hypotheses", "--timeout-seconds", "60", "--output", str(output)]), \
                    patch("backend.investigation.window_cli.ModelConfig.from_env"), \
                    patch("backend.investigation.window_cli.AsyncHypothesisChatModel", return_value=model), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 130)
            report = HypothesisReport.model_validate_json(output.read_text(encoding="utf-8"))
            self.assertTrue(model.closed)
            self.assertEqual(report.termination, "cancelled")
            self.assertEqual(report.hypotheses[0].version, 1)
            self.assertTrue(audit_history(report, store))
