import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import httpx
from fastapi import FastAPI

from backend.investigation.bundle_evidence import RuntimeReading
from backend.investigation.paired_evidence import TimedReading
from evals.collect_paired_windows import LIMITS, audit, collect, open_loop, rebuild_window, selected, write
from lab.orders.runtime_observation import initialize, install
from test_paired_evidence import reading
from test_window_investigation import observed


def raw_window():
    trace = observed()
    for span in trace["spans"]:
        span["startTime"] = 200
    for tag in trace["spans"][1]["tags"]:
        if tag["key"] == "client.max_connections":
            tag["value"] = 2
        if tag["key"] == "client.pool_timeout_ms":
            tag["value"] = 1000
    # Independent raw observation validator requires the transport measurement.
    trace["spans"][1]["tags"] += [dict(key="client.transport_ms", value=90.),
                                     dict(key="client.in_flight_at_start", value=1)]
    return dict(before=reading(100), after=reading(1_000_200, 1), raw_traces=[trace], load=dict(
        start_us=101, end_us=1_000_100, samples=[dict(index=0, trace_id="a" * 32,
            sent_us=150, status=200, business_ok=True, client_ms=100., error=None, lag_seconds=0.)],
        errors=[], scheduled=1, max_lag_seconds=0., send_seconds=1., drain_seconds=0.))


