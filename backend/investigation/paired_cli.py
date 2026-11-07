"""Run a single frozen paired-v2 investigation; fixed is offline by default."""
import argparse
import asyncio
from pathlib import Path

from .async_investigation import InvestigationCancelled
from .model import ModelConfig
from .paired_agent import PairedChatModel, PairedHypothesisModel, investigate_paired
from .paired_baseline import investigate_paired_fixed
from .snapshot_store import SnapshotStore


async def run_model(args, store):
    adapter = PairedChatModel if args.strategy == "agent" else PairedHypothesisModel
    try:
        async with adapter(ModelConfig.from_env()) as model:
            return await investigate_paired(store, args.snapshot_id, model, args.strategy,
                max_tool_calls=args.max_tool_calls, timeout_seconds=args.timeout_seconds,
                max_total_tokens=args.max_total_tokens)
    except InvestigationCancelled as exc:
        return exc.report


def main(argv=None):
    parser = argparse.ArgumentParser(description="双窗口调查；agent/hypotheses会发送观测并产生模型费用")
    parser.add_argument("snapshot_id")
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--strategy", choices=("fixed", "agent", "hypotheses"), default="fixed")
    parser.add_argument("--max-tool-calls", type=int, choices=range(1, 101), default=6)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--max-total-tokens", type=int, default=40000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    from .budget import InvestigationBudget, ObservedTokenBudget
    InvestigationBudget(args.timeout_seconds)
    ObservedTokenBudget(args.max_total_tokens)
    # Reserve the destination before any paid call, never overwrite an earlier result.
    output = args.output.open("x", encoding="utf-8") if args.output else None
    try:
        store = SnapshotStore(args.store)
        report = (investigate_paired_fixed(store, args.snapshot_id, args.max_tool_calls)
                  if args.strategy == "fixed" else asyncio.run(run_model(args, store)))
        value = report.model_dump_json(indent=2)
        if output:
            output.write(value + "\n")
        print(value)
        return 130 if report.termination == "cancelled" else int(report.termination != "completed")
    finally:
        if output:
            output.close()


if __name__ == "__main__":
    raise SystemExit(main())
