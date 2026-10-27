"""python -m backend.investigation.window_cli --help"""

import argparse
import asyncio
from pathlib import Path

from .evidence_models import WindowQuery
from .snapshot_store import SnapshotStore
from .window_queries import WindowEvidenceTools
from .model import ModelConfig, ModelConfigError
from .async_model import AsyncWindowChatModel, AsyncHypothesisChatModel
from .async_investigation import InvestigationCancelled, investigate_window_async, investigate_hypotheses_async
from .window_baseline import investigate_window_fixed


async def run_async_agent(store, snapshot_id, config, max_tool_calls, timeout_seconds, *, strategy="agent",
                          max_total_tokens=None):
    """只在 CLI 边界将取消转成可落盘报告，库函数仍传播取消。"""
    adapters = {"agent": (AsyncWindowChatModel, investigate_window_async),
                "hypotheses": (AsyncHypothesisChatModel, investigate_hypotheses_async)}
    adapter, runner = adapters[strategy]
    try:
        async with adapter(config) as model:
            return await runner(store, snapshot_id, model, max_tool_calls, timeout_seconds=timeout_seconds,
                                max_total_tokens=max_total_tokens)
    except InvestigationCancelled as exc:
        return exc.report


def main():
    parser = argparse.ArgumentParser(description="查询、下钻或调查冻结窗口；agent/hypotheses 策略会调用模型")
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
    investigate.add_argument("--strategy", choices=("fixed", "agent", "hypotheses"), default="fixed")
    investigate.add_argument("--max-tool-calls", type=int, choices=range(1, 101), default=6)
    investigate.add_argument("--output", type=Path)
    investigate.add_argument("--timeout-seconds", type=float,
                             help="agent/hypotheses 调查总时限，默认120秒；到期取消异步模型等待")
    investigate.add_argument("--max-total-tokens", type=int,
                             help="可选累计token停止阈值；响应后计量，最后一次请求可能超额")
    args = parser.parse_args()
    if args.command == "investigate" and args.max_total_tokens is not None:
        if args.strategy == "fixed":
            parser.error("--max-total-tokens 仅支持 agent/hypotheses 策略")
        from .budget import ObservedTokenBudget
        try:
            ObservedTokenBudget(args.max_total_tokens)
        except ValueError as exc:
            parser.error(str(exc))
    if (args.command == "investigate" and args.timeout_seconds is not None
            and args.strategy == "fixed"):
        parser.error("--timeout-seconds 仅支持 agent/hypotheses 策略")
    if args.command == "investigate" and args.timeout_seconds is not None:
        from .budget import InvestigationBudget
        try:
            InvestigationBudget(args.timeout_seconds)
        except ValueError as exc:
            parser.error(str(exc))
    tools = WindowEvidenceTools(SnapshotStore(args.store))
    if args.command == "query":
        evidence = tools.query_window(WindowQuery(start_us=args.start_us, end_us=args.end_us, limit=args.limit))
    elif args.command == "trace":
        evidence = tools.get_snapshot_trace(args.snapshot_id, args.trace_id)
    else:
        if args.strategy in ("agent", "hypotheses"):
            try:
                config = ModelConfig.from_env()
            except ModelConfigError as exc:
                parser.error(str(exc))
            result = asyncio.run(run_async_agent(
                tools.store, args.snapshot_id, config, args.max_tool_calls,
                args.timeout_seconds if args.timeout_seconds is not None else 120,
                strategy=args.strategy, max_total_tokens=args.max_total_tokens))
        else:
            result = investigate_window_fixed(tools.store, args.snapshot_id, args.max_tool_calls)
        output = result.model_dump_json(indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output + "\n", encoding="utf-8")
        print(output)
        if result.termination == "cancelled":
            return 130
        return 0 if result.termination == "completed" else 1
    print(evidence.model_dump_json(indent=2))
    return 0 if evidence.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
