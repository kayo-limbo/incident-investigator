"""python -m backend.investigation.window_cli --help"""

import argparse
from pathlib import Path

from .evidence_models import WindowQuery
from .snapshot_store import SnapshotStore
from .window_queries import WindowEvidenceTools


def main():
    parser = argparse.ArgumentParser(description="查询订单窗口或从冻结快照下钻（不调用模型）")
    parser.add_argument("--store", type=Path, default=Path(".snapshots"))
    commands = parser.add_subparsers(dest="command", required=True)
    query = commands.add_parser("query")
    query.add_argument("--start-us", type=int, required=True)
    query.add_argument("--end-us", type=int, required=True)
    query.add_argument("--limit", type=int, default=100)
    trace = commands.add_parser("trace")
    trace.add_argument("snapshot_id")
    trace.add_argument("trace_id")
    args = parser.parse_args()
    tools = WindowEvidenceTools(SnapshotStore(args.store))
    if args.command == "query":
        evidence = tools.query_window(WindowQuery(start_us=args.start_us, end_us=args.end_us, limit=args.limit))
    else:
        evidence = tools.get_snapshot_trace(args.snapshot_id, args.trace_id)
    print(evidence.model_dump_json(indent=2))
    return 0 if evidence.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
