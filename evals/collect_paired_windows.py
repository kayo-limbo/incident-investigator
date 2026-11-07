"""Bounded open-loop development collection; never invokes a model."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import time
import zipfile
from pathlib import Path

import httpx

from backend.investigation.evidence_models import WindowQuery
from backend.investigation.paired_evidence import PairedBundle, PairedWindow, TimedReading, mask_sources
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_investigation import trace_index
from backend.investigation.window_queries import project_window
from lab.run_delay_experiment import ROOT, compose
from lab.run_pool_experiment import observe_trace, percentile
from lab.run_trace_experiment import wait_for_trace

# Development parameters, not a held-out benchmark. Tuple: delay ms, pool, requests/s.
CASES = {
    "stable": ((60, 2, 10), (60, 2, 10)),
    "downstream": ((60, 6, 10), (350, 6, 10)),
    "capacity": ((60, 2, 17), (60, 1, 17)),
    "arrival": ((60, 2, 20), (60, 2, 33)),
    "restart": ((60, 2, 10), (60, 2, 10)),
}
LIMITS = dict(max_requests=1000, max_in_flight=100, request_timeout=5.0,
              drain_timeout=10.0, max_lag_seconds=.1, max_tail_fraction=.2,
              max_interval_overhead_fraction=.25, trace_sample_size=40)


def write(path, value):
    # Only the current run manifest is replaced; run directory is exclusive.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            # Windows readers may temporarily deny deletion of the target.
            # Preserve the old complete manifest; never fall back to truncation.
            if attempt == 5:
                raise
            time.sleep(.02 * (attempt + 1))


def sources():
    paths = sorted((ROOT / "backend/investigation").glob("*.py")) + sorted((ROOT / "lab").rglob("*.py"))
    paths += [Path(__file__), ROOT / "compose.yaml", ROOT / "Dockerfile", ROOT / "requirements.txt"]
    return {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in paths}


async def open_loop(client, url, rate, duration, limits):
    if (type(rate) is not int or not 1 <= rate <= 100 or not math.isfinite(duration) or
            not 0 < duration <= 60 or not 1 <= math.ceil(rate * duration) <= limits["max_requests"]):
        raise ValueError("invalid_load_budget")
    # Windows event-loop time can be coarser than perf_counter. Recheck the
    # actual deadline after sleep so early wakeups do not shorten the window.
    async def sleep_until(deadline):
        while (remaining := deadline - time.perf_counter()) > 0:
            await asyncio.sleep(max(remaining, .001))

    started = time.perf_counter()
    result = dict(start_us=time.time_ns() // 1000, samples=[], errors=[], scheduled=math.ceil(rate * duration),
                  max_lag_seconds=0.0, send_seconds=duration, drain_seconds=0.0)
    tasks = []
    active = set()

    async def send(index, planned):
        sent = time.perf_counter()
        lag = max(0.0, sent - planned)
        sample = dict(index=index, sent_us=time.time_ns() // 1000, lag_seconds=lag,
                      status=None, trace_id=None, client_ms=0.0, business_ok=False, error=None)
        result["samples"].append(sample)
        result["max_lag_seconds"] = max(result["max_lag_seconds"], lag)
        try:
            async with asyncio.timeout(limits["request_timeout"]):
                response = await client.get(url)
                sample.update(status=response.status_code, trace_id=response.headers.get("X-Trace-ID"))
                sample["business_ok"] = (response.status_code == 200 and
                    response.json() == {"sku": "demo", "can_order": True})
        except asyncio.CancelledError:
            sample["error"] = "CancelledError"
            raise
        except Exception as exc:
            sample["error"] = type(exc).__name__
        finally:
            sample["client_ms"] = (time.perf_counter() - sent) * 1000

    try:
        for index in range(result["scheduled"]):
            planned = started + index / rate
            await sleep_until(planned)
            if time.perf_counter() - planned > limits["max_lag_seconds"]:
                result["errors"].append("schedule_lag")
                break
            if len(active) >= limits["max_in_flight"]:
                result["errors"].append("in_flight_limit")
                break
            task = asyncio.create_task(send(index, planned))
            tasks.append(task)
            active.add(task)
            task.add_done_callback(active.discard)
        await sleep_until(started + duration)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=limits["drain_timeout"])
            if pending:
                result["errors"].append("drain_timeout")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    result["end_us"] = time.time_ns() // 1000
    result["drain_seconds"] = max(0.0, time.perf_counter() - started - duration)
    result["samples"].sort(key=lambda s: s["index"])
    if result["max_lag_seconds"] > limits["max_lag_seconds"]:
        result["errors"].append("schedule_lag")
    if result["drain_seconds"] > duration * limits["max_tail_fraction"]:
        result["errors"].append("excessive_tail")
    return result


async def drive(base, rate, duration, limits):
    async with httpx.AsyncClient(timeout=limits["request_timeout"], trust_env=False,
            limits=httpx.Limits(max_connections=limits["max_in_flight"],
                               max_keepalive_connections=limits["max_in_flight"])) as client:
        url = base + "/orders/preview?sku=demo"
        # Same fixed warmup for every window, excluded from runtime and business counts.
        for _ in range(3):
            response = await client.get(url)
            if response.status_code != 200 or response.json() != {"sku": "demo", "can_order": True}:
                raise ValueError("warmup_failed")
        await asyncio.sleep(.1)
        response = await client.get(base + "/observations/client/timed")
        response.raise_for_status()
        before = TimedReading.model_validate(response.json()).model_dump()
        load = await open_loop(client, url, rate, duration, limits)
        response = await client.get(base + "/observations/client/timed")
        response.raise_for_status()
        after = TimedReading.model_validate(response.json()).model_dump()
    return dict(before=before, after=after, load=load, raw_traces=[])


def selected(samples, size):
    return sorted(samples, key=lambda s: hashlib.sha256(s["trace_id"].encode()).hexdigest())[:size]


def business_summary(samples):
    return dict(request_count=len(samples), client_p95_ms=percentile([s["client_ms"] for s in samples], .95),
                failed_requests=sum(not s["business_ok"] for s in samples))


def rebuild_window(raw, limits):
    load, before, after = raw["load"], raw["before"], raw["after"]
    samples = load["samples"]
    if (load["errors"] or len(samples) != load["scheduled"] or
            load["max_lag_seconds"] > limits["max_lag_seconds"] or
            load["drain_seconds"] > load["send_seconds"] * limits["max_tail_fraction"]):
        raise ValueError("invalid_driver_window")
    if (not samples or len(samples) > limits["max_requests"] or
            [s["index"] for s in samples] != list(range(len(samples))) or
            any(s["error"] or s["status"] not in (200, 503) or not s["trace_id"] or
                (s["status"] == 200 and not s["business_ok"]) or
                not load["start_us"] <= s["sent_us"] < load["end_us"] for s in samples) or
            len({s["trace_id"] for s in samples}) != len(samples)):
        raise ValueError("invalid_business_observations")
    if (after["requests_started"] - before["requests_started"] != len(samples) or
            after["requests_finished"] - before["requests_finished"] != len(samples)):
        raise ValueError("runtime_membership_mismatch")
    elapsed = (after["monotonic_us"] - before["monotonic_us"]) / 1_000_000
    if not load["send_seconds"] <= elapsed <= load["send_seconds"] * (1 + limits["max_interval_overhead_fraction"]):
        raise ValueError("runtime_interval_overhead")
    expected = selected(samples, limits["trace_sample_size"])
    if [t["traceID"] for t in raw["raw_traces"]] != [s["trace_id"] for s in expected]:
        raise ValueError("sample_membership_mismatch")
    # Independent raw-span checks retain status/parentage validation before projection.
    for trace, sample in zip(raw["raw_traces"], expected):
        observe_trace(trace, sample)
    # Jaeger orders spans and runtime readings use the container wall clock.
    # Host driver timestamps are not strictly comparable to that clock.
    start_us, end_us = before["observed_us"], after["observed_us"]
    query = WindowQuery(start_us=start_us, end_us=end_us, limit=200)
    projected = project_window({"data": raw["raw_traces"]}, query)
    rows = trace_index(projected)
    if projected["rejected"] or projected["excluded"] or len(rows) != len(expected):
        raise ValueError("trace_projection_incomplete")
    for row in rows:
        if row["max_connections"] != after["max_connections"] or row["pool_timeout_ms"] != after["pool_timeout_ms"]:
            raise ValueError("trace_runtime_config_mismatch")
    return PairedWindow.model_validate(dict(start_us=start_us, end_us=end_us,
        runtime=dict(before=before, after=after), trace=dict(start_us=start_us, end_us=end_us,
        quality="observed", coverage="sampled", requests=rows)))


def rebuild_pair(windows, limits):
    return PairedBundle(alert=business_summary(windows["incident"]["load"]["samples"]),
        reference=rebuild_window(windows["reference"], limits),
        incident=rebuild_window(windows["incident"], limits))


def describe(bundle):
    result = {}
    for name in ("reference", "incident"):
        window = getattr(bundle, name)
        result[name] = dict(rate=window.runtime.request_rate(), pool=window.runtime.after.max_connections,
            process_id=window.runtime.after.process_id, sample_size=len(window.trace.requests),
            **{k + "_p95": percentile([getattr(r, k) for r in window.trace.requests if getattr(r, k) is not None], .95)
               for k in ("orders_ms", "inventory_ms", "pre_send_ms")})
    return result


def assess_case(name, windows, bundle):
    """Development feasibility gates, separate from collection integrity or model scoring."""
    metrics = describe(bundle)
    a, b = metrics["reference"], metrics["incident"]
    ba = business_summary(windows["reference"]["load"]["samples"])
    bb = business_summary(windows["incident"]["load"]["samples"])
    failures = []
    for window, spec in zip(("reference", "incident"), CASES[name]):
        reading = getattr(bundle, window).runtime.after
        if reading.max_connections != spec[1] or reading.pool_timeout_ms != 1000:
            failures.append(window + "_unexpected_config")
    if (ba["failed_requests"] or ba["client_p95_ms"] >= 150 or a["inventory_ms_p95"] is None or
            a["inventory_ms_p95"] >= 150 or a["pre_send_ms_p95"] is None or a["pre_send_ms_p95"] >= 50):
        failures.append("reference_not_healthy")
    ratio = b["rate"] / a["rate"] if a["rate"] else 0
    if name == "arrival":
        if ratio < 1.5:
            failures.append("arrival_change_not_observed")
    elif not .9 <= ratio <= 1.1:
        failures.append("arrival_rate_not_controlled")
    if name in ("stable", "restart"):
        if bb["failed_requests"] or bb["client_p95_ms"] >= 150:
            failures.append("control_degraded")
    elif name == "downstream":
        if b["inventory_ms_p95"] is None or b["inventory_ms_p95"] < 250 or bb["client_p95_ms"] < 250:
            failures.append("downstream_symptom_not_observed")
    elif (b["pre_send_ms_p95"] is None or b["pre_send_ms_p95"] < 150 or
          b["inventory_ms_p95"] is None or b["inventory_ms_p95"] >= 150 or
          bb["client_p95_ms"] < ba["client_p95_ms"] + 100):
        failures.append("caller_symptom_not_observed")
    changed_process = a["process_id"] != b["process_id"]
    if (name in ("restart", "capacity")) != changed_process:
        failures.append("unexpected_process_transition")
    return dict(ready=not failures, failures=failures, business_reference=ba, business_incident=bb,
                rate_ratio=ratio, source_review="manual_review_required")


def recover(base):
    compose("up", "-d", "--wait", "--wait-timeout", "60", delay=0)
    with httpx.Client(timeout=5, trust_env=False) as client:
        response = client.get(base + "/orders/preview?sku=demo")
        runtime = client.get(base + "/observations/client/timed")
        runtime.raise_for_status()
        reading = TimedReading.model_validate(runtime.json())
        if (response.status_code != 200 or response.json() != {"sku": "demo", "can_order": True} or
                reading.max_connections != 100 or reading.pool_timeout_ms != 2000):
            raise ValueError("recovery_failed")
        return dict(verified=True, runtime=reading.model_dump(), status=response.status_code, business=response.json())


def collect(directory, *, names=None, repeats=2, duration=15.0):
    names = list(CASES) if names is None else names
    if (not names or len(set(names)) != len(names) or any(n not in CASES for n in names) or
            type(repeats) is not int or not 1 <= repeats <= 3 or not math.isfinite(duration) or not 1 <= duration <= 30 or
            any(math.ceil(s[2] * duration) > LIMITS["max_requests"] for n in names for s in CASES[n])):
        raise ValueError("invalid_collection_plan")
    directory.mkdir(parents=True, exist_ok=False)
    source = sources()
    plan = dict(protocol="paired-development-v1", split="development", duration=duration, repeats=repeats,
                cases={n: CASES[n] for n in names}, limits=LIMITS,
                source_hashes={p: hashlib.sha256(b).hexdigest() for p, b in source.items()})
    write(directory / "plan.private.json", plan)
    with zipfile.ZipFile(directory / "protocol-sources.zip", "x") as archive:
        for name, content in source.items():
            archive.writestr(name, content)
    target = directory / "collection.private.json"
    result = dict(completed=False, cases=[], recovery=None, error=None)
    store = SnapshotStore(directory / "snapshots")
    base = f"http://127.0.0.1:{os.getenv('ORDERS_PORT', '8000')}"
    jaeger = f"http://127.0.0.1:{os.getenv('JAEGER_PORT', '16686')}"
    write(target, result)
    try:
        for repeat in range(repeats):
            for name in names:
                entry = dict(name=name, repeat=repeat, valid=False, windows={}, error=None)
                result["cases"].append(entry)
                write(target, result)
                try:
                    for window, spec in zip(("reference", "incident"), CASES[name]):
                        delay, pool, rate = spec
                        args = ["up", "-d", "--wait", "--wait-timeout", "60"]
                        if name == "restart" and window == "incident":
                            args += ["--force-recreate", "orders"]
                        compose(*args, delay=delay, max_connections=pool, pool_timeout_ms=1000)
                        raw = asyncio.run(drive(base, rate, duration, LIMITS))
                        entry["windows"][window] = raw
                        write(target, result)
                        if not raw["load"]["errors"] and all(s["trace_id"] for s in raw["load"]["samples"]):
                            with httpx.Client(timeout=5, trust_env=False) as client:
                                for sample in selected(raw["load"]["samples"], LIMITS["trace_sample_size"]):
                                    raw["raw_traces"].append(wait_for_trace(client, jaeger, sample["trace_id"],
                                                                                 3 if sample["status"] == 200 else 2))
                                    write(target, result)
                        rebuild_window(raw, LIMITS)
                    bundle = rebuild_pair(entry["windows"], LIMITS)
                    entry.update(snapshot_id=store.freeze(bundle.model_dump(mode="json")),
                                 observations=describe(bundle), valid=True,
                                 feasibility=assess_case(name, entry["windows"], bundle))
                    entry["source_ablations"] = {
                        "trace_only": store.freeze(mask_sources(bundle.model_dump(mode="json"), runtime=False)),
                        "runtime_only": store.freeze(mask_sources(bundle.model_dump(mode="json"), trace=False))}
                except Exception as exc:
                    entry["error"] = type(exc).__name__ + ":" + str(exc)[:200]
                write(target, result)
                print(f"{repeat}/{name}: valid={entry['valid']} error={entry['error']}", flush=True)
        result["completed"] = True
        result["all_valid"] = all(c["valid"] for c in result["cases"])
    except BaseException as exc:
        result["error"] = type(exc).__name__
        raise
    finally:
        try:
            result["recovery"] = recover(base)
        except BaseException as exc:
            result["recovery"] = dict(verified=False, error=type(exc).__name__)
            raise
        finally:
            write(target, result)
    return result


def audit(directory):
    plan = json.loads((directory / "plan.private.json").read_text(encoding="utf-8"))
    result = json.loads((directory / "collection.private.json").read_text(encoding="utf-8"))
    if plan["source_hashes"] != {p: hashlib.sha256(b).hexdigest() for p, b in sources().items()}:
        raise ValueError("frozen_protocol_changed")
    if (plan["protocol"] != "paired-development-v1" or plan["split"] != "development" or
            plan["limits"] != LIMITS or not 1 <= plan["repeats"] <= 3 or
            not 1 <= plan["duration"] <= 30 or not plan["cases"] or
            any(n not in CASES or specs != [list(s) for s in CASES[n]] for n, specs in plan["cases"].items())):
        raise ValueError("invalid_frozen_plan")
    with zipfile.ZipFile(directory / "protocol-sources.zip") as archive:
        if {p: hashlib.sha256(archive.read(p)).hexdigest() for p in archive.namelist()} != plan["source_hashes"]:
            raise ValueError("source_archive_mismatch")
    if not result["completed"] or not result["recovery"]["verified"]:
        raise ValueError("collection_not_complete_or_recovered")
    recovery = result["recovery"]
    runtime = TimedReading.model_validate(recovery["runtime"])
    if (runtime.max_connections != 100 or runtime.pool_timeout_ms != 2000 or recovery["status"] != 200 or
            recovery["business"] != {"sku": "demo", "can_order": True}):
        raise ValueError("invalid_recovery_observation")
    if [(c["repeat"], c["name"]) for c in result["cases"]] != [
            (i, n) for i in range(plan["repeats"]) for n in plan["cases"]]:
        raise ValueError("case_membership_mismatch")
    store = SnapshotStore(directory / "snapshots")
    if result["all_valid"] != all(c["valid"] for c in result["cases"]):
        raise ValueError("valid_count_mismatch")
    for entry in result["cases"]:
        if not entry["valid"]:
            continue
        bundle = rebuild_pair(entry["windows"], plan["limits"])
        for window, spec in zip(("reference", "incident"), plan["cases"][entry["name"]]):
            load = entry["windows"][window]["load"]
            if load["send_seconds"] != plan["duration"] or load["scheduled"] != math.ceil(spec[2] * plan["duration"]):
                raise ValueError("load_plan_mismatch")
        if store.read(entry["snapshot_id"]) != bundle.model_dump(mode="json") or entry["observations"] != describe(bundle):
            raise ValueError("bundle_provenance_mismatch")
        if entry["feasibility"] != assess_case(entry["name"], entry["windows"], bundle):
            raise ValueError("feasibility_mismatch")
        for name, kwargs in (("trace_only", dict(runtime=False)), ("runtime_only", dict(trace=False))):
            if store.read(entry["source_ablations"][name]) != mask_sources(bundle.model_dump(mode="json"), **kwargs):
                raise ValueError("ablation_provenance_mismatch")
    return dict(valid=sum(c["valid"] for c in result["cases"]), attempted=len(result["cases"]),
                task_ready=sum(c.get("feasibility", {}).get("ready", False) for c in result["cases"]), recovery=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("collect", "audit"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--cases", nargs="+", choices=list(CASES))
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--duration", type=float, default=15.0)
    args = parser.parse_args()
    if args.mode == "audit":
        print(json.dumps(audit(args.directory)))
    else:
        result = collect(args.directory, names=args.cases, repeats=args.repeats, duration=args.duration)
        if not result["all_valid"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
