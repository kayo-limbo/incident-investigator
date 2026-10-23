import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evals.run_holdout import PLAN, check_observations, collect, evaluate


def summary(expected):
    slow = expected in ("inventory_latency", "inventory_latency_with_pool_contention")
    pool = expected in ("pool_contention", "inventory_latency_with_pool_contention")
    return dict(requests=18, successes=6 if pool else 18, pool_timeouts=12 if pool else 0,
                inventory_p95_ms=350 if slow else 60,
                pre_send_success_p95_ms=300 if expected == "pre_send_delay" else 3,
                orders_p95_ms=90 if expected == "no_anomaly_observed" else 400)


class HoldoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "manifest.private.json"
        self.manifest = dict(completed=True, recovery_verified=True, protocol={"code": "hash"}, plan=list(PLAN),
            cases=[dict(name=s["name"], expected=s["expected"], collected=True, snapshot_id=str(i),
                        business_summary=summary(s["expected"])) for i, s in enumerate(PLAN)])
        self.store = Mock()

    def save(self):
        self.source.write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_business_checks_do_not_treat_injection_as_observed_truth(self):
        for spec in PLAN:
            check_observations(spec, summary(spec["expected"]))
            broken = summary(spec["expected"])
            broken["successes"] = 0
            with self.assertRaisesRegex(ValueError, "scenario_not_observed"):
                check_observations(spec, broken)
        with self.assertRaises(ValueError):
            check_observations(PLAN[1], summary("no_anomaly_observed"))

    def test_incomplete_changed_protocol_or_missing_case_never_calls_model(self):
        for mutation in (lambda m: m.update(completed=False), lambda m: m.update(recovery_verified=False),
                         lambda m: m.update(protocol={"code": "other"}), lambda m: m["cases"].pop(),
                         lambda m: m["cases"][0].update(expected="inventory_latency")):
            original = copy.deepcopy(self.manifest)
            mutation(self.manifest)
            self.save()
            with patch("evals.run_holdout.protocol_fingerprint", return_value={"code": "hash"}), \
                    patch("evals.run_holdout.replay") as replay:
                with self.assertRaises(ValueError):
                    evaluate(self.source, self.root / "runs", self.store, Mock())
                replay.assert_not_called()
            self.manifest = original

    def test_failed_trial_is_preserved_and_second_trial_still_runs(self):
        self.save()
        original = self.source.read_bytes()
        directory = self.root / "runs"
        with patch("evals.run_holdout.protocol_fingerprint", return_value={"code": "hash"}), \
                patch("evals.run_holdout.replay", side_effect=[1, 0]) as replay:
            self.assertEqual(evaluate(self.source, directory, self.store, Mock()), 1)
            self.assertEqual(replay.call_count, 2)
            self.assertEqual(replay.call_args_list[0].args[0], self.source)
        record = json.loads((directory / "run.private.json").read_text())
        self.assertTrue(record["completed"])
        self.assertEqual([t["exit_code"] for t in record["trials"]], [1, 0])
        self.assertEqual(self.source.read_bytes(), original)
        with patch("evals.run_holdout.protocol_fingerprint", return_value={"code": "hash"}), \
                patch("evals.run_holdout.replay") as replay:
            with self.assertRaises(FileExistsError):
                evaluate(self.source, directory, self.store, Mock())
            replay.assert_not_called()

    def test_protocol_change_during_trial_stops_further_calls(self):
        self.save()
        directory = self.root / "runs"
        with patch("evals.run_holdout.protocol_fingerprint", side_effect=[{"code": "hash"}, {"code": "hash"}, {"code": "changed"}]), \
                patch("evals.run_holdout.replay", return_value=0) as replay:
            with self.assertRaisesRegex(ValueError, "protocol_changed_during_evaluation"):
                evaluate(self.source, directory, self.store, Mock())
            self.assertEqual(replay.call_count, 1)
        self.assertFalse(json.loads((directory / "run.private.json").read_text())["completed"])

    def test_collection_failure_still_restores_and_records_failure(self):
        response = Mock(status_code=200)
        response.json.return_value = {"sku": "demo", "can_order": True}
        with patch("evals.run_holdout.protocol_fingerprint", return_value={"code": "hash"}), \
                patch("evals.run_holdout.compose", side_effect=[RuntimeError("startup"), "restored"]) as compose, \
                patch("evals.run_holdout.httpx.Client") as client:
            client.return_value.__enter__.return_value.get.return_value = response
            with self.assertRaises(RuntimeError):
                collect(self.source, self.store)
        saved = json.loads(self.source.read_text(encoding="utf-8"))
        self.assertFalse(saved["completed"])
        self.assertTrue(saved["recovery_verified"])
        self.assertEqual(saved["error"], "RuntimeError")
        self.assertEqual(compose.call_args.kwargs["delay"], 0)
        with patch("evals.run_holdout.compose") as compose:
            with self.assertRaises(ValueError):
                collect(self.source, self.store)
            compose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
