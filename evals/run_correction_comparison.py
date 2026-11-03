"""预注册新快照的异步图纠错开关对照；不向调查输入评测标签。"""

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.investigation.async_investigation import investigate_window_async, investigate_hypotheses_async
from backend.investigation.async_model import AsyncWindowChatModel, AsyncHypothesisChatModel
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_baseline import investigate_window_fixed
from evals.run_holdout import collect, check_observations, protocol_fingerprint, write
from evals.run_hypothesis_comparison import audit_history
from evals.run_window_comparison import score
from lab.run_delay_experiment import ROOT


PLAN = (
    dict(name="low_load_55", delay=55, connections=1, concurrency=1, timeout=900, expected="no_anomaly_observed"),
    dict(name="queue_75", delay=75, connections=1, concurrency=6, timeout=1100, expected="pre_send_delay"),
    dict(name="inventory_420", delay=420, connections=6, concurrency=6, timeout=900, expected="inventory_latency"),
    dict(name="pool_110", delay=110, connections=1, concurrency=6, timeout=45, expected="pool_contention"),
    dict(name="combined_420", delay=420, connections=2, concurrency=6, timeout=120, expected="inventory_latency_with_pool_contention"),
)
BUDGET = dict(max_tool_calls=6, timeout_seconds=120, max_total_tokens=40000, repeat_query_policy="stop")
REPEATS = 2


def fingerprint():
    result = protocol_fingerprint()
    paths = [Path(__file__), ROOT / "compose.yaml"]
    paths += sorted((ROOT / "lab").rglob("*.py"))
    paths += sorted(ROOT.glob("requirements*.txt"))
    for path in paths:
        result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def model_settings(config):
    return {key: getattr(config, key) for key in ("base_url", "model", "timeout_seconds", "max_output_tokens")}


def schedule():
    # 每个配对在两轮中反转先后；策略顺序也反转，不按结果选择重试。
    return [dict(case=i, repeat=r + 1, strategy=strategy, max_corrections=enabled)
            for r in range(REPEATS) for i in range(len(PLAN))
            for strategy in (("agent", "hypotheses") if r == 0 else ("hypotheses", "agent"))
            for enabled in ((0, 1) if (r + i) % 2 == 0 else (1, 0))]


def freeze(directory, config):
    directory.mkdir(parents=True, exist_ok=False)
    plan = dict(recorded_at=datetime.now(timezone.utc).isoformat(), protocol=fingerprint(),
                plan=list(PLAN), budget=BUDGET, model=model_settings(config), schedule=schedule())
    write(directory / "plan.private.json", plan)
    return plan


def validate_plan(directory, config):
    plan = json.loads((directory / "plan.private.json").read_text(encoding="utf-8"))
    if (plan["protocol"] != fingerprint() or plan["plan"] != list(PLAN)
            or plan["budget"] != BUDGET or plan["schedule"] != schedule()
            or plan["model"] != model_settings(config)):
        raise ValueError("frozen_protocol_changed")
    return plan


async def run_one(store, key, strategy, enabled, config):
    adapter, investigate = ((AsyncWindowChatModel, investigate_window_async) if strategy == "agent"
                            else (AsyncHypothesisChatModel, investigate_hypotheses_async))
    async with adapter(config) as model:
        return await investigate(store, key, model, max_corrections=enabled, **BUDGET)


async def evaluate(directory, store, config):
    plan = validate_plan(directory, config)
    manifest = json.loads((directory / "collection.private.json").read_text(encoding="utf-8"))
    if (not manifest.get("completed") or not manifest.get("recovery_verified")
            or manifest["protocol"] != plan["protocol"] or manifest["plan"] != list(PLAN)
            or len(manifest["cases"]) != len(PLAN)):
        raise ValueError("invalid_collection")
    for spec, case in zip(PLAN, manifest["cases"]):
        if not case.get("collected") or (case["name"], case["expected"]) != (spec["name"], spec["expected"]):
            raise ValueError("invalid_case")
        check_observations(spec, case["business_summary"])
        store.read(case["snapshot_id"])
    # 原子占用目录，失败或中断后不能以同一目录重跑覆盖。
    reports = directory / "reports"
    reports.mkdir(exist_ok=False)
    result = dict(completed=False, baselines=[], runs=[], error=None)
    target = directory / "result.private.json"
    write(target, result)
    try:
        for case in manifest["cases"]:
            report = investigate_window_fixed(store, case["snapshot_id"], BUDGET["max_tool_calls"])
            result["baselines"].append(dict(report=report.model_dump(mode="json"),
                passed=score(report, case["expected"], case["snapshot_id"], store)))
        write(target, result)
        for index, item in enumerate(plan["schedule"]):
            validate_plan(directory, config)
            case = manifest["cases"][item["case"]]
            entry = dict(item, status="started", report_file=f"{index:02}.private.json")
            result["runs"].append(entry)
            write(target, result)  # 未知调用保持 started，不自动重发。
            report = await run_one(store, case["snapshot_id"], item["strategy"], item["max_corrections"], config)
            write(reports / entry["report_file"], report.model_dump(mode="json"))
            same_input = report.evidence[0].data == result["baselines"][item["case"]]["report"]["evidence"][0]["data"]
            history_ok = item["strategy"] != "hypotheses" or audit_history(report, store)
            entry.update(status="finished", passed=same_input and history_ok and score(report, case["expected"], case["snapshot_id"], store),
                         same_input=same_input, history_ok=history_ok, termination=report.termination,
                         diagnostic=report.diagnostic.model_dump(mode="json") if report.diagnostic else None,
                         correction=report.correction.model_dump(mode="json") if report.correction else None,
                         usage=report.usage_totals)
            write(target, result)
            print(f"{index + 1}/40 {case['name']} {item['strategy']} correction={item['max_corrections']} {report.termination} passed={entry['passed']}", flush=True)
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
    parser.add_argument("command", choices=("freeze", "collect", "evaluate"))
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--store", default=ROOT / ".snapshots", type=Path)
    args = parser.parse_args()
    config = ModelConfig.from_env()
    if args.command == "freeze":
        freeze(args.directory, config)
    elif args.command == "collect":
        validate_plan(args.directory, config)
        collect(args.directory / "collection.private.json", SnapshotStore(args.store), plan=PLAN, fingerprint_fn=fingerprint)
    else:
        asyncio.run(evaluate(args.directory, SnapshotStore(args.store), config))


if __name__ == "__main__":
    main()
