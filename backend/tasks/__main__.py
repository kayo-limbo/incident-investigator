"""python -m backend.tasks --help"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import psycopg
from pydantic import ValidationError

from backend.investigation.snapshot_store import SnapshotStore
from .models import TaskRequest
from .store import TaskStore, TaskConflict, TaskNotFound
from .worker import run_once, WorkerCancelled
from .daemon import run_service


async def run_cli(store, snapshots, worker_id, lease_seconds=30):
    try:
        return await run_once(store, snapshots, worker_id, lease_seconds)
    except WorkerCancelled as exc:
        return exc.task


def main(argv=None):
    parser = argparse.ArgumentParser(description="持久化调查任务；模型策略会发送冻结证据到模型服务")
    parser.add_argument("--snapshots", type=Path, default=Path(".snapshots"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("recover")
    submit = commands.add_parser("submit")
    submit.add_argument("snapshot_id")
    submit.add_argument("--idempotency-key", required=True)
    submit.add_argument("--strategy", choices=("fixed", "agent", "hypotheses"), default="fixed")
    submit.add_argument("--max-tool-calls", type=int, default=6)
    submit.add_argument("--timeout-seconds", type=float, default=120)
    submit.add_argument("--max-total-tokens", type=int)
    submit.add_argument("--repeat-query-policy", choices=("stop", "allow"), default="stop")
    for command in ("get", "cancel"):
        commands.add_parser(command).add_argument("task_id", type=UUID)
    work = commands.add_parser("work-once")
    work.add_argument("--worker-id", required=True)
    work.add_argument("--lease-seconds", type=int, default=30)
    daemon = commands.add_parser("work", help="持续消费任务；停止信号等待当前调查结束")
    daemon.add_argument("--worker-id", required=True)
    daemon.add_argument("--lease-seconds", type=int, default=30)
    daemon.add_argument("--poll-seconds", type=float, default=1)
    daemon.add_argument("--max-errors", type=int, default=5)
    args = parser.parse_args(argv)
    dsn = os.environ.get("TASK_DATABASE_URL")
    if not dsn:
        print(json.dumps({"error": "task_database_url_missing"}))
        return 2
    store = TaskStore(dsn)
    try:
        if args.command == "work":
            return asyncio.run(run_service(store, SnapshotStore(args.snapshots), args.worker_id,
                lease_seconds=args.lease_seconds, poll_seconds=args.poll_seconds, max_errors=args.max_errors))
        if args.command == "init":
            store.initialize()
            print('{"initialized": true}')
            return 0
        if args.command == "recover":
            tasks = store.recover_expired()
            print(json.dumps({"recovered": [str(task.id) for task in tasks]}))
            return 0
        if args.command == "submit":
            request = TaskRequest(**{name: getattr(args, name) for name in TaskRequest.model_fields})
            SnapshotStore(args.snapshots).read(request.snapshot_id)
            task = store.submit(request, args.idempotency_key)
        elif args.command == "get":
            task = store.get(args.task_id)
        elif args.command == "cancel":
            task = store.cancel_queued(args.task_id)
        else:
            task = asyncio.run(run_cli(store, SnapshotStore(args.snapshots), args.worker_id, args.lease_seconds))
        print(task.model_dump_json(indent=2) if task else '{"idle": true}')
        if args.command == "work-once" and task:
            return 130 if task.state == "cancelled" else (1 if task.state == "failed" else 0)
        return 0
    except (TaskConflict, TaskNotFound) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    except psycopg.Error:
        print('{"error": "database_error"}')
        return 1
    except (ValidationError, ValueError, OSError):
        print('{"error": "invalid_request_or_snapshot"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
