import copy
import tempfile
import unittest
from pathlib import Path

from backend.investigation.paired_evidence import (
    PairedBundle, PairedReference, PairedSession, TimedRuntime, mask_sources,
)
from backend.investigation.snapshot_store import SnapshotStore


def reading(at, count=0, process="f"):
    return dict(process_id=process * 32, started_us=1, observed_us=at, monotonic_us=at + 100,
                max_connections=2, pool_timeout_ms=1000, requests_started=count,
                requests_finished=count, in_flight=0, peak_in_flight=1)


def payload():
    def window(start, letter, count):
        return dict(start_us=start + 1, end_us=start + 1_000_000,
            runtime=dict(before=reading(start, count), after=reading(start + 1_000_001, count + 10)),
            trace=dict(start_us=start + 1, end_us=start + 1_000_000, quality="observed", coverage="sampled",
                requests=[dict(trace_id=letter * 32, status=200, orders_ms=100., inventory_ms=60.,
                    pre_send_ms=30., error_type=None, max_connections=2, pool_timeout_ms=1000,
                    required_observations_present=True)]))
    return PairedBundle(alert=dict(request_count=10, client_p95_ms=110., failed_requests=0),
        reference=window(100, "a", 0), incident=window(2_000_100, "b", 10)).model_dump(mode="json")


class PairedEvidenceTests(unittest.TestCase):
    def test_samples_are_not_population_and_projection_rejects_truth(self):
        value = payload()
        self.assertEqual(value["alert"]["request_count"], 10)
        self.assertEqual(len(value["incident"]["trace"]["requests"]), 1)
        for target in (value, value["reference"], value["reference"]["runtime"]["before"]):
            target["truth"] = "capacity"
            with self.assertRaises(ValueError):
                PairedBundle.model_validate(value)
            del target["truth"]

    def test_clock_jump_drain_and_counter_reset_rejected(self):
        for key, value in (("monotonic_us", 1), ("observed_us", 2_000_000),
                           ("process_id", "e" * 32), ("requests_finished", 0), ("max_connections", 1)):
            raw = payload()["reference"]["runtime"]
            raw["after"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                TimedRuntime.model_validate(raw)
        raw = payload()["reference"]["runtime"]
        raw["after"].update(in_flight=1, requests_started=11)
        with self.assertRaisesRegex(ValueError, "window_not_drained"):
            TimedRuntime.model_validate(raw)

    def test_between_window_restart_is_allowed_without_counter_subtraction(self):
        value = payload()
        for key, count in (("before", 0), ("after", 10)):
            value["incident"]["runtime"][key].update(process_id="e" * 32, requests_started=count, requests_finished=count)
        bundle = PairedBundle.model_validate(value)
        self.assertAlmostEqual(bundle.reference.runtime.request_rate(), bundle.incident.runtime.request_rate())
        for key in ("before", "after"):
            value["incident"]["runtime"][key]["process_id"] = "f" * 32
        with self.assertRaises(ValueError):
            PairedBundle.model_validate(value)

    def test_time_overlap_duplicate_and_missing_source_consistency(self):
        for change in (lambda v: v["incident"].update(start_us=1),
                       lambda v: v["reference"]["trace"].update(quality="unavailable"),
                       lambda v: v["incident"]["trace"]["requests"][0].update(trace_id="a" * 32),
                       lambda v: v["reference"]["trace"].update(end_us=999)):
            value = payload()
            change(value)
            with self.assertRaises(ValueError):
                PairedBundle.model_validate(value)
        masked = mask_sources(payload(), runtime=False, trace=False)
        self.assertIsNone(masked["reference"]["runtime"])
        self.assertEqual(masked["incident"]["trace"]["requests"], [])
        self.assertNotIn("derived_from", masked)

    def test_offline_session_window_scope_copy_budget_and_repeats(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory))
            key = store.freeze(payload())
            session = PairedSession(store, key, 3)
            ref = PairedReference(snapshot_id=key, window="incident", source="trace", field="requests.0.inventory_ms")
            session.query("reference", "trace")
            with self.assertRaisesRegex(ValueError, "invalid_reference"):
                session.resolve(ref)
            with self.assertRaisesRegex(ValueError, "repeated_query"):
                session.query("reference", "trace")
            evidence = session.query("incident", "trace")
            evidence.data["requests"][0]["inventory_ms"] = 0
            self.assertEqual(session.resolve(ref), 60.)
            with self.assertRaisesRegex(ValueError, "budget_exhausted"):
                session.query("incident", "runtime")
            for refs in ([ref, ref], [ref.model_copy(update={"snapshot_id": "0" * 64})]):
                with self.assertRaises(ValueError):
                    session.validate_references(refs)
            self.assertNotIn("requests", str(session.evidence[0].data["windows"]))
            self.assertEqual(len(session.usage.calls), 3)

    def test_missing_runtime_cannot_resolve_hidden_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory))
            key = store.freeze(mask_sources(payload(), runtime=False))
            session = PairedSession(store, key)
            self.assertEqual(session.query("reference", "runtime").data["quality"], "unavailable")
            with self.assertRaises(ValueError):
                session.resolve(PairedReference(snapshot_id=key, window="reference", source="runtime", field="after.max_connections"))
