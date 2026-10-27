import tempfile
import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from backend.investigation.budget import InvestigationBudget, InvestigationDeadlineExceeded
from backend.investigation.agent import ModelFailure
from backend.investigation.evidence_models import WindowQuery
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_queries import WindowEvidenceTools
from backend.investigation.window_investigation import FrozenWindowSession, investigate_window_agent
from test_window_investigation import Scripted, finish, observed
from backend.investigation.window_cli import main


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class BudgetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SnapshotStore(Path(temp.name))
        tools = WindowEvidenceTools(self.store)
        with patch.object(tools, "_fetch_window", return_value={"data": [observed()]}):
            self.key = tools.query_window(WindowQuery(start_us=100, end_us=200)).data["snapshot_id"]
        self.clock = Clock()

    def run_agent(self, model):
        return investigate_window_agent(self.store, self.key, model,
                                        timeout_seconds=5, clock=self.clock)

    def test_validation_and_fixed_deadline(self):
        for value in (0, -1, True, "5", float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                InvestigationBudget(value)
        budget = InvestigationBudget(5, clock=self.clock)
        self.clock.now = 2
        self.assertEqual(budget.remaining_seconds(), 3)
        budget.check()
        self.clock.now = 5
        with self.assertRaises(InvestigationDeadlineExceeded):
            budget.check()
        self.clock.now = 8
        self.assertEqual(budget.remaining_seconds(), 0)

    def test_cli_rejects_invalid_or_unsupported_budget_before_model_setup(self):
        for strategy, value in (("agent", "nan"), ("agent", "0"),
                                ("fixed", "5"), ("hypotheses", "nan"), ("hypotheses", "0")):
            with patch("sys.argv", ["window", "investigate", self.key,
                                    "--strategy", strategy, "--timeout-seconds", value]), \
                    patch("backend.investigation.window_cli.ModelConfig.from_env") as config, \
                    redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertEqual(raised.exception.code, 2)
                config.assert_not_called()

    def test_snapshot_loading_counts_and_prevents_model_call(self):
        original = self.store.read
        def slow_read(key):
            self.clock.now = 5
            return original(key)
        model = Scripted()
        with patch.object(self.store, "read", side_effect=slow_read):
            result = self.run_agent(model)
        self.assertEqual(result.termination, "deadline_exceeded")
        self.assertEqual(result.usage_totals["model_calls"], 0)
        self.assertEqual(len(result.evidence), 1)

    def test_late_finish_and_late_error_keep_call_usage(self):
        for fails in (False, True):
            self.clock.now = 0
            model = Scripted()
            model.last_usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
            def late(context):
                self.clock.now = 6
                if fails:
                    raise ModelFailure("timeout")
                return finish(self.key, "insufficient_evidence", [])
            with patch.object(model, "decide", side_effect=late):
                result = self.run_agent(model)
            self.assertEqual(result.termination, "deadline_exceeded")
            self.assertEqual(result.usage_totals["model_calls"], 1)
            self.assertEqual(result.usage_totals["total_tokens"], 3)
            self.assertEqual(result.usage.calls[-1].error_code, "timeout" if fails else None)
            self.assertEqual(result.finding.references, [])

    def test_late_tool_action_does_not_execute(self):
        model = Scripted()
        def late(context):
            self.clock.now = 5
            return {"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32}
        with patch.object(model, "decide", side_effect=late):
            result = self.run_agent(model)
        self.assertEqual(result.termination, "deadline_exceeded")
        self.assertEqual(result.usage_totals["drilldown_calls"], 0)

    def test_completed_drilldown_is_preserved_without_next_model_call(self):
        original = FrozenWindowSession.drilldown
        def slow_drilldown(session, trace_id):
            evidence = original(session, trace_id)
            self.clock.now = 5
            return evidence
        model = Scripted({"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32})
        with patch.object(FrozenWindowSession, "drilldown", slow_drilldown):
            result = self.run_agent(model)
        self.assertEqual(result.termination, "deadline_exceeded")
        self.assertEqual(result.usage_totals["model_calls"], 1)
        self.assertEqual(result.usage_totals["drilldown_calls"], 1)
        self.assertEqual(result.evidence[-1].data["trace_id"], "a" * 32)

    def test_normal_finish_and_network_error_remain_distinct(self):
        model = Scripted({"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32}, finish(self.key))
        result = self.run_agent(model)
        self.assertEqual(result.termination, "completed")
        self.assertEqual(result.finding.code, "no_anomaly_observed")
        with patch.object(model, "decide", side_effect=ModelFailure("timeout")):
            result = self.run_agent(model)
        self.assertEqual(result.termination, "model_error")
        self.assertEqual(result.usage.calls[-1].error_code, "timeout")
