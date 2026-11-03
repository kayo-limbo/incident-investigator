import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from backend.investigation.model import ModelConfig
from evals import run_correction_comparison as runner
from evals.run_holdout import write
from test_holdout import summary
from test_async_hypotheses import setup_snapshot
from test_corrections import RecordingModel
from test_window_investigation import finish
from backend.investigation.async_investigation import investigate_window_async
from evals.summarize_correction_comparison import summarize


class CorrectionComparisonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "experiment"
        self.config = ModelConfig("https://example.com", "test", "secret")
        self.enterContext(patch.object(runner, "fingerprint", return_value={"code": "hash"}))
        runner.freeze(self.root, self.config)
        write(self.root / "collection.private.json", dict(completed=True, recovery_verified=True,
            protocol={"code": "hash"}, plan=list(runner.PLAN), cases=[dict(name=s["name"],
            expected=s["expected"], collected=True, snapshot_id=str(i), business_summary=summary(s["expected"]))
            for i, s in enumerate(runner.PLAN)]))
        self.report = Mock()
        self.report.evidence = [Mock(data={"frozen": True})]
        self.report.model_dump.return_value = {"evidence": [{"data": {"frozen": True}}]}
        self.report.termination = "invalid_action"
        self.report.diagnostic = self.report.correction = None
        self.report.usage_totals = {"model_calls": 1, "total_tokens": None}
        self.enterContext(patch.object(runner, "investigate_window_fixed", return_value=self.report))
        self.enterContext(patch.object(runner, "score", return_value=False))
        self.enterContext(patch.object(runner, "audit_history", return_value=True))

    def test_balanced_pairs_and_secret_free_plan(self):
        runs = runner.schedule()
        self.assertEqual(len(runs), 40)
        for case in range(5):
            for strategy in ("agent", "hypotheses"):
                a = [r["max_corrections"] for r in runs if r["case"] == case and r["strategy"] == strategy]
                self.assertEqual(a[:2], list(reversed(a[2:])))
        self.assertNotIn("secret", (self.root / "plan.private.json").read_text())
        with self.assertRaises(FileExistsError):
            runner.freeze(self.root, self.config)

    async def test_failed_reports_are_retained_without_retry(self):
        with patch.object(runner, "run_one", new=AsyncMock(return_value=self.report)) as call:
            result = await runner.evaluate(self.root, Mock(), self.config)
            self.assertEqual(call.await_count, 40)
            self.assertTrue(result["completed"])
            self.assertTrue(all(not r["passed"] for r in result["runs"]))
            self.assertEqual(len(list((self.root / "reports").glob("*.json"))), 40)
            with self.assertRaises(FileExistsError):
                await runner.evaluate(self.root, Mock(), self.config)
            self.assertEqual(call.await_count, 40)

    async def test_unknown_interrupted_call_is_not_retried(self):
        with patch.object(runner, "run_one", new=AsyncMock(side_effect=RuntimeError("private"))) as call:
            with self.assertRaises(RuntimeError):
                await runner.evaluate(self.root, Mock(), self.config)
            self.assertEqual(call.await_count, 1)
        result = json.loads((self.root / "result.private.json").read_text())
        self.assertFalse(result["completed"])
        self.assertEqual(result["runs"][0]["status"], "started")
        self.assertEqual(result["error"], "RuntimeError")
        self.assertNotIn("private", result["error"])

    async def test_protocol_or_collection_change_blocks_model(self):
        path = self.root / "collection.private.json"
        original = json.loads(path.read_text())
        for change in (lambda m: m.update(recovery_verified=False),
                       lambda m: m["cases"].pop(),
                       lambda m: m["cases"][0].update(expected="pool_contention"),
                       lambda m: m["cases"][0]["business_summary"].update(successes=0)):
            changed = copy.deepcopy(original)
            change(changed)
            write(path, changed)
            with patch.object(runner, "run_one", new=AsyncMock()) as call:
                with self.assertRaises(ValueError):
                    await runner.evaluate(self.root, Mock(), self.config)
                call.assert_not_called()
        write(path, original)
        with patch.object(runner, "fingerprint", return_value={"code": "changed"}), \
                patch.object(runner, "run_one", new=AsyncMock()) as call:
            with self.assertRaisesRegex(ValueError, "frozen_protocol_changed"):
                await runner.evaluate(self.root, Mock(), self.config)
            call.assert_not_called()

    async def test_changed_code_after_response_preserves_report_and_stops(self):
        async def changed(*args):
            runner.fingerprint.return_value = {"code": "changed"}
            return self.report
        with patch.object(runner, "run_one", new=AsyncMock(side_effect=changed)) as call:
            with self.assertRaisesRegex(ValueError, "frozen_protocol_changed"):
                await runner.evaluate(self.root, Mock(), self.config)
            self.assertEqual(call.await_count, 1)
        result = json.loads((self.root / "result.private.json").read_text())
        self.assertFalse(result["completed"])
        self.assertEqual(result["runs"][0]["status"], "finished")


class CorrectionSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rescore_repaired_report_and_detect_usage_tampering(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store, key = setup_snapshot(str(root))
        end = finish(key, "insufficient_evidence", [])
        tool = dict(kind="tool", tool="get_snapshot_trace", trace_id="a" * 32)
        plain = await investigate_window_async(store, key, RecordingModel(tool, end))
        repaired = await investigate_window_async(store, key, RecordingModel({"kind": "bad"}, tool, end), max_corrections=1)
        plan = [dict(case=0, repeat=1, strategy="agent", max_corrections=n) for n in (0, 1)]
        write(root / "plan.private.json", {"schedule": plan})
        write(root / "collection.private.json", dict(completed=True, recovery_verified=True,
            cases=[dict(snapshot_id=key, expected="insufficient_evidence")]))
        result = dict(completed=True, baselines=[dict(report=plain.model_dump(mode="json"), passed=True)], runs=[])
        for item, report in zip(plan, (plain, repaired)):
            file = f"{item['max_corrections']}.private.json"
            write(root / "reports" / file, report.model_dump(mode="json"))
            result["runs"].append(dict(item, status="finished", report_file=file, passed=True,
                same_input=True, history_ok=True, usage=report.usage_totals))
        write(root / "result.private.json", result)
        totals = summarize(root, store)
        on = totals["groups"]["agent_correction_1"]
        self.assertEqual(on["corrected_correct"], 1)
        self.assertEqual(on["known_total_tokens"], 9)
        self.assertEqual(sum(on["correction_failures"].values()), 1)
        self.assertEqual(totals["pairs"]["agent"]["both_correct"], 1)
        result["runs"][1]["usage"]["total_tokens"] = 0
        write(root / "result.private.json", result)
        with self.assertRaisesRegex(ValueError, "report_score_or_usage_mismatch"):
            summarize(root, store)
