import asyncio
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from backend.investigation.agent import ModelFailure
from backend.investigation.async_investigation import (
    investigate_hypotheses_async, investigate_hypotheses_legacy_async, InvestigationCancelled,
)
from backend.investigation.hypothesis_graph import HypothesisGraphRun
from test_async_hypotheses import setup_snapshot
from test_async_investigation import AsyncScripted
from test_hypotheses import query, update
from test_window_investigation import finish
from evals.run_hypothesis_comparison import audit_history


def normalized(report):
    data = report.model_dump(mode="json")
    ids = {e["id"]: f"e{i}" for i, e in enumerate(data["evidence"])}

    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in ("strategy", "elapsed_ms", "observed_at")}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return ids.get(value, value) if isinstance(value, str) else value

    return clean(data)


class GraphTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store, self.key = setup_snapshot(directory)
        self.ref = dict(snapshot_id=self.key, trace_id="a" * 32, span_id="3", field="duration_ms")
        self.end = {**finish(self.key, "inventory_latency", [self.ref]),
                    "updates": [{k: v for k, v in update(version=1, status="supported", support=[self.ref]).items()
                                 if k != "expected_version"}], "hypothesis_ids": ["slow"]}

    async def test_graph_and_legacy_reports_match_for_success_and_failures(self):
        cases = [([query([update()]), self.end], {}),
                 ([{"kind": "unknown", "secret": "not-in-report"}], {}),
                 ([query([update()]), self.end | {"hypothesis_ids": ["unknown"]}], {}),
                 ([query([update()]), query([update()])], {}),
                 ([query([update()]), query([update()])], {"max_tool_calls": 2, "repeat_query_policy": "allow"}),
                 ([ModelFailure("timeout")], {}),
                 ([query([update()])], {"max_total_tokens": 10})]
        for actions, options in cases:
            with self.subTest(actions=actions, options=options):
                graph = await investigate_hypotheses_async(self.store, self.key, AsyncScripted(*copy.deepcopy(actions)), **options)
                legacy = await investigate_hypotheses_legacy_async(self.store, self.key, AsyncScripted(*copy.deepcopy(actions)), **options)
                self.assertEqual(normalized(graph), normalized(legacy))
                self.assertNotIn("not-in-report", graph.model_dump_json())
                self.assertTrue(audit_history(graph, self.store))

    async def test_distinct_nodes_exchange_json_data(self):
        seen = []
        patches = []
        for name in ("decide", "validate", "commit", "tool"):
            original = getattr(HypothesisGraphRun, name)

            async def wrapped(run, state, name=name, original=original):
                # 经JSON往返仍能运行；状态没有客户端/时钟/可变账本对象。
                state = json.loads(json.dumps(state))
                seen.append(name)
                return await original(run, state)

            patches.append(patch.object(HypothesisGraphRun, name, wrapped))
        for item in patches:
            self.enterContext(item)
        result = await investigate_hypotheses_async(self.store, self.key, AsyncScripted(query([update()]), self.end))
        self.assertEqual(result.termination, "completed")
        self.assertEqual(seen, ["decide", "validate", "commit", "tool", "decide", "validate", "commit"])

    async def test_cancel_between_validation_and_commit_rejects_candidate(self):
        entered = asyncio.Event()

        async def pause(run, state):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(HypothesisGraphRun, "commit", pause):
            task = asyncio.create_task(investigate_hypotheses_async(self.store, self.key, AsyncScripted(query([update()]))))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(InvestigationCancelled) as caught:
                await task
        self.assertEqual(caught.exception.report.hypothesis_history, [])
        self.assertEqual(caught.exception.report.usage_totals["model_calls"], 1)
        self.assertEqual(caught.exception.report.usage_totals["drilldown_calls"], 0)

    async def test_concurrent_runs_have_isolated_state(self):
        good, bad = await asyncio.gather(
            investigate_hypotheses_async(self.store, self.key, AsyncScripted(query([update()]), self.end)),
            investigate_hypotheses_async(self.store, self.key, AsyncScripted({"kind": "invalid"})))
        self.assertEqual(good.termination, "completed")
        self.assertEqual(bad.termination, "invalid_action")
        self.assertEqual(bad.hypothesis_history, [])
        self.assertEqual(len(bad.evidence), 1)

    async def test_node_recursion_limit_scales_with_business_budget(self):
        model = AsyncScripted(query([update()]), *[query() for _ in range(6)], self.end)
        report = await investigate_hypotheses_async(self.store, self.key, model,
            max_tool_calls=8, repeat_query_policy="allow")
        self.assertEqual(report.termination, "completed")
        self.assertEqual(report.usage_totals["model_calls"], 8)
        self.assertEqual(report.usage_totals["tool_calls"], 8)
