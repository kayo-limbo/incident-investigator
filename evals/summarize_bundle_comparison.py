"""Recompute bundle label/protocol results offline; never call the model."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from backend.investigation.bundle_agent import BundleReport
from backend.investigation.snapshot_store import SnapshotStore
from evals.run_bundle_comparison import validate_collection, score


def summarize(directory, store, *, fixed_only=False):
    manifest = validate_collection(directory, store)
    cases = {c["name"]: c for c in manifest["cases"]}
    root = directory / ("fixed-reports" if fixed_only else "reports")
    result = json.loads((root / "result.private.json").read_text(encoding="utf-8"))
    groups = defaultdict(list)
    for entry in result["runs"]:
        if entry["status"] != "finished":
            raise ValueError("unfinished_call_do_not_retry")
        case = cases[entry["case"]]
        name = entry.get("report_file")
        if name is None:
            name = f"fixed-{manifest['cases'].index(case)}.private.json"
        report = BundleReport.model_validate_json((root / name).read_text(encoding="utf-8"))
        passed = score(report, case, store)
        usage = report.usage.totals()
        if passed != entry["passed"] or usage != entry["usage"] or report.termination != entry["termination"]:
            raise ValueError("saved_result_mismatch")
        groups[entry["strategy"]].append((passed, report))
    summary = {}
    for strategy, runs in groups.items():
        reports = [r for _, r in runs]
        totals = [r.usage.totals() for r in reports]
        unknown = sum(t["unknown_usage_calls"] for t in totals)
        summary[strategy] = dict(
            trials=len(runs), label_protocol_passed=sum(p for p, _ in runs),
            terminations=dict(Counter(r.termination for r in reports)),
            model_calls=sum(t["model_calls"] for t in totals),
            tool_calls=sum(t["tool_calls"] for t in totals),
            total_tokens=None if unknown else sum(t["total_tokens"] for t in totals),
            unknown_usage_calls=unknown,
            runtime_queries=sum(any(e.tool == "client_runtime" for e in r.evidence) for r in reports),
            elapsed_ms=sum(t["elapsed_ms"] for t in totals),
        )
    return dict(completed=result["completed"], groups=summary,
                semantic_review="Separate manual review required; label acceptance is not causal correctness.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--store", default=Path(".snapshots"), type=Path)
    parser.add_argument("--fixed-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(summarize(args.directory, SnapshotStore(args.store), fixed_only=args.fixed_only),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
