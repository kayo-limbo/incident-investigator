"""python -m backend.investigation.window_cli --help"""

import argparse
from pathlib import Path

from .evidence_models import WindowQuery
from .snapshot_store import SnapshotStore
from .window_queries import WindowEvidenceTools
from .model import ModelConfig, ModelConfigError
from .window_model import WindowChatModel
from .window_baseline import investigate_window_fixed
from .window_investigation import investigate_window_agent


def main():
    parser = argparse.ArgumentParser(description="查询、下钻或调查冻结窗口；agent 策略会调用模型")
    parser.add_argument("--store", type=Path, default=Path(".snapshots"))
    commands = parser.add_subparsers(dest="command", required=True)
    query = commands.add_parser("query")
    query.add_argument("--start-us", type=int, required=True)
    query.add_argument("--end-us", type=int, required=True)
    query.add_argument("--limit", type=int, default=100)
    trace = commands.add_parser("trace")
    trace.add_argument("snapshot_id")
    trace.add_argument("trace_id")
    investigate = commands.add_parser("investigate")
    investigate.add_argument("snapshot_id")
    investigate.add_argument("--strategy", choices=("fixed", "agent"), default="fixed")
    investigate.add_argument("--max-tool-calls", type=int, choices=range(1, 101), default=6)
    investigate.add_argument("--output", type=Path)
    args = parser.parse_args()
    tools = WindowEvidenceTools(SnapshotStore(args.store))
    if args.command == "query":
        evidence = tools.query_window(WindowQuery(start_us=args.start_us, end_us=args.end_us, limit=args.limit))
    elif args.command == "trace":
        evidence = tools.get_snapshot_trace(args.snapshot_id, args.trace_id)
    else:
        if args.strategy == "agent":
            try:
                config = ModelConfig.from_env()
            except ModelConfigError as exc:
                parser.error(str(exc))
            with WindowChatModel(config) as model:
                result = investigate_window_agent(tools.store, args.snapshot_id, model, args.max_tool_calls)
        else:
            result = investigate_window_fixed(tools.store, args.snapshot_id, args.max_tool_calls)
        output = result.model_dump_json(indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output + "\n", encoding="utf-8")
        print(output)
        return 0 if result.termination == "completed" else 1
    print(evidence.model_dump_json(indent=2))
    return 0 if evidence.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
