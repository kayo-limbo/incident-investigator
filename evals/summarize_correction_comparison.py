"""离线重算纠错对照评分和用量；不调用模型，不修改原始结果。"""

import argparse
import json
from collections import Counter
from pathlib import Path

from backend.investigation.hypothesis_agent import HypothesisReport
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_investigation import WindowReport
from evals.run_hypothesis_comparison import audit_history
from evals.run_window_comparison import score
from lab.run_delay_experiment import ROOT


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(directory, store):
    plan = read(directory / "plan.private.json")
    collection = read(directory / "collection.private.json")
    result = read(directory / "result.private.json")
    if (not result["completed"] or not collection["completed"] or not collection["recovery_verified"]
            or len(result["runs"]) != len(plan["schedule"])):
        raise ValueError("incomplete_experiment")
    groups, paired = {}, {}
    baseline_passes = 0
    for case, saved in zip(collection["cases"], result["baselines"], strict=True):
        report = WindowReport.model_validate(saved["report"])
        passed = score(report, case["expected"], case["snapshot_id"], store)
        if passed != saved["passed"]:
            raise ValueError("baseline_score_mismatch")
        baseline_passes += passed
    for item, entry in zip(plan["schedule"], result["runs"], strict=True):
        if entry["status"] != "finished" or any(entry[key] != value for key, value in item.items()):
            raise ValueError("schedule_mismatch")
        case = collection["cases"][item["case"]]
        report_type = WindowReport if item["strategy"] == "agent" else HypothesisReport
        report = report_type.model_validate(read(directory / "reports" / entry["report_file"]))
        history_ok = item["strategy"] != "hypotheses" or audit_history(report, store)
        same_input = report.evidence[0].data == result["baselines"][item["case"]]["report"]["evidence"][0]["data"]
        passed = same_input and history_ok and score(report, case["expected"], case["snapshot_id"], store)
        if (passed != entry["passed"] or history_ok != entry["history_ok"] or same_input != entry["same_input"]
                or report.usage_totals != report.usage.totals() or report.usage_totals != entry["usage"]):
            raise ValueError("report_score_or_usage_mismatch")
        name = f"{item['strategy']}_correction_{item['max_corrections']}"
        group = groups.setdefault(name, dict(runs=0, correct=0, completed=0, corrections_used=0,
            corrected_completed=0, corrected_correct=0, model_calls=0, tool_calls=0,
            known_total_tokens=0, unknown_usage_calls=0, elapsed_ms=0,
            terminations=Counter(), diagnostics=Counter(), correction_failures=Counter()))
        group["runs"] += 1
        group["correct"] += passed
        group["completed"] += report.termination == "completed"
        group["terminations"][report.termination] += 1
        if report.diagnostic:
            group["diagnostics"][report.diagnostic.code] += 1
        used = bool(report.correction and report.correction.used)
        group["corrections_used"] += used
        group["corrected_completed"] += used and report.termination == "completed"
        group["corrected_correct"] += used and passed
        if report.correction:
            for failure in report.correction.failures:
                group["correction_failures"][failure.diagnostic.code] += 1
        for key in ("model_calls", "tool_calls", "unknown_usage_calls", "elapsed_ms"):
            group[key] += report.usage_totals[key]
        group["known_total_tokens"] += sum(call.total_tokens or 0 for call in report.usage.calls if call.kind == "model")
        paired.setdefault((item["strategy"], item["case"], item["repeat"]), {})[item["max_corrections"]] = passed
    pairs = {}
    for (strategy, _, _), values in paired.items():
        if set(values) != {0, 1}:
            raise ValueError("missing_pair")
        label = ("both_correct" if values[0] and values[1] else "both_failed" if not values[0] and not values[1]
                 else "on_only_correct" if values[1] else "off_only_correct")
        pairs.setdefault(strategy, Counter())[label] += 1
    return dict(distinct_snapshots=len({c["snapshot_id"] for c in collection["cases"]}),
                baseline_correct=baseline_passes, groups=groups, pairs=pairs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--store", type=Path, default=ROOT / ".snapshots")
    args = parser.parse_args()
    print(json.dumps(summarize(args.directory, SnapshotStore(args.store)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
