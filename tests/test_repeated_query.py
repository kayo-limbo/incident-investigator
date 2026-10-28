import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.investigation.async_investigation import investigate_hypotheses_async, investigate_window_async
from backend.investigation.window_investigation import FrozenWindowSession, RepeatedQueryDetected, investigate_window_agent
from backend.investigation.window_cli import main
from backend.investigation.evidence_models import WindowQuery
from backend.investigation.window_queries import WindowEvidenceTools
from evals.run_hypothesis_comparison import audit_history
from evals.run_window_comparison import score
from test_async_hypotheses import setup_snapshot
from test_hypotheses import query, update
from test_token_budget import MeteredModel
from test_window_investigation import Scripted, finish, observed


class RepeatedQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store, self.key = setup_snapshot(temp.name)
        self.tool = {"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32}
        self.variants = ((investigate_window_async, self.tool,
                          finish(self.key, "insufficient_evidence", [])),
                         (investigate_hypotheses_async, query([update()]),
                          {**finish(self.key, "insufficient_evidence", []), "updates": [], "hypothesis_ids": []}))

    async def test_stop_retains_original_evidence_and_actual_model_cost(self):
        for runner, tool, done in self.variants:
            repeated = tool if runner is investigate_window_async else query()
            model = MeteredModel((tool, {"total_tokens": 3}), (repeated, {"total_tokens": 4}))
            result = await runner(self.store, self.key, model, max_total_tokens=100)
            self.assertEqual(result.termination, "repeated_query")
            self.assertEqual(result.repeat_query_policy, "stop")
            self.assertEqual(result.repeated_query.snapshot_id, self.key)
            self.assertEqual(result.repeated_query.trace_id, "a" * 32)
            self.assertEqual(result.repeated_query.previous_evidence_id, result.evidence[1].id)
            self.assertEqual(result.repeated_query.model_call, 2)
            self.assertEqual(result.usage_totals["model_calls"], 2)
            self.assertEqual(result.usage_totals["drilldown_calls"], 1)
            self.assertEqual(result.usage_totals["tool_calls"], 2)
            self.assertEqual(result.token_budget.total_tokens, 7)
            self.assertFalse(score(result, "insufficient_evidence", self.key, self.store))
            if hasattr(result, "hypotheses"):
                self.assertTrue(audit_history(result, self.store))

    async def test_allow_policy_can_continue_to_finish(self):
        for runner, tool, done in self.variants:
            repeated = tool if runner is investigate_window_async else query()
            model = MeteredModel((tool, None), (repeated, None), (done, None))
            result = await runner(self.store, self.key, model, repeat_query_policy="allow")
            self.assertEqual(result.termination, "completed")
            self.assertEqual(result.repeat_query_policy, "allow")
            self.assertIsNone(result.repeated_query)
            self.assertEqual(result.usage_totals["drilldown_calls"], 2)
            self.assertNotEqual(result.evidence[1].id, result.evidence[2].id)
            self.assertEqual(result.evidence[1].data, result.evidence[2].data)
            if hasattr(result, "hypotheses"):
                self.assertTrue(audit_history(result, self.store))

    async def test_rejected_repeat_does_not_commit_valid_hypothesis_update(self):
        ref = dict(snapshot_id=self.key, trace_id="a" * 32, span_id="3", field="duration_ms")
        candidate = query([update(version=1, status="supported", support=[ref])])
        model = MeteredModel((query([update()]), None), (candidate, None))
        result = await investigate_hypotheses_async(self.store, self.key, model)
        self.assertEqual(result.termination, "repeated_query")
        self.assertEqual(result.hypotheses[0].status, "open")
        self.assertEqual(result.hypotheses[0].version, 1)
        self.assertEqual(len(result.query_tasks), 1)
        self.assertEqual(len(result.hypothesis_history), 1)

    async def test_different_traces_are_not_repeats(self):
        model = MeteredModel((self.tool, None), ({**self.tool, "trace_id": "b" * 32}, None),
                             (finish(self.key, "insufficient_evidence", []), None))
        result = await investigate_window_async(self.store, self.key, model)
        self.assertEqual(result.termination, "completed")
        self.assertEqual(result.usage_totals["drilldown_calls"], 2)

    async def test_invalid_action_is_not_misclassified_as_repeat(self):
        for repeated in ({**self.tool, "extra": "forbidden"}, {**self.tool, "trace_id": "f" * 32}):
            model = MeteredModel((self.tool, None), (repeated, None))
            result = await investigate_window_async(self.store, self.key, model)
            self.assertEqual(result.termination, "invalid_action")
            self.assertIsNone(result.repeated_query)

    async def test_invalid_hypothesis_batch_wins_before_repeat_check(self):
        candidate = query([update(version=1, status="supported")])
        model = MeteredModel((query([update()]), None), (candidate, None))
        result = await investigate_hypotheses_async(self.store, self.key, model)
        self.assertEqual(result.termination, "invalid_action")
        self.assertEqual(result.validation_error, "support_required")
        self.assertIsNone(result.repeated_query)

    async def test_token_budget_wins_before_repeat_check(self):
        model = MeteredModel((self.tool, {"total_tokens": 3}), (self.tool, {"total_tokens": 4}))
        result = await investigate_window_async(self.store, self.key, model, max_total_tokens=7)
        self.assertEqual(result.termination, "token_budget_exhausted")
        self.assertIsNone(result.repeated_query)

    async def test_invalid_policy_rejected_before_reading_snapshot(self):
        with patch.object(self.store, "read") as read:
            with self.assertRaises(ValueError):
                await investigate_window_async(self.store, self.key, MeteredModel(), repeat_query_policy="cache")
            read.assert_not_called()

    async def test_context_exposes_policy_without_mutating_session(self):
        class Mutates:
            calls = 0
            async def decide(inner, context):
                self.assertEqual(context.repeat_query_policy, "stop")
                context.repeat_query_policy = "allow"
                context.evidence.clear()
                inner.calls += 1
                return self.tool
        result = await investigate_window_async(self.store, self.key, Mutates())
        self.assertEqual(result.termination, "repeated_query")
        self.assertEqual(result.repeat_query_policy, "stop")

    def test_session_scope_does_not_deduplicate_across_investigations_or_snapshots(self):
        tools = WindowEvidenceTools(self.store)
        with patch.object(tools, "_fetch_window", return_value={"data": [observed()]}):
            other = tools.query_window(WindowQuery(start_us=100, end_us=200)).data["snapshot_id"]
        for key in (self.key, self.key, other):
            session = FrozenWindowSession(self.store, key, repeat_query_policy="stop")
            session.drilldown("a" * 32)
            with self.assertRaises(RepeatedQueryDetected):
                session.drilldown("a" * 32)
            self.assertEqual(session.usage.totals()["drilldown_calls"], 1)

    def test_sync_entry_preserves_allow_behavior(self):
        result = investigate_window_agent(self.store, self.key, Scripted(
            self.tool, self.tool, finish(self.key, "insufficient_evidence", [])))
        self.assertEqual(result.termination, "completed")
        self.assertEqual(result.repeat_query_policy, "allow")
        self.assertEqual(result.usage_totals["drilldown_calls"], 2)


class RepeatedQueryCliTests(unittest.TestCase):
    def test_fixed_rejects_explicit_repeat_policy(self):
        with patch("sys.argv", ["window", "investigate", "a" * 64, "--repeat-query-policy", "allow"]), \
                redirect_stderr(io.StringIO()), patch("backend.investigation.window_cli.ModelConfig.from_env") as config:
            with self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, 2)
            config.assert_not_called()

    def test_cli_default_and_allow_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            store, key = setup_snapshot(directory)
            for strategy in ("agent", "hypotheses"):
                for policy in (None, "allow"):
                    tool = ({"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32}
                            if strategy == "agent" else query([update()]))
                    repeated = tool if strategy == "agent" else query()
                    done = finish(key, "insufficient_evidence", [])
                    if strategy == "hypotheses":
                        done.update(updates=[], hypothesis_ids=[])
                    class Adapter(MeteredModel):
                        closed = False
                        async def __aenter__(self):
                            return self
                        async def __aexit__(self, *args):
                            self.closed = True
                    model = Adapter((tool, None), (repeated, None), (done, None))
                    output = Path(directory) / "result.private.json"
                    argv = ["window", "--store", directory, "investigate", key, "--strategy", strategy,
                            "--output", str(output)]
                    if policy:
                        argv += ["--repeat-query-policy", policy]
                    adapter = "AsyncWindowChatModel" if strategy == "agent" else "AsyncHypothesisChatModel"
                    with patch("sys.argv", argv), patch("backend.investigation.window_cli.ModelConfig.from_env"), \
                            patch("backend.investigation.window_cli." + adapter, return_value=model), \
                            redirect_stdout(io.StringIO()):
                        self.assertEqual(main(), 0 if policy else 1)
                    result = json.loads(output.read_text(encoding="utf-8"))
                    self.assertEqual(result["termination"], "completed" if policy else "repeated_query")
                    self.assertEqual(result["repeat_query_policy"], policy or "stop")
                    self.assertTrue(model.closed)
