"""Single-snapshot query policy entry; fixed is offline."""
import argparse
import asyncio
from pathlib import Path
import json

from .active_policies import ReactQueryModel, ActivePlanModel, PolicyCancelled, investigate_policy
from .model import ModelConfig
from .snapshot_store import SnapshotStore
from .failure_evidence import investigate_503


async def run(args):
    store = SnapshotStore(args.store)
    if args.strategy == 'failure-503':
        return investigate_503(store, args.snapshot_id, budget=args.budget)
    options = dict(strategy=args.strategy, budget=args.budget, timeout_seconds=args.timeout_seconds,
                   max_total_tokens=args.max_total_tokens, scoring=not args.no_scoring)
    if args.strategy == 'fixed':
        return await investigate_policy(store, args.snapshot_id, **options)
    adapter = ReactQueryModel if args.strategy == 'react' else ActivePlanModel
    try:
        async with adapter(ModelConfig.from_env()) as model:
            return await investigate_policy(store, args.snapshot_id, model, **options)
    except PolicyCancelled as exc:
        return exc.report


def main(argv=None):
    parser = argparse.ArgumentParser(description='同协议查询策略；react/active会外发观测并调用收费模型')
    parser.add_argument('snapshot_id')
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--strategy', choices=('fixed', 'react', 'active', 'failure-503'), default='fixed')
    parser.add_argument('--budget', type=int, choices=range(1, 101), default=6)
    parser.add_argument('--timeout-seconds', type=float, default=120)
    parser.add_argument('--max-total-tokens', type=int, default=40000)
    parser.add_argument('--no-scoring', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.no_scoring and args.strategy != 'active':
        parser.error('--no-scoring只用于active')
    from .budget import InvestigationBudget, ObservedTokenBudget
    InvestigationBudget(args.timeout_seconds)
    ObservedTokenBudget(args.max_total_tokens)
    with args.output.open('x', encoding='utf-8') as output:
        result = asyncio.run(run(args))
        json.dump(result, output, ensure_ascii=False, indent=2)
    if args.strategy == 'failure-503':
        return 0 if result['code'] == 'inspect_orders_connection_pool' else 1
    return 0 if result['termination'] == 'completed' else 130 if result['termination'] == 'cancelled' else 1


if __name__ == '__main__':
    raise SystemExit(main())
