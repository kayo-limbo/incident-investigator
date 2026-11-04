"""11月3日失败形态的最小合成回归；不包含私有快照或声称复现未知原始响应。"""

import copy
import tempfile
import unittest
from pathlib import Path

import httpx

from backend.investigation.async_investigation import investigate_window_async, investigate_hypotheses_async
from backend.investigation.async_model import AsyncWindowChatModel, AsyncHypothesisChatModel
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore
from evals.run_hypothesis_comparison import audit_history
from test_async_investigation import envelope, freeze
from test_corrections import RecordingModel
from test_hypotheses import query, update
from test_window_investigation import finish


class FailureRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = SnapshotStore(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.key = freeze(self.store)  # 健康合成窗口，与重复查询的真实失败形态对应。
        self.tool = dict(kind="tool", tool="get_snapshot_trace", trace_id="a" * 32)
        self.wait = dict(snapshot_id=self.key, trace_id="a" * 32, span_id="2", field="tags.client.pre_send_ms")
        self.duration = dict(snapshot_id=self.key, trace_id="a" * 32, span_id="3", field="duration_ms")

    def candidate(self):
        updates = [update("primary", status="supported", support=[self.wait]),
                   update("alternative", status="refuted", counter=[self.duration])]
        return {**finish(self.key, "pre_send_delay", [self.wait, self.duration]),
                "updates": [{k: v for k, v in h.items() if k != "expected_version"} for h in updates],
                "hypothesis_ids": ["primary"]}

    def initial(self):
        return query([update("primary"), update("alternative")], ids=["primary", "alternative"])

    async def test_invalid_json_stops_without_repair_and_keeps_provider_usage(self):
        # 原实验未保存content；以下是合成语法边界，不能据此认定真实失败根因。
        for adapter, run in ((AsyncWindowChatModel, investigate_window_async),
                             (AsyncHypothesisChatModel, investigate_hypotheses_async)):
            for content in ('{"kind":', '```json\n{"kind":"finish"}\n```', '{"kind":"finish"} trailing'):
                with self.subTest(adapter=adapter.__name__, content=content):
                    requests = []
                    def handle(request):
                        requests.append(request)
                        response = envelope({})
                        response["choices"][0]["message"]["content"] = content
                        return httpx.Response(200, json=response)
                    config = ModelConfig("https://example.test", "test", "test")
                    async with adapter(config, transport=httpx.MockTransport(handle)) as model:
                        report = await run(self.store, self.key, model, max_corrections=1)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(report.termination, "model_error")
                    self.assertEqual(report.diagnostic.code, "action_json_invalid")
                    self.assertEqual(report.usage_totals["total_tokens"], 3)
                    self.assertEqual(report.usage_totals["drilldown_calls"], 0)
                    self.assertEqual(report.correction.used, 0)

    async def test_low_load_repeat_does_not_execute_twice_or_consume_correction(self):
        for enabled in (0, 1):
            model = RecordingModel(self.tool, self.tool, finish(self.key, "insufficient_evidence", []))
            report = await investigate_window_async(self.store, self.key, model, max_corrections=enabled)
            self.assertEqual(report.termination, "repeated_query")
            self.assertEqual(model.calls, 2)
            self.assertEqual(report.usage_totals["drilldown_calls"], 1)
            self.assertEqual(report.usage_totals["total_tokens"], 6)
            self.assertEqual(len(report.evidence), 2)
            if enabled:
                self.assertEqual(report.correction.used, 0)

    async def test_unselected_counter_reference_is_rejected_without_state_commit(self):
        model = RecordingModel(self.initial(), self.candidate())
        report = await investigate_hypotheses_async(self.store, self.key, model)
        self.assertEqual(report.diagnostic.code, "claim_reference_not_linked")
        self.assertEqual([h.status for h in report.hypotheses], ["open", "open"])
        self.assertEqual(len(report.hypothesis_history), 2)
        self.assertEqual(report.usage_totals["drilldown_calls"], 1)

    async def test_repeating_unlinked_candidate_uses_only_one_opportunity(self):
        candidate = self.candidate()
        original = copy.deepcopy(candidate)
        model = RecordingModel(self.initial(), candidate, candidate)
        report = await investigate_hypotheses_async(self.store, self.key, model, max_corrections=1)
        self.assertEqual(report.termination, "invalid_finding")
        self.assertEqual(report.correction.used, 1)
        self.assertEqual([f.diagnostic.code for f in report.correction.failures], ["claim_reference_not_linked"] * 2)
        self.assertEqual([h.status for h in report.hypotheses], ["open", "open"])
        self.assertEqual(candidate, original)
        feedback = model.contexts[2]["correction_feedback"]
        self.assertEqual(feedback["candidate"], candidate)
        for name in ("hypothesis_ids", "updates", "supporting_refs", "counter_refs", "snapshot_id", "trace_id", "span_id", "field"):
            self.assertIn(name, feedback["instruction"])
        self.assertEqual(report.usage_totals["total_tokens"], 9)

    async def test_selecting_refuted_alternative_allows_claim_without_extra_query(self):
        candidate = self.candidate()
        corrected = copy.deepcopy(candidate)
        corrected["hypothesis_ids"].append("alternative")
        model = RecordingModel(self.initial(), candidate, corrected)
        report = await investigate_hypotheses_async(self.store, self.key, model, max_corrections=1)
        self.assertEqual(report.termination, "completed")
        self.assertEqual(report.correction.used, 1)
        self.assertEqual(report.claim_hypothesis_versions, {"primary": 2, "alternative": 2})
        self.assertTrue(audit_history(report, self.store))
        self.assertEqual(report.usage_totals["drilldown_calls"], 1)
        self.assertEqual([h["status"] for h in model.contexts[2]["hypotheses"]], ["open", "open"])

    async def test_same_span_different_field_is_still_unlinked(self):
        candidate = self.candidate()
        candidate["finding"]["references"] = [self.wait, self.wait | {"field": "duration_ms"}]
        model = RecordingModel(self.initial(), candidate)
        report = await investigate_hypotheses_async(self.store, self.key, model)
        self.assertEqual(report.diagnostic.code, "claim_reference_not_linked")
