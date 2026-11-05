"""Collect real orders observations and replay a separately versioned protocol."""

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx

from backend.investigation.bundle_agent import (
    BundleChatModel, BundleHypothesisModel, investigate_bundle, BundleDomain, BundleLedger, BundleUpdate,
)
from backend.investigation.bundle_baseline import investigate_bundle_fixed
from backend.investigation.bundle_evidence import build_bundle, BundleSession, RuntimeReading
from backend.investigation.evidence_models import WindowQuery
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_queries import WindowEvidenceTools
from evals.run_holdout import check_observations, write
from lab.run_delay_experiment import ROOT, compose
from lab.run_pool_experiment import load, observe_trace, summarize
from lab.run_trace_experiment import wait_for_trace

PLAN = (
    dict(name="low_load_control", delay=60, connections=1, concurrency=1, timeout=1500, expected="no_anomaly_observed"),
    dict(name="wide_pool_control", delay=60, connections=6, concurrency=6, timeout=1500, expected="no_anomaly_observed"),
    dict(name="caller_wait", delay=60, connections=1, concurrency=6, timeout=1500, expected="pre_send_delay"),
    dict(name="downstream_slow", delay=350, connections=6, concurrency=6, timeout=1500, expected="inventory_latency"),
)
BUDGET = dict(max_tool_calls=6, timeout_seconds=120, max_total_tokens=40000)
REPEATS = 2


def fingerprint():
    paths = sorted((ROOT / "backend/investigation").glob("*.py"))
    paths += sorted((ROOT / "lab").rglob("*.py"))
    paths += [ROOT / "evals/run_bundle_comparison.py", ROOT / "evals/run_holdout.py",
              ROOT / "compose.yaml", ROOT / "requirements.txt", ROOT / "requirements-graph.txt", ROOT / "Dockerfile"]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()}


def model_settings(config):
    return {k: getattr(config, k) for k in ("base_url", "model", "timeout_seconds", "max_output_tokens")}


def freeze(directory, config):
    directory.mkdir(parents=True, exist_ok=False)
    write(directory / "plan.private.json", dict(protocol=fingerprint(), cases=list(PLAN),
        masked_from=["caller_wait", "downstream_slow"], budget=BUDGET, repeats=REPEATS,
        model=model_settings(config), split="development", protocol_name="bundle-comparison-v1"))


def validate_plan(directory, config=None):
    plan = json.loads((directory / "plan.private.json").read_text(encoding="utf-8"))
    if (plan["protocol"] != fingerprint() or plan["cases"] != list(PLAN) or plan["budget"] != BUDGET
            or plan["repeats"] != REPEATS or plan["masked_from"] != ["caller_wait", "downstream_slow"]
            or (config is not None and plan["model"] != model_settings(config))):
        raise ValueError("frozen_protocol_changed")
    return plan


