"""独立四场景验收；同一冻结快照分别交给规则和 Agent，真值不进入策略。"""

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from backend.investigation.evidence_models import WindowQuery
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_baseline import investigate_window_fixed
from backend.investigation.window_investigation import investigate_window_agent
from backend.investigation.window_model import WindowChatModel
from backend.investigation.window_queries import WindowEvidenceTools
from lab.run_delay_experiment import ROOT, compose
from lab.run_pool_experiment import load
from lab.run_trace_experiment import wait_for_trace


def score(report, expected, snapshot_id, store):
    if report.termination != "completed" or report.finding.code != expected or report.snapshot_id != snapshot_id:
        return False
    if expected != "insufficient_evidence" and not report.finding.references:
        return False
    seen = {e.data.get("trace_id") for e in report.evidence if e.tool == "get_snapshot_trace" and e.status == "ok"}
    keys = set()
    try:
        for ref in report.finding.references:
            key = (ref.snapshot_id, ref.trace_id, ref.span_id, ref.field)
            if ref.snapshot_id != snapshot_id or ref.trace_id not in seen or key in keys:
                return False
            keys.add(key)
            store.resolve(ref)
    except (ValueError, KeyError, OSError):
        return False
    return True


def replay(source, output_path, store, config, max_tool_calls):
    """只从既有验收记录取快照 ID 与评测标签；不把记录本身传给策略。"""
    previous = json.loads(source.read_text(encoding="utf-8"))
    output = {"recorded_at": datetime.now(timezone.utc).isoformat(), "mode": "snapshot_replay",
              "source": source.name, "environment_modified": False,
              "model": config.model if config else None, "max_tool_calls": max_tool_calls,
              "cases": [], "completed": False}
    try:
        for old in previous["cases"]:
            name, key, expected = old["name"], old["snapshot_id"], old["expected"]
            fixed = investigate_window_fixed(store, key, max_tool_calls)
            case = {"name": name, "snapshot_id": key, "expected": expected,
                    "reports": {"fixed": fixed.model_dump(mode="json")},
                    "passed": {"fixed": score(fixed, expected, key, store)}}
            output["cases"].append(case)
            if config:
                with WindowChatModel(config) as model:
                    agent = investigate_window_agent(store, key, model, max_tool_calls)
                case["reports"]["agent"] = agent.model_dump(mode="json")
                case["passed"]["agent"] = score(agent, expected, key, store)
                assert agent.evidence[0].data == fixed.evidence[0].data, "unequal_input"
            print(f"{name}: {case['passed']}", flush=True)
        output["completed"] = True
    finally:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if output["cases"] and all(all(c["passed"].values()) for c in output["cases"]) else 1


def main():
    parser = argparse.ArgumentParser(description="四场景冻结证据对照；独占本地 Compose 环境")
    parser.add_argument("--strategy", choices=("fixed", "both"), default="fixed")
    parser.add_argument("--max-tool-calls", type=int, choices=range(1, 101), default=6)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/experiments/window-comparison.private.json")
    parser.add_argument("--replay", type=Path, help="从既有对照记录读取快照 ID；不操作容器")
    args = parser.parse_args()
    # 在修改环境前验证模型配置；绝不输出密钥或完整配置。
    config = ModelConfig.from_env() if args.strategy == "both" else None
    store = SnapshotStore(ROOT / ".snapshots")
    if args.replay:
        if args.replay.resolve() == args.output.resolve():
            parser.error("回放输出必须不同于原始记录，保留首次结果")
        return replay(args.replay, args.output, store, config, args.max_tool_calls)
    tools = WindowEvidenceTools(store)
    orders = f"http://127.0.0.1:{os.getenv('ORDERS_PORT', '8000')}/orders/preview?sku=demo"
    jaeger = f"http://127.0.0.1:{os.getenv('JAEGER_PORT', '16686')}"
    output = {"recorded_at": datetime.now(timezone.utc).isoformat(), "cases": [],
              "model": config.model if config else None, "max_tool_calls": args.max_tool_calls,
              "completed": False, "recovery_verified": False}
    cases = (("healthy", 80, 8, "no_anomaly_observed"),
             ("pool_wait", 80, 1, "pre_send_delay"),
             ("downstream_slow", 500, 8, "inventory_latency"),
             ("slow_and_pool_timeout", 500, 4, "inventory_latency_with_pool_contention"))
    try:
        for name, delay, connections, expected in cases:
            compose("up", "-d", "--wait", "--wait-timeout", "60", delay=delay,
                    max_connections=connections, pool_timeout_ms=250)
            start = time.time_ns() // 1000
            samples = asyncio.run(load(orders, concurrency=8))
            end = time.time_ns() // 1000
            with httpx.Client(timeout=5, trust_env=False) as client:
                for sample in samples:
                    wait_for_trace(client, jaeger, sample["trace_id"], 3 if sample["status"] == 200 else 2)
            evidence = tools.query_window(WindowQuery(start_us=start, end_us=end))
            assert evidence.status == "ok" and evidence.data["quality"] == "observed"
            key = evidence.data["snapshot_id"]
            assert {s["trace_id"] for s in samples} <= set(evidence.data["summary"]["trace_ids"])
            assert evidence.data["summary"]["request_count"] == 25
            if name == "slow_and_pool_timeout":
                assert any(s["status"] == 503 for s in samples), "fault_not_observed"
            else:
                assert all(s["status"] == 200 for s in samples), "unexpected_business_failure"
            case = {"name": name, "expected": expected, "snapshot_id": key,
                    "collection_usage": evidence.data["usage"], "reports": {}, "passed": {}}
            output["cases"].append(case)
            fixed = investigate_window_fixed(store, key, args.max_tool_calls)
            case["reports"]["fixed"] = fixed.model_dump(mode="json")
            case["passed"]["fixed"] = score(fixed, expected, key, store)
            print(f"{name} fixed: {fixed.finding.code}, {fixed.termination}, passed={case['passed']['fixed']}", flush=True)
            if config:
                with WindowChatModel(config) as model:
                    agent = investigate_window_agent(store, key, model, args.max_tool_calls)
                case["reports"]["agent"] = agent.model_dump(mode="json")
                case["passed"]["agent"] = score(agent, expected, key, store)
                assert agent.evidence[0].data == fixed.evidence[0].data, "unequal_input"
                print(f"{name} agent: {agent.finding.code}, {agent.termination}, passed={case['passed']['agent']}", flush=True)
        output["completed"] = True
    finally:
        try:
            compose("up", "-d", "--wait", "--wait-timeout", "60", delay=0)
            with httpx.Client(timeout=5, trust_env=False) as client:
                response = client.get(orders)
                output["recovery_verified"] = response.status_code == 200 and response.json() == {
                    "sku": "demo", "can_order": True}
                assert output["recovery_verified"]
        finally:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    passed = all(all(case["passed"].values()) for case in output["cases"])
    print(f"Completed={output['completed']}; passed={passed}; recovery={output['recovery_verified']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
