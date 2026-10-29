"""单槽常驻消费者：有限基础设施重试、可唤醒等待与排空退出。"""

import asyncio
import json
import math
import random
import signal

import psycopg

from .store import TaskConflict, validate_label, validate_lease
from .worker import run_once


def emit_event(event):
    print(json.dumps(event), flush=True)


async def wait_or_stop(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass


async def run_forever(store, snapshots, worker_id, *, stop, lease_seconds=30,
                      poll_seconds=1, max_errors=5, emit=emit_event):
    validate_label(worker_id)
    validate_lease(lease_seconds)
    if type(poll_seconds) not in (int, float) or not math.isfinite(poll_seconds) or not 0.05 <= poll_seconds <= 60:
        raise ValueError("poll_seconds_must_be_0.05_to_60")
    if type(max_errors) is not int or not 1 <= max_errors <= 100:
        raise ValueError("max_errors_must_be_integer_1_to_100")
    errors = 0
    processed = 0
    emit({"event": "worker_started", "worker_id": worker_id})
    while not stop.is_set():
        try:
            task = await run_once(store, snapshots, worker_id, lease_seconds)
        except (psycopg.OperationalError, psycopg.InterfaceError, TaskConflict) as exc:
            # 不按原任务直接重试；下轮仍通过回收、领取和新令牌裁决所有权。
            errors += 1
            code = "lease_conflict" if isinstance(exc, TaskConflict) else "database_unavailable"
            if errors >= max_errors:
                emit({"event": "worker_stopped", "reason": "error_limit", "error_code": code,
                      "consecutive_errors": errors, "processed": processed})
                return 1
            delay = random.uniform(0.5, 1) * min(30, 2 ** min(errors - 1, 5))
            emit({"event": "worker_retry", "error_code": code,
                  "consecutive_errors": errors, "delay_seconds": delay})
            await wait_or_stop(stop, delay)
            continue
        errors = 0
        if task is None:
            await wait_or_stop(stop, poll_seconds)
        else:
            processed += 1
            emit({"event": "task_finished", "task_id": str(task.id), "state": task.state,
                  "attempt_count": task.attempt_count, "error_code": task.error_code})
            # 同步fixed执行可能没有让出循环，必须允许已排队的停止信号先运行。
            await asyncio.sleep(0)
    emit({"event": "worker_stopped", "reason": "stop_requested", "processed": processed})
    return 0


async def run_service(store, snapshots, worker_id, **options):
    """仅供主线程CLI使用；第一次停止信号停止新领取，当前调查继续至终态。"""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    previous = {}

    def request_stop(signum, frame):
        loop.call_soon_threadsafe(stop.set)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        return await run_forever(store, snapshots, worker_id, stop=stop, **options)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