def collect(directory, store):
    plan = validate_plan(directory)
    target = directory / "collection.private.json"
    with target.open("x", encoding="utf-8") as stream:
        stream.write("{}")
    result = dict(protocol=plan["protocol"], cases=[], completed=False, recovery_verified=False, error=None)
    write(target, result)
    base = f"http://127.0.0.1:{os.getenv('ORDERS_PORT', '8000')}"
    orders = base + "/orders/preview?sku=demo"
    jaeger = f"http://127.0.0.1:{os.getenv('JAEGER_PORT', '16686')}"
    tools = WindowEvidenceTools(store)
    try:
        for spec in PLAN:
            case = dict(name=spec["name"], expected=spec["expected"], collected=False)
            result["cases"].append(case)
            write(target, result)
            compose("up", "-d", "--wait", "--wait-timeout", "60", delay=spec["delay"],
                    max_connections=spec["connections"], pool_timeout_ms=spec["timeout"])
            with httpx.Client(timeout=5, trust_env=False) as client:
                response = client.get(base + "/observations/client")
                response.raise_for_status()
                before = RuntimeReading.model_validate(response.json()).model_dump()
                start = time.time_ns() // 1000
                samples = asyncio.run(load(orders, spec["concurrency"], requests=18))
                end = time.time_ns() // 1000
                response = client.get(base + "/observations/client")
                response.raise_for_status()
                after = RuntimeReading.model_validate(response.json()).model_dump()
                observations = [observe_trace(wait_for_trace(client, jaeger, s["trace_id"],
                                3 if s["status"] == 200 else 2), s) for s in samples]
            case["business_observations"] = observations
            case["business_summary"] = summarize(observations)
            check_observations(spec, case["business_summary"])
            if (before["in_flight"] or after["in_flight"] or
                    after["requests_finished"] - before["requests_finished"] != 19 or
                    after["max_connections"] != spec["connections"] or after["pool_timeout_ms"] != spec["timeout"]):
                raise ValueError("runtime_observation_mismatch")
            evidence = tools.query_window(WindowQuery(start_us=start, end_us=end))
            if evidence.status != "ok" or evidence.data["quality"] != "observed":
                raise ValueError("window_not_observed")
            original = store.read(evidence.data["snapshot_id"])
            if len(original["traces"]) != 19 or not {s["trace_id"] for s in samples} <= original["traces"].keys():
                raise ValueError("membership_mismatch")
            payload = build_bundle(original, before, after)
            case.update(snapshot_id=store.freeze(payload), collected=True,
                        original_snapshot_id=evidence.data["snapshot_id"], runtime_before=before, runtime_after=after)
            if spec["name"] in plan["masked_from"]:
                masked = build_bundle(original, before, after, traces_available=False)
                result["cases"].append(dict(name=spec["name"] + "_missing_trace", expected="insufficient_evidence",
                    snapshot_id=store.freeze(masked), collected=True, derived_from=spec["name"],
                    observation_condition="trace_unavailable"))
            write(target, result)
            print(spec["name"] + ": collected", flush=True)
        validate_plan(directory)
        result["completed"] = True
    except BaseException as exc:
        result["error"] = type(exc).__name__
        raise
    finally:
        try:
            compose("up", "-d", "--wait", "--wait-timeout", "60", delay=0)
            with httpx.Client(timeout=5, trust_env=False) as client:
                response = client.get(orders)
                runtime = client.get(base + "/observations/client").json()
                result["recovery_verified"] = (response.status_code == 200 and response.json() ==
                    {"sku": "demo", "can_order": True} and runtime["max_connections"] == 100
                    and runtime["pool_timeout_ms"] == 2000)
            if not result["recovery_verified"]:
                raise ValueError("recovery_failed")
        finally:
            write(target, result)
    return result


def score(report, case, store):
    """Label/protocol acceptance, NOT a semantic or causal judge of free text."""
    if report.termination != "completed" or report.snapshot_id != case["snapshot_id"] or report.finding.code != case["expected"]:
        return False
    session = BundleSession(store, report.snapshot_id)
    try:
        for item in report.evidence[1:]:
            actual = session.query(item.tool)
            if actual.data != item.data or actual.parameters != item.parameters:
                return False
        if report.evidence[0].data != session.evidence[0].data:
            return False
        BundleDomain(session, "audit").validate_finding(report.finding)
        if report.strategy == "bundle_hypotheses_v1":
            ledger = BundleLedger()
            for revision in report.hypothesis_history:
                if not 1 <= revision.evidence_count <= len(report.evidence):
                    return False
                audit_session = BundleSession(store, report.snapshot_id)
                for item in report.evidence[1:revision.evidence_count]:
                    audit_session.query(item.tool)
                update = BundleUpdate(**revision.model_dump(include=set(BundleUpdate.model_fields)))
                ledger = ledger.propose([update], audit_session, revision.turn)
            versions = ledger.validate_claim(report.finding, list(report.claim_hypothesis_versions))
            if versions != report.claim_hypothesis_versions:
                return False
        return True
    except (ValueError, KeyError):
        return False