class PairedCollectionTests(unittest.IsolatedAsyncioTestCase):
    def test_atomic_write_retries_temporary_lock_without_truncating_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.private.json"
            write(target, {"old": True})
            original_replace = Path.replace
            calls = []
            def replace(source, destination):
                calls.append(source)
                self.assertEqual(json.loads(target.read_text()), {"old": True})
                if len(calls) < 3:
                    raise PermissionError("temporary reader lock")
                return original_replace(source, destination)
            with patch.object(Path, "replace", replace), patch("evals.collect_paired_windows.time.sleep") as sleep:
                write(target, {"new": True})
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(json.loads(target.read_text()), {"new": True})
            with patch.object(Path, "replace", side_effect=PermissionError("persistent lock")) as replace, \
                 patch("evals.collect_paired_windows.time.sleep"):
                with self.assertRaises(PermissionError):
                    write(target, {"lost": True})
                self.assertEqual(replace.call_count, 6)
            self.assertEqual(json.loads(target.read_text()), {"new": True})

    async def test_open_loop_does_not_wait_for_previous_response(self):
        active, peak = 0, 0
        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.15)
            active -= 1
            return httpx.Response(200, json={"sku": "demo", "can_order": True}, headers={"X-Trace-ID": "a" * 32})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await open_loop(client, "http://test", 20, .2, {**LIMITS, "max_tail_fraction": 2.})
        self.assertEqual(len(result["samples"]), 4)
        self.assertGreater(peak, 1)
        self.assertEqual(result["errors"], [])

    async def test_inflight_limit_does_not_wait_and_cancel_drains_tasks(self):
        active = 0
        async def handler(request):
            nonlocal active
            active += 1
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await open_loop(client, "http://test", 20, .1,
                {**LIMITS, "max_in_flight": 1, "drain_timeout": .01})
        self.assertIn("in_flight_limit", result["errors"])
        self.assertIn("drain_timeout", result["errors"])
        self.assertEqual(active, 0)
        self.assertEqual(len(result["samples"]), 1)
        self.assertEqual(result["samples"][0]["error"], "CancelledError")

    async def test_external_cancel_waits_for_child_cleanup(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def handler(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(open_loop(client, "http://test", 10, 1., LIMITS))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cleaned.is_set())

    async def test_late_scheduler_is_recorded_without_catchup_burst(self):
        async def handler(request):
            time.sleep(.15)  # Deliberately block the driver, not the target service.
            return httpx.Response(200, json={"sku": "demo", "can_order": True}, headers={"X-Trace-ID": "a" * 32})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await open_loop(client, "http://test", 20, .2, LIMITS)
        self.assertIn("schedule_lag", result["errors"])
        self.assertEqual(len(result["samples"]), 1)

    async def test_request_timeout_is_retained_and_duration_is_not_shortened(self):
        async def handler(request):
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            start = time.perf_counter()
            result = await open_loop(client, "http://test", 10, .1, {**LIMITS, "request_timeout": .01})
            self.assertGreaterEqual(time.perf_counter() - start, .1)
        self.assertEqual(result["samples"][0]["error"], "TimeoutError")

    async def test_old_and_timed_endpoints_preserve_legacy_schema(self):
        app = FastAPI()
        initialize(app, 2, 1000)
        install(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            old = RuntimeReading.model_validate((await client.get("/observations/client")).json())
            a = TimedReading.model_validate((await client.get("/observations/client/timed")).json())
            b = TimedReading.model_validate((await client.get("/observations/client/timed")).json())
        self.assertEqual(old.process_id, a.process_id)
        self.assertGreater(b.monotonic_us, a.monotonic_us)
        self.assertEqual(b.requests_started, 0)

    def test_raw_projection_and_rejection_of_membership_timing_or_driver_errors(self):
        raw = raw_window()
        self.assertEqual(len(rebuild_window(raw, LIMITS).trace.requests), 1)
        for change in (lambda r: r["after"].update(requests_started=2, requests_finished=2),
                       lambda r: r["load"].update(errors=["schedule_lag"]),
                       lambda r: r["load"].update(max_lag_seconds=.2),
                       lambda r: r["load"].update(drain_seconds=.3),
                       lambda r: r.update(raw_traces=[]),
                       lambda r: r["load"]["samples"][0].update(business_ok=False),
                       lambda r: r["after"].update(monotonic_us=2_000_000)):
            altered = copy.deepcopy(raw)
            change(altered)
            with self.subTest(change=change), self.assertRaises(ValueError):
                rebuild_window(altered, LIMITS)

    def test_sampling_does_not_depend_on_latency_or_status(self):
        samples = [dict(trace_id=f"{i:032x}", client_ms=i, status=200) for i in range(100)]
        ids = [s["trace_id"] for s in selected(samples, 10)]
        for sample in samples:
            sample.update(client_ms=999, status=503)
        self.assertEqual(ids, [s["trace_id"] for s in selected(list(reversed(samples)), 10)])

    def test_trace_bounds_use_server_clock_despite_small_host_offset(self):
        raw = raw_window()
        raw["load"]["start_us"] = 90  # Host time precedes the server before-reading.
        window = rebuild_window(raw, LIMITS)
        self.assertEqual(window.start_us, raw["before"]["observed_us"])
        self.assertEqual(window.end_us, raw["after"]["observed_us"])
        raw["raw_traces"][0]["spans"][0]["startTime"] = 99
        with self.assertRaisesRegex(ValueError, "trace_projection_incomplete"):
            rebuild_window(raw, LIMITS)

    def test_failed_attempts_retained_restoration_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run"
            with patch("evals.collect_paired_windows.compose", side_effect=RuntimeError("fixture")), \
                 patch("evals.collect_paired_windows.recover", return_value=dict(verified=True)) as recovery:
                result = collect(target, names=["stable"], repeats=2, duration=1.)
                self.assertTrue(result["completed"])
                self.assertFalse(result["all_valid"])
                self.assertEqual(len(result["cases"]), 2)
                self.assertTrue(all(c["error"].startswith("RuntimeError") for c in result["cases"]))
                recovery.assert_called_once()
                with self.assertRaises(FileExistsError):
                    collect(target, names=["stable"], repeats=1, duration=1.)
            saved = json.loads((target / "collection.private.json").read_text())
            self.assertTrue(saved["recovery"]["verified"])

    def test_interrupt_and_recovery_failure_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run"
            with patch("evals.collect_paired_windows.compose", side_effect=KeyboardInterrupt), \
                 patch("evals.collect_paired_windows.recover", side_effect=RuntimeError("fixture")):
                with self.assertRaises(RuntimeError):
                    collect(target, names=["stable"], repeats=1, duration=1.)
            saved = json.loads((target / "collection.private.json").read_text())
            self.assertEqual(saved["error"], "KeyboardInterrupt")
            self.assertFalse(saved["recovery"]["verified"])

    def test_collection_offline_audit_and_tamper_rejection(self):
        windows, traces = [], {}
        for offset in (0, 2_000_000):
            raw = raw_window()
            raw["before"] = reading(100 + offset, 0 if offset == 0 else 10)
            raw["after"] = reading(1_000_200 + offset, 10 if offset == 0 else 20)
            raw["load"].update(start_us=101 + offset, end_us=1_000_100 + offset, scheduled=10)
            sample, trace = raw["load"]["samples"][0], raw["raw_traces"][0]
            raw["load"]["samples"], raw["raw_traces"] = [], []
            for index in range(10):
                trace_id = f"{offset + index + 1:032x}"
                raw["load"]["samples"].append({**sample, "index": index, "trace_id": trace_id, "sent_us": 150 + offset})
                item = copy.deepcopy(trace)
                item["traceID"] = trace_id
                for span in item["spans"]:
                    span.update(traceID=trace_id, startTime=200 + offset)
                    for ref in span.get("references", []):
                        ref["traceID"] = trace_id
                traces[trace_id] = item
            windows.append(raw)
        recovery = dict(verified=True, runtime={**reading(5_000_000), "max_connections": 100, "pool_timeout_ms": 2000},
                        status=200, business={"sku": "demo", "can_order": True})
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run"
            with patch("evals.collect_paired_windows.compose"), \
                 patch("evals.collect_paired_windows.drive", new=AsyncMock(side_effect=windows)), \
                 patch("evals.collect_paired_windows.wait_for_trace", side_effect=lambda c, j, key, n: traces[key]), \
                 patch("evals.collect_paired_windows.recover", return_value=recovery):
                result = collect(target, names=["stable"], repeats=1, duration=1.)
            self.assertTrue(result["all_valid"], result)
            self.assertEqual(audit(target), dict(valid=1, attempted=1, task_ready=1, recovery=True))
            path = target / "collection.private.json"
            original = path.read_bytes()
            for change in (lambda r: r["cases"][0]["observations"]["incident"].update(rate=999),
                           lambda r: r["cases"][0]["feasibility"].update(ready=False),
                           lambda r: r["cases"][0]["windows"]["incident"]["load"].update(scheduled=9),
                           lambda r: r["recovery"].update(status=503),
                           lambda r: r.update(all_valid=False)):
                changed = json.loads(original)
                change(changed)
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(ValueError):
                    audit(target)
            path.write_bytes(original)
            plan_path = target / "plan.private.json"
            plan = json.loads(plan_path.read_text())
            plan["limits"]["max_lag_seconds"] = 100
            plan_path.write_text(json.dumps(plan))
            with self.assertRaisesRegex(ValueError, "invalid_frozen_plan"):
                audit(target)
