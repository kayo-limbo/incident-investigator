"""Read-only development replay, not a new collection audit or held-out score."""
import argparse
import hashlib
import json
from pathlib import Path

from backend.investigation.paired_baseline import investigate_paired_fixed
from backend.investigation.snapshot_store import SnapshotStore


def replay(root):
    manifest = root / "collection.private.json"
    content = manifest.read_bytes()
    store = SnapshotStore(root / "snapshots")
    reports = []
    for case in json.loads(content)["cases"]:
        if not case.get("valid"):
            continue
        for view, key in {"complete": case["snapshot_id"], **case.get("source_ablations", {})}.items():
            # Labels stay in the evaluator; the investigator gets only the hash-addressed evidence.
            report = investigate_paired_fixed(store, key)
            reports.append(dict(case=case["name"], repeat=case["repeat"], view=view,
                                report=report.model_dump(mode="json")))
    source = Path(__file__).resolve().parents[1]
    fingerprints = {str(p.relative_to(source)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((source / "backend" / "investigation").glob("*.py"))}
    return dict(protocol="paired-fixed-development-replay-v1", manifest_sha256=hashlib.sha256(content).hexdigest(),
                source_sha256=fingerprints, reports=reports,
                limitations=["既有开发样本回放；不是独立测试或模型效果成绩，也不替代原采集audit。"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(replay(args.root), output, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
