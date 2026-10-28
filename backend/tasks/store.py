"""短事务领取与条件更新；模型调用期间不持有数据库事务。"""

import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import TaskRecord, TaskRequest


class TaskConflict(ValueError):
    pass


class TaskNotFound(ValueError):
    pass


def validate_label(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
        raise ValueError("标识须为1至100位字母、数字或_.:-")


class TaskStore:
    def __init__(self, dsn, *, schema="investigator_tasks"):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ValueError("invalid_schema")
        self._dsn, self.schema = dsn, schema

    @contextmanager
    def connection(self):
        with psycopg.connect(self._dsn, connect_timeout=5, row_factory=dict_row,
                              options="-c statement_timeout=5000 -c lock_timeout=2000") as conn:
            conn.execute(sql.SQL("SET LOCAL search_path TO {} ").format(sql.Identifier(self.schema)))
            yield conn  # 正常退出提交，异常回滚并关闭连接。

    def initialize(self):
        with self.connection() as conn:
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
            conn.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))

    def submit(self, request: TaskRequest, idempotency_key: str):
        validate_label(idempotency_key)
        request = TaskRequest.model_validate(request.model_dump())
        payload = request.model_dump(mode="json")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.connection() as conn:
            row = conn.execute("""INSERT INTO investigation_tasks (id, idempotency_key, request, request_hash)
                VALUES (%s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING *""",
                (uuid4(), idempotency_key, Jsonb(payload), digest)).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM investigation_tasks WHERE idempotency_key=%s",
                                   (idempotency_key,)).fetchone()
                if row["request_hash"] != digest or row["request"] != payload:
                    raise TaskConflict("idempotency_key_conflict")
            return TaskRecord.model_validate(row)

    def get(self, task_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM investigation_tasks WHERE id=%s", (UUID(str(task_id)),)).fetchone()
            if row is None:
                raise TaskNotFound("task_not_found")
            return TaskRecord.model_validate(row)

    def claim(self, worker_id):
        validate_label(worker_id)
        with self.connection() as conn:
            row = conn.execute("""WITH candidate AS (
                SELECT id FROM investigation_tasks WHERE state='queued'
                ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1
            ) UPDATE investigation_tasks AS task SET state='running', worker_id=%s,
                run_token=%s, started_at=clock_timestamp()
              FROM candidate WHERE task.id=candidate.id RETURNING task.*""",
                (worker_id, uuid4())).fetchone()
            return TaskRecord.model_validate(row) if row else None

    def finish(self, task_id, run_token, report):
        # 报告绑定快照，不允许把另一个调查的结果写进该任务。
        payload = report.model_dump(mode="json")
        state = "completed" if report.termination == "completed" else (
            "cancelled" if report.termination == "cancelled" else "failed")
        with self.connection() as conn:
            row = conn.execute("""UPDATE investigation_tasks SET state=%s, result=%s,
                error_code=%s, finished_at=clock_timestamp()
                WHERE id=%s AND run_token=%s AND state='running' AND request->>'snapshot_id'=%s
                RETURNING *""", (state, Jsonb(payload), None if state == "completed" else report.termination,
                UUID(str(task_id)), UUID(str(run_token)), report.snapshot_id)).fetchone()
            if row is None:
                raise TaskConflict("task_ownership_or_state_conflict")
            return TaskRecord.model_validate(row)

    def fail(self, task_id, run_token, *, cancelled=False):
        with self.connection() as conn:
            row = conn.execute("""UPDATE investigation_tasks SET state=%s, error_code=%s,
                finished_at=clock_timestamp() WHERE id=%s AND run_token=%s AND state='running' RETURNING *""",
                ("cancelled" if cancelled else "failed", "worker_cancelled" if cancelled else "worker_error",
                 UUID(str(task_id)), UUID(str(run_token)))).fetchone()
            if row is None:
                raise TaskConflict("task_ownership_or_state_conflict")
            return TaskRecord.model_validate(row)

    def cancel_queued(self, task_id):
        with self.connection() as conn:
            row = conn.execute("""UPDATE investigation_tasks SET state='cancelled',
                error_code='cancelled_before_start', finished_at=clock_timestamp()
                WHERE id=%s AND state='queued' RETURNING *""", (UUID(str(task_id)),)).fetchone()
            if row is None:
                raise TaskConflict("task_not_queued")
            return TaskRecord.model_validate(row)
