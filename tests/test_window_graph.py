import asyncio
import copy
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from backend.investigation.agent import ModelFailure
from backend.investigation.async_investigation import (
    investigate_window_async, investigate_window_legacy_async, InvestigationCancelled,
)
from backend.investigation.window_graph import WindowGraphRun
from backend.investigation.budget import InvestigationBudget
from backend.investigation.window_cli import run_async_agent
from backend.tasks.models import TaskRequest
from backend.tasks.worker import execute_with_lease
from test_async_hypotheses import setup_snapshot
from test_async_investigation import AsyncScripted
from test_hypothesis_graph import normalized
from test_window_investigation import finish


class WindowGraphTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store, self.key = setup_snapshot(directory)
        self.tool = dict(kind="tool", tool="get_snapshot_trace", trace_id="a" * 32)
        self.ref = dict(snapshot_id=self.key, trace_id="a" * 32, span_id="3", field="duration_ms")
        self.end = finish(self.key, "inventory_latency", [self.ref])

    async def test_graph_matches_legacy_domain_results(self):
        cases = [([self.tool, self.end], {}, self.key, None),
                 ([finish(self.key, "insufficient_evidence", [])], {}, self.key, None),
                 ([{"kind": "unknown", "secret": "not-in-report"}], {}, self.key, None),
                 ([self.end], {}, self.key, None),
                 ([self.tool, self.tool], {}, self.key, None),
                 ([self.tool | {"trace_id": "b" * 32}], {}, self.key, None),
                 ([self.tool], {"max_tool_calls": 1}, self.key, None),
                 ([ModelFailure("timeout")], {}, self.key, None),
                 ([self.tool], {"max_total_tokens": 10}, self.key, None),
                 ([self.tool], {"max_total_tokens": 3}, self.key,
                  {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}),
                 ([], {}, "b" * 64, None),
                 ([], {"timeout_seconds": 1e-12}, self.key, None)]
        for actions, options, key, usage in cases:
            with self.subTest(actions=actions, options=options, key=key):
                reports = []
                for runner in (investigate_window_async, investigate_window_legacy_async):
                    model = AsyncScripted(*copy.deepcopy(actions))
                    model.last_usage = usage
                    reports.append(await runner(self.store, key, model, **options))
                self.assertEqual(normalized(reports[0]), normalized(reports[1]))
                self.assertNotIn("not-in-report", reports[0].model_dump_json())

    async def test_distinct_nodes_exchange_json_without_hypothesis_state(self):
        seen = []
        for name in ("decide", "validate", "tool"):
            original = getattr(WindowGraphRun, name)

            async def wrapped(run, state, name=name, original=original):
                state = json.loads(json.dumps(state))
                self.assertNotIn("history", state)
                self.assertNotIn("queries", state)
                seen.append(name)
                return await original(run, state)

            self.enterContext(patch.object(WindowGraphRun, name, wrapped))
        report = await investigate_window_async(self.store, self.key, AsyncScripted(self.tool, self.end))
        self.assertEqual(report.strategy, "window_react_langgraph_v1")
        self.assertEqual(report.finding.references[0].field, "duration_ms")
        self.assertEqual(seen, ["decide", "validate", "tool", "decide", "validate"])

    async def test_cancel_between_validation_and_tool_preserves_usage_without_execution(self):
        entered = asyncio.Event()

        async def pause(run, state):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(WindowGraphRun, "tool", pause):
            task = asyncio.create_task(investigate_window_async(self.store, self.key, AsyncScripted(self.tool)))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                task.cancel()
                with self.assertRaises(InvestigationCancelled) as caught:
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(caught.exception.report.usage_totals["model_calls"], 1)
        self.assertEqual(caught.exception.report.usage_totals["drilldown_calls"], 0)
        self.assertEqual(len(caught.exception.report.evidence), 1)

    async def test_execution_rechecks_deadline_after_validation(self):
        now = [0]
        original = WindowGraphRun.validate

        async def late(run, state):
            result = await original(run, state)
            now[0] = 11
            return result

        with patch.object(WindowGraphRun, "validate", late), \
                patch("backend.investigation.async_investigation.InvestigationBudget",
                      return_value=InvestigationBudget(10, clock=lambda: now[0])):
            report = await investigate_window_async(self.store, self.key, AsyncScripted(self.tool))
        self.assertEqual(report.termination, "deadline_exceeded")
        self.assertEqual(report.usage_totals["drilldown_calls"], 0)

    async def test_long_graph_and_concurrent_isolation(self):
        good, bad = await asyncio.gather(
            investigate_window_async(self.store, self.key, AsyncScripted(*[self.tool] * 9, self.end),
                                     max_tool_calls=10, repeat_query_policy="allow"),
            investigate_window_async(self.store, self.key, AsyncScripted({"kind": "invalid"})))
        self.assertEqual(good.termination, "completed")
        self.assertEqual(good.usage_totals["model_calls"], 10)
        self.assertEqual(good.usage_totals["tool_calls"], 10)
        self.assertEqual(bad.termination, "invalid_action")
        self.assertEqual(len(bad.evidence), 1)

    async def test_cli_runs_same_graph_without_checkpointer(self):
        model = AsyncMock()
        model.__aenter__.return_value = AsyncScripted(self.tool, self.end)
        original = WindowGraphRun.run
        seen = []

        async def run(graph):
            seen.append(graph.checkpointer)
            return await original(graph)

        with patch("backend.investigation.window_cli.AsyncWindowChatModel", return_value=model), \
                patch.object(WindowGraphRun, "run", run):
            report = await run_async_agent(self.store, self.key, None, 6, 120)
        self.assertEqual(report.termination, "completed")
        self.assertEqual(seen, [None])
        model.__aexit__.assert_awaited_once()

    async def test_worker_selects_persistent_execution_for_both_graph_strategies(self):
        for strategy in ("agent", "hypotheses"):
            task = SimpleNamespace(request=TaskRequest(snapshot_id=self.key, strategy=strategy))
            with patch("backend.tasks.checkpoints.execute_persistent", new_callable=AsyncMock) as persistent, \
                    patch("backend.tasks.worker.execute", new_callable=AsyncMock) as legacy:
                await execute_with_lease(Mock(), task, self.store, 30)
                persistent.assert_awaited_once()
                legacy.assert_not_awaited()
