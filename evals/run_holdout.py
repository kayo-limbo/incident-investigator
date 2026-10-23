"""预先固定参数的新样本采集与重复回放；评测真值不进入调查工具。"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from backend.investigation.evidence_models import WindowQuery
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_queries import WindowEvidenceTools
from evals.run_hypothesis_comparison import replay
from lab.run_delay_experiment import ROOT, compose
from lab.run_pool_experiment import load, observe_trace, summarize
from lab.run_trace_experiment import wait_for_trace

# 在采集和查看模型输出前固定；每组18次请求，另有一次预热。
PLAN = (
    dict(name="small_pool_low_load", delay=40, connections=1, concurrency=1, timeout=800, expected="no_anomaly_observed"),
    dict(name="queue_under_load", delay=60, connections=1, concurrency=6, timeout=1000, expected="pre_send_delay"),
    dict(name="slower_inventory", delay=350, connections=6, concurrency=6, timeout=800, expected="inventory_latency"),
    dict(name="fast_inventory_pool_timeout", delay=90, connections=1, concurrency=6, timeout=35, expected="pool_contention"),
    dict(name="slow_inventory_pool_timeout", delay=350, connections=2, concurrency=6, timeout=100, expected="inventory_latency_with_pool_contention"),
)


def protocol_fingerprint():
    """冻结实际工作区代码，而非仅记录可能与工作区不同的 HEAD。"""
    paths = sorted((ROOT / "backend/investigation").glob("*.py"))
    paths += [ROOT / "evals/run_holdout.py", ROOT / "evals/run_hypothesis_comparison.py",
              ROOT / "evals/run_window_comparison.py", ROOT / "lab/run_pool_experiment.py",
              ROOT / "lab/run_trace_experiment.py", ROOT / "lab/run_delay_experiment.py"]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_observations(case, summary):
    """独立负载和原始span验收；不用调查策略的输出决定场景是否成立。"""
    slow = summary["inventory_p95_ms"] is not None and summary["inventory_p95_ms"] >= 250
    wait = summary["pre_send_success_p95_ms"] is not None and summary["pre_send_success_p95_ms"] >= 150
    pool = summary["pool_timeouts"] > 0
    expected = case["expected"]
    checks = {
        "no_anomaly_observed": not slow and not wait and not pool and summary["orders_p95_ms"] < 250,
        "pre_send_delay": not slow and wait and not pool,
        "inventory_latency": slow and not wait and not pool,
        "pool_contention": not slow and pool,
        "inventory_latency_with_pool_contention": slow and pool,
    }
    if summary["requests"] != 18 or summary["successes"] <= 0 or not checks[expected]:
        raise ValueError("scenario_not_observed")


def collect(output, store):
    if output.exists():
        raise ValueError("输出文件已存在，不能覆盖原始验收")
    fingerprint = protocol_fingerprint()
    result = dict(recorded_at=datetime.now(timezone.utc).isoformat(), protocol=fingerprint,
                  plan=list(PLAN), requests_per_case=18, cases=[], completed=False,
                  recovery_verified=False, error=None)
    write(output, result)  # 修改环境前保存计划，失败也保留。
    orders = f"http://127.0.0.1:{os.getenv('ORDERS_PORT', '8000')}/orders/preview?sku=demo"
    jaeger = f"http://127.0.0.1:{os.getenv('JAEGER_PORT', '16686')}"
    tools = WindowEvidenceTools(store)
    try:
        for spec in PLAN:
            case = dict(name=spec["name"], expected=spec["expected"], collected=False)
            result["cases"].append(case)
            compose("up", "-d", "--wait", "--wait-timeout", "60", delay=spec["delay"],
                    max_connections=spec["connections"], pool_timeout_ms=spec["timeout"])
            start = time.time_ns() // 1000
            samples = asyncio.run(load(orders, spec["concurrency"], requests=18))
            end = time.time_ns() // 1000
            observations = []
            with httpx.Client(timeout=5, trust_env=False) as client:
                for sample in samples:
                    raw = wait_for_trace(client, jaeger, sample["trace_id"], 3 if sample["status"] == 200 else 2)
                    observations.append(observe_trace(raw, sample))
            case["business_summary"] = summarize(observations)
            check_observations(spec, case["business_summary"])
            evidence = tools.query_window(WindowQuery(start_us=start, end_us=end))
            if evidence.status != "ok" or evidence.data.get("quality") != "observed":
                raise ValueError("window_not_observed")
            snapshot = store.read(evidence.data["snapshot_id"])
            if len(snapshot["traces"]) != 19 or not {s["trace_id"] for s in samples} <= snapshot["traces"].keys():
                raise ValueError("window_membership_mismatch")
            case.update(snapshot_id=evidence.data["snapshot_id"], collected=True)
            write(output, result)
            print(spec["name"] + ": collected", flush=True)
        if fingerprint != protocol_fingerprint():
            raise ValueError("protocol_changed_during_collection")
        result["completed"] = True
    except Exception as exc:
        result["error"] = type(exc).__name__  # 不把任意外部响应写入公开错误说明。
        raise
    finally:
        try:
            compose("up", "-d", "--wait", "--wait-timeout", "60", delay=0)
            with httpx.Client(timeout=5, trust_env=False) as client:
                response = client.get(orders)
                result["recovery_verified"] = response.status_code == 200 and response.json() == {"sku": "demo", "can_order": True}
            if not result["recovery_verified"]:
                raise ValueError("recovery_failed")
        finally:
            write(output, result)
    return result


def evaluate(source, directory, store, config, repeats=2):
    manifest = json.loads(source.read_text(encoding="utf-8"))
    if not manifest.get("completed") or not manifest.get("recovery_verified"):
        raise ValueError("collection_or_recovery_incomplete")
    if manifest["plan"] != list(PLAN) or len(manifest["cases"]) != len(PLAN):
        raise ValueError("unexpected_plan")
    for spec, case in zip(PLAN, manifest["cases"]):
        if not case.get("collected") or (case["name"], case["expected"]) != (spec["name"], spec["expected"]):
            raise ValueError("incomplete_cases")
        check_observations(spec, case["business_summary"])
        store.read(case["snapshot_id"])
    if manifest["protocol"] != protocol_fingerprint():
        raise ValueError("protocol_changed_since_collection")
    if type(repeats) is not int or not 1 <= repeats <= 5:
        raise ValueError("repeats_out_of_range")
    directory.mkdir(parents=True, exist_ok=False)
    runs = dict(protocol=manifest["protocol"], repeats=repeats, completed=False, trials=[])
    try:
        for trial in range(1, repeats + 1):
            if protocol_fingerprint() != manifest["protocol"]:
                raise ValueError("protocol_changed_during_evaluation")
            target = directory / f"trial-{trial}.private.json"
            code = replay(source, target, store, config, 6)
            runs["trials"].append(dict(trial=trial, exit_code=code))
            if protocol_fingerprint() != manifest["protocol"]:
                raise ValueError("protocol_changed_during_evaluation")
            write(directory / "run.private.json", runs)
        runs["completed"] = True
    finally:
        write(directory / "run.private.json", runs)
    return 0 if all(t["exit_code"] == 0 for t in runs["trials"]) else 1


def main():
    parser = argparse.ArgumentParser(description="固定协议新样本采集与对照；collect 独占 Compose 环境")
    parser.add_argument("--store", type=Path, default=ROOT / ".snapshots")
    commands = parser.add_subparsers(dest="command", required=True)
    collection = commands.add_parser("collect")
    collection.add_argument("--output", type=Path, required=True)
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--source", type=Path, required=True)
    evaluation.add_argument("--directory", type=Path, required=True)
    evaluation.add_argument("--repeats", type=int, choices=range(1, 6), default=2)
    args = parser.parse_args()
    store = SnapshotStore(args.store)
    if args.command == "collect":
        collect(args.output, store)
        return 0
    return evaluate(args.source, args.directory, store, ModelConfig.from_env(), args.repeats)


if __name__ == "__main__":
    raise SystemExit(main())
