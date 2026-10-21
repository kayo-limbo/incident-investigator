"""同快照三策略回放；场景标签仅由独立评测读取，不进入调查上下文。"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from backend.investigation.hypothesis_agent import investigate_hypotheses
from backend.investigation.hypothesis_model import HypothesisChatModel
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_baseline import investigate_window_fixed
from backend.investigation.window_investigation import investigate_window_agent
from backend.investigation.window_model import WindowChatModel
from evals.run_window_comparison import score


def audit_history(report, store):
    """从报告独立检查版本、可见证据时序及查询对应关系；不评判语义。"""
    current = {}
    previous_turn = 0
    try:
        for revision in report.hypothesis_history:
            old = current.get(revision.id)
            if revision.version != (old.version + 1 if old else 1):
                return False
            if revision.expected_version != revision.version - 1 or revision.turn < previous_turn:
                return False
            if old and (old.statement != revision.statement or old.affected_services != revision.affected_services):
                return False
            expected_count = 1 + sum(t.turn < revision.turn and t.status == "completed" for t in report.query_tasks)
            if not 1 <= revision.evidence_count <= len(report.evidence) or revision.evidence_count != expected_count:
                return False
            seen = {e.data.get("trace_id") for e in report.evidence[:revision.evidence_count]
                    if e.tool == "get_snapshot_trace" and e.status == "ok"}
            for ref in revision.supporting_refs + revision.counter_refs:
                if ref.snapshot_id != report.snapshot_id or ref.trace_id not in seen:
                    return False
                store.resolve(ref)
            current[revision.id], previous_turn = revision, revision.turn
        if list(current.values()) != report.hypotheses:
            return False
        used_evidence = set()
        previous_query_turn = 0
        for task in report.query_tasks:
            if task.status != "completed" or task.turn <= previous_query_turn:
                return False
            previous_query_turn = task.turn
            matching = [e for e in report.evidence if e.id == task.evidence_id and e.tool == "get_snapshot_trace"
                        and e.status == "ok" and e.data.get("trace_id") == task.trace_id]
            if len(matching) != 1 or task.evidence_id in used_evidence or task.cost_tool_calls != 1:
                return False
            used_evidence.add(task.evidence_id)
            at_query = {h.id: h.version for h in report.hypothesis_history if h.turn <= task.turn}
            if not task.hypothesis_versions or any(at_query.get(i) != v for i, v in task.hypothesis_versions.items()):
                return False
        if len(used_evidence) != report.usage_totals["drilldown_calls"]:
            return False
        selected = report.claim_hypothesis_versions
        if not all(i in current and current[i].version == v for i, v in selected.items()):
            return False
        if report.finding.code != "insufficient_evidence":
            if not selected or any(current[i].status not in ("supported", "refuted") for i in selected):
                return False
            if report.finding.code != "no_anomaly_observed" and not any(current[i].status == "supported" for i in selected):
                return False
            def keys(refs):
                return {(r.snapshot_id, r.trace_id, r.span_id, r.field) for r in refs}
            finding_refs, eligible = keys(report.finding.references), set()
            for i in selected:
                h = current[i]
                required = h.supporting_refs if h.status == "supported" else h.counter_refs
                if not finding_refs & keys(required):
                    return False
                eligible |= keys(h.supporting_refs + h.counter_refs)
            if not finding_refs <= eligible:
                return False
        return True
    except (ValueError, KeyError, OSError):
        return False


def replay(source, output_path, store, config, max_tool_calls=6):
    if source.resolve() == output_path.resolve() or output_path.exists():
        raise ValueError("输出文件必须是新文件，保留既有实验结果")
    cases = json.loads(source.read_text(encoding="utf-8"))["cases"]
    output = {"recorded_at": datetime.now(timezone.utc).isoformat(), "mode": "snapshot_replay",
              "environment_modified": False, "model": config.model,
              "max_tool_calls": max_tool_calls, "max_output_tokens": config.max_output_tokens,
              "timeout_seconds": config.timeout_seconds, "cases": [], "completed": False}
    try:
        for old in cases:
            key, expected = old["snapshot_id"], old["expected"]
            case = {"name": old["name"], "snapshot_id": key, "expected": expected, "reports": {}, "passed": {}}
            output["cases"].append(case)
            fixed = investigate_window_fixed(store, key, max_tool_calls)
            case["reports"]["fixed"] = fixed.model_dump(mode="json")
            case["passed"]["fixed"] = score(fixed, expected, key, store)
            for name, adapter, runner in (("agent", WindowChatModel, investigate_window_agent),
                                          ("hypotheses", HypothesisChatModel, investigate_hypotheses)):
                with adapter(config) as model:
                    report = runner(store, key, model, max_tool_calls)
                same_input = report.evidence[0].data == fixed.evidence[0].data
                case["reports"][name] = report.model_dump(mode="json")
                case["passed"][name] = same_input and score(report, expected, key, store)
                if name == "hypotheses":
                    case["history_audit"] = audit_history(report, store)
                    case["passed"][name] &= case["history_audit"]
                print(f"{old['name']} {name}: passed={case['passed'][name]} termination={report.termination}", flush=True)
                # 每个策略结束即保存，便于中断后复盘；不自动重试失败样本。
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output["completed"] = True
    finally:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if cases and all(all(c["passed"].values()) for c in output["cases"]) else 1


def main():
    parser = argparse.ArgumentParser(description="既有快照三策略真实模型回放，不操作容器")
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--store", type=Path, default=Path(".snapshots"))
    parser.add_argument("--output", type=Path, default=Path("docs/experiments/hypothesis-comparison.private.json"))
    parser.add_argument("--max-tool-calls", type=int, choices=range(1, 101), default=6)
    args = parser.parse_args()
    return replay(args.replay, args.output, SnapshotStore(args.store), ModelConfig.from_env(), args.max_tool_calls)


if __name__ == "__main__":
    raise SystemExit(main())