def validate_collection(directory, store):
    plan = validate_plan(directory)
    manifest = json.loads((directory / "collection.private.json").read_text(encoding="utf-8"))
    expected = []
    for spec in PLAN:
        expected.append((spec["name"], spec["expected"]))
        if spec["name"] in plan["masked_from"]:
            expected.append((spec["name"] + "_missing_trace", "insufficient_evidence"))
    if (not manifest["completed"] or not manifest["recovery_verified"] or manifest["protocol"] != plan["protocol"]
            or [(c["name"], c["expected"]) for c in manifest["cases"]] != expected):
        raise ValueError("invalid_collection")
    for case in manifest["cases"]:
        if not case["collected"] or BundleSession(store, case["snapshot_id"])._payload is None:
            raise ValueError("invalid_case")
        if "derived_from" not in case:
            spec = next(s for s in PLAN if s["name"] == case["name"])
            if summarize(case["business_observations"]) != case["business_summary"]:
                raise ValueError("business_summary_mismatch")
            check_observations(spec, case["business_summary"])
            original = store.read(case["original_snapshot_id"])
            rebuilt = build_bundle(original, case["runtime_before"], case["runtime_after"])
        else:
            parent = next(c for c in manifest["cases"] if c["name"] == case["derived_from"])
            rebuilt = build_bundle(store.read(parent["original_snapshot_id"]),
                                   parent["runtime_before"], parent["runtime_after"], traces_available=False)
        if rebuilt != store.read(case["snapshot_id"]):
            raise ValueError("bundle_provenance_mismatch")
    return manifest


async def evaluate(directory, store, config=None):
    if config is not None:
        validate_plan(directory, config)
    manifest = validate_collection(directory, store)
    reports = directory / ("reports" if config is not None else "fixed-reports")
    reports.mkdir(exist_ok=False)
    result = dict(completed=False, runs=[], semantic_review="pending", error=None)
    target = reports / "result.private.json"
    try:
        for index, case in enumerate(manifest["cases"]):
            fixed = investigate_bundle_fixed(store, case["snapshot_id"], BUDGET["max_tool_calls"])
            write(reports / f"fixed-{index}.private.json", fixed.model_dump(mode="json"))
            result["runs"].append(dict(case=case["name"], strategy="fixed", status="finished",
                passed=score(fixed, case, store), termination=fixed.termination, usage=fixed.usage_totals))
        write(target, result)
        if config is not None:
            for repeat in range(REPEATS):
                for index, case in enumerate(manifest["cases"]):
                    for strategy in (("agent", "hypotheses") if repeat == 0 else ("hypotheses", "agent")):
                        validate_plan(directory, config)
                        filename = f"{repeat}-{index}-{strategy}.private.json"
                        entry = dict(case=case["name"], repeat=repeat + 1, strategy=strategy,
                                     status="started", report_file=filename)
                        result["runs"].append(entry)
                        write(target, result)
                        adapter = BundleChatModel if strategy == "agent" else BundleHypothesisModel
                        async with adapter(config) as model:
                            report = await investigate_bundle(store, case["snapshot_id"], model, strategy, **BUDGET)
                        write(reports / filename, report.model_dump(mode="json"))
                        entry.update(status="finished", passed=score(report, case, store),
                            termination=report.termination, usage=report.usage_totals)
                        write(target, result)
                        print(f"{filename}: {report.termination} passed={entry['passed']}", flush=True)
        validate_plan(directory, config)
        result["completed"] = True
    except BaseException as exc:
        result["error"] = type(exc).__name__
        raise
    finally:
        write(target, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "collect", "fixed", "evaluate"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--store", type=Path, default=ROOT / ".snapshots")
    args = parser.parse_args()
    if args.command == "freeze":
        freeze(args.directory, ModelConfig.from_env())
    elif args.command == "collect":
        collect(args.directory, SnapshotStore(args.store))
    else:
        asyncio.run(evaluate(args.directory, SnapshotStore(args.store),
                            ModelConfig.from_env() if args.command == "evaluate" else None))


if __name__ == "__main__":
    main()
