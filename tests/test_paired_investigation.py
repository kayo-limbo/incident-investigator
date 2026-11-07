import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.investigation.paired_agent import investigate_paired, PairedDomain, InvestigationSession
from backend.investigation.paired_baseline import investigate_paired_fixed, statistics
from backend.investigation.paired_cli import main
from backend.investigation.paired_evidence import mask_sources
from backend.investigation.snapshot_store import SnapshotStore
from test_paired_evidence import payload
from test_bundle_investigation import Scripted, hypothesis


def fixture(kind="capacity"):
    value = payload()
    value.update(schema_version=4, projection_version="paired-bundle-v2")
    value["alert"]["request_count"] = 40
    for index, name in enumerate(("reference", "incident")):
        window = value[name]
        row = window["trace"]["requests"][0]
        window["trace"]["requests"] = [dict(row, trace_id=f"{index * 40 + i + 1:032x}") for i in range(20)]
        r = window["runtime"]
        r["before"].update(requests_started=index * 40, requests_finished=index * 40)
        count = 40 if kind in ("arrival", "competing") and index else 20
        r["after"].update(requests_started=index * 40 + count, requests_finished=index * 40 + count)
        r["send_end"] = dict(r["after"], observed_us=r["after"]["observed_us"] - 1,
                             monotonic_us=r["after"]["monotonic_us"] - 1)
        if index and kind in ("capacity", "competing"):
            for reading in r.values():
                reading["max_connections"] = 1
                reading["process_id"] = "e" * 32
        if index:
            for request in window["trace"]["requests"]:
                if kind in ("capacity", "arrival", "competing"):
                    request.update(pre_send_ms=300., orders_ms=400.)
                if kind == "downstream":
                    request.update(inventory_ms=350., orders_ms=400.)
    return value


class PairedInvestigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SnapshotStore(Path(self.temp.name) / "snapshots")

    def test_fixed_changes_and_ablations(self):
        for kind, code, stage in (("stable", "no_anomaly_observed", "no_anomaly"),
                ("capacity", "capacity_change", "pre_send"), ("arrival", "arrival_change", "pre_send"),
                ("downstream", "downstream_change", "downstream"), ("competing", "competing_changes", "pre_send")):
            with self.subTest(kind=kind):
                report = investigate_paired_fixed(self.store, self.store.freeze(fixture(kind)))
                self.assertEqual((report.finding.code, report.finding.stage), (code, stage))
                self.assertEqual(len(report.evidence), 5)
        for source, phase in (("trace", "unknown"), ("runtime", "pre_send")):
            report = investigate_paired_fixed(self.store, self.store.freeze(mask_sources(fixture(), **{source: False})))
            self.assertEqual((report.finding.code, report.finding.stage), ("insufficient_evidence", phase))

    def test_p95_is_independent_nearest_rank_not_max(self):
        trace = fixture()["reference"]["trace"]
        trace["requests"][0]["inventory_ms"] = 1000.
        trace["requests"][1]["orders_ms"] = 2000.
        result = statistics(trace)
        self.assertEqual(result["inventory_ms"], 60.)
        self.assertEqual(result["orders_ms"], 100.)
        self.assertNotEqual(result["inventory_ms_index"], 0)

    def test_partial_and_abnormal_reference_refuse_change(self):
        for partial in (False, True):
            value = fixture()
            if partial:
                value["reference"]["trace"]["quality"] = "partial"
            else:
                for row in value["reference"]["trace"]["requests"]:
                    row["inventory_ms"] = 400.
            report = investigate_paired_fixed(self.store, self.store.freeze(value))
            self.assertEqual(report.finding.code, "insufficient_evidence")

    def test_source_error_and_budget(self):
        self.assertEqual(investigate_paired_fixed(self.store, "f" * 64).termination, "source_error")
        self.assertEqual(investigate_paired_fixed(self.store, self.store.freeze(payload())).termination, "source_error")
        report = investigate_paired_fixed(self.store, self.store.freeze(fixture()), 2)
        self.assertEqual(report.termination, "budget_exhausted")
        self.assertEqual(len(report.evidence), 2)

    async def test_both_graph_strategies_complete_same_queries(self):
        key = self.store.freeze(fixture())
        expected = investigate_paired_fixed(self.store, key).finding.model_dump()
        for strategy in ("agent", "hypotheses"):
            actions = [dict(kind="tool", tool=s, window=w) for w in ("reference", "incident") for s in ("trace", "runtime")]
            finish = dict(kind="finish", finding=expected)
            if strategy == "hypotheses":
                for action in actions:
                    action.update(purpose="比较窗口证据", hypothesis_ids=["slow"], updates=[hypothesis()] if action is actions[0] else [])
                h = hypothesis()
                h.update(status="supported", supporting_refs=expected["references"], pending_checks=[])
                finish.update(hypothesis_ids=["slow"], updates=[h])
            model = Scripted(*actions, finish)
            report = await investigate_paired(self.store, key, model, strategy)
            self.assertEqual(report.termination, "completed", report.model_dump_json())
            self.assertEqual(report.finding.code, "capacity_change")
            self.assertEqual([q["window"] for q in report.query_tasks], ["reference", "reference", "incident", "incident"])
            self.assertEqual(len(model.contexts[0].evidence), 1)
            if strategy == "hypotheses":
                self.assertEqual([h.version for h in report.hypothesis_history], [1, 2])

    async def test_repeat_and_unseen_window_rejected(self):
        key = self.store.freeze(fixture())
        query = dict(kind="tool", tool="trace", window="reference")
        report = await investigate_paired(self.store, key, Scripted(query, query))
        self.assertEqual(report.termination, "repeated_query")
        finding = investigate_paired_fixed(self.store, key).finding.model_dump()
        report = await investigate_paired(self.store, key, Scripted(query, dict(kind="finish", finding=finding)))
        self.assertEqual(report.termination, "invalid_finding")

    async def test_invalid_window_and_token_stop(self):
        key = self.store.freeze(fixture())
        report = await investigate_paired(self.store, key, Scripted(dict(kind="tool", tool="trace", window="other")))
        self.assertEqual(report.termination, "invalid_action")
        self.assertTrue(report.schema_issues)
        report = await investigate_paired(self.store, key, Scripted(dict(kind="tool", tool="trace", window="reference")), max_total_tokens=1)
        self.assertEqual(len(report.evidence), 1)
        self.assertNotEqual(report.termination, "completed")

    def test_presence_guard_is_not_semantic_validator(self):
        key = self.store.freeze(fixture())
        finding = investigate_paired_fixed(self.store, key).finding
        domain = PairedDomain(InvestigationSession(self.store, key, 6), "paired_fixed_v1")
        with self.assertRaises(ValueError):
            domain.validate_finding(finding)
        for window in ("reference", "incident"):
            for source in ("trace", "runtime"):
                domain.session.query(window, source)
        domain.validate_finding(finding.model_copy(update={"code": "arrival_change"}))

    def test_cli_defaults_offline_and_never_overwrites(self):
        key = self.store.freeze(fixture())
        output = Path(self.temp.name) / "report.json"
        args = [key, "--store", str(Path(self.temp.name) / "snapshots"), "--output", str(output)]
        with patch("backend.investigation.paired_cli.ModelConfig.from_env", side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
            with self.assertRaises(FileExistsError):
                main(args)


if __name__ == "__main__":
    unittest.main()
