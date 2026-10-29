"""本地调查任务 API；HTTP 只持久化任务，独立 Worker 执行调查。"""

import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from backend.investigation.snapshot_store import SnapshotStore
from .models import TaskRecord, TaskRequest
from .store import TaskConflict, TaskNotFound, TaskStore, EventCursorError
from .events import stream_events


class TaskView(BaseModel):
    """显式公开字段；领取令牌、幂等键和 Worker 身份不进入 HTTP 响应。"""

    id: UUID
    request: TaskRequest
    state: Literal["queued", "running", "completed", "failed", "cancelled"]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    attempt_count: int
    result: dict | None
    error_code: str | None

    @classmethod
    def from_record(cls, record: TaskRecord):
        return cls.model_validate(record.model_dump(include=set(cls.model_fields)))


class ApiError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def create_app(store=None, snapshots=None):
    """Uvicorn factory；启动不迁移、不领取任务，也不加载模型凭据。"""
    if store is None:
        dsn = os.environ.get("TASK_DATABASE_URL")
        if not dsn:
            raise RuntimeError("task_database_url_missing")
        store = TaskStore(dsn)
    if snapshots is None:
        snapshots = SnapshotStore(Path(os.environ.get("TASK_SNAPSHOT_DIR", ".snapshots")))
    app = FastAPI(title="Incident Investigator Tasks", version="0.1.0")

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # 不回显 body/input/ctx；无效输入也可能含误填的模型密钥。
        return JSONResponse({"error": {"code": "invalid_request"}}, status_code=422)

    @app.exception_handler(psycopg.Error)
    async def database_error(request: Request, exc: psycopg.Error):
        # 连接信息与 SQL 不返回客户端；503 的写入结果可能不确定。
        return JSONResponse({"error": {"code": "database_unavailable"}}, status_code=503)

    @app.exception_handler(TaskNotFound)
    async def not_found(request: Request, exc: TaskNotFound):
        return JSONResponse({"error": {"code": "task_not_found"}}, status_code=404)

    @app.exception_handler(TaskConflict)
    async def conflict(request: Request, exc: TaskConflict):
        code = str(exc) if str(exc) in {"idempotency_key_conflict", "task_not_queued"} else "task_conflict"
        return JSONResponse({"error": {"code": code}}, status_code=409)

    @app.exception_handler(EventCursorError)
    async def cursor_error(request: Request, exc: EventCursorError):
        return JSONResponse({"error": {"code": "invalid_event_cursor"}}, status_code=409)

    @app.get("/tasks/{task_id}/events")
    def events(task_id: UUID,
               after: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
               last_event_id: Annotated[str | None, Header(pattern=r"^[0-9]{1,19}$")] = None):
        cursor = int(last_event_id) if last_event_id is not None else after
        initial = store.read_events(task_id, cursor)
        if initial["terminal"] and not initial["events"]:
            return Response(status_code=204)
        return StreamingResponse(stream_events(store, task_id, cursor, initial),
            media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/tasks", response_model=TaskView, status_code=202)
    def submit(request: TaskRequest, response: Response,
               idempotency_key: Annotated[str, Header(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")]):
        try:
            snapshots.read(request.snapshot_id)
        except FileNotFoundError:
            raise ApiError(404, "snapshot_not_found") from None
        except ValueError:
            raise ApiError(422, "snapshot_invalid") from None
        except OSError:
            raise ApiError(503, "snapshot_unavailable") from None
        task = store.submit(request, idempotency_key)
        response.headers["Location"] = f"/tasks/{task.id}"
        return TaskView.from_record(task)

    @app.get("/tasks/{task_id}", response_model=TaskView)
    def get(task_id: UUID):
        return TaskView.from_record(store.get(task_id))

    @app.post("/tasks/{task_id}/cancel", response_model=TaskView)
    def cancel(task_id: UUID):
        # 先区分不存在；真正取消仍使用数据库条件更新，与领取竞争只有一方成功。
        store.get(task_id)
        return TaskView.from_record(store.cancel_queued(task_id))

    return app
