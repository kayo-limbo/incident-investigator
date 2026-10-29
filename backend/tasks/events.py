"""有界SSE连接；每批数据库读取完成后释放事务，不持有订阅连接。"""

import asyncio
import json
from time import monotonic

import psycopg
from starlette.concurrency import run_in_threadpool

from .store import TaskNotFound, EventCursorError


def encode_event(event):
    return f"id: {event['sequence']}\nevent: task_state\ndata: {json.dumps(event, default=str)}\n\n"


async def stream_events(store, task_id, after, initial, *, poll_seconds=1, duration_seconds=30):
    deadline = monotonic() + duration_seconds
    batch = initial
    yield "retry: 1000\n\n"
    while True:
        for event in batch["events"]:
            yield encode_event(event)
            after = event["sequence"]
            if event["state"] in ("completed", "failed", "cancelled"):
                return
        if not batch["events"] and batch["terminal"]:
            return
        if monotonic() >= deadline:
            return
        # 满批立即继续补读，避免历史积压每批都等待。
        if len(batch["events"]) < 100:
            yield ": keepalive\n\n"
            await asyncio.sleep(poll_seconds)
        try:
            batch = await run_in_threadpool(store.read_events, task_id, after)
        except (psycopg.Error, TaskNotFound, EventCursorError):
            # 响应头已发出，不能再转为HTTP错误；无id，客户端保留最后成功游标。
            yield 'event: stream_error\ndata: {"code":"event_stream_unavailable"}\n\n'
            return
