"""Offline active reference policy; no API or heldout access."""
import argparse
import json
from pathlib import Path
from .replay_paired_fixed import replay
from backend.investigation.active_investigation import run_reference
from backend.investigation.snapshot_store import SnapshotStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--budget', type=int, default=6)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    # Reuse development gate and input/source fingerprints, not baseline findings as input.
    baseline = replay(args.root)
    store = SnapshotStore(args.root / 'snapshots')
    result = dict(protocol='active-reference-development-v1', manifest_sha256=baseline['manifest_sha256'],
                  source_sha256=baseline['source_sha256'], reports=[])
    for item in baseline['reports']:
        result['reports'].append(dict(case=item['case'], repeat=item['repeat'], view=item['view'],
            report=run_reference(store, item['report']['snapshot_id'], args.budget)))
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
