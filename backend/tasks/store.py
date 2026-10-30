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


class EventCursorError(ValueError):
    pass


def validate_label(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
        raise ValueError("标识须为1至100位字母、数字或_.:-")


def validate_lease(seconds):
    if type(seconds) is not int or not 3 <= seconds <= 3600:
        raise ValueError("lease_seconds_must_be_integer_3_to_3600")


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
            conn.execute("LOCK TABLE investigation_tasks IN ACCESS EXCLUSIVE MODE")
            conn.execute("CREATE TABLE IF NOT EXISTS task_schema_versions (version integer PRIMARY KEY)")
            if not conn.execute("SELECT version FROM task_schema_versions WHERE version=2").fetchone():
                conn.execute(Path(__file__).with_name("lease_migration.sql").read_text(encoding="utf-8"))
                conn.execute("INSERT INTO task_schema_versions VALUES (2)")
            if not conn.execute("SELECT version FROM task_schema_versions WHERE version=3").fetchone():
                conn.execute(Path(__file__).with_name("events_migration.sql").read_text(encoding="utf-8"))
                conn.execute("INSERT INTO task_schema_versions VALUES (3)")
            if not conn.execute("SELECT version FROM task_schema_versions WHERE version=4").fetchone():
                conn.execute(Path(__file__).with_name("graph_migration.sql").read_text(encoding="utf-8"))
                conn.execute("INSERT INTO task_schema_versions VALUES (4)")

    def read_events(self, task_id, after=0, limit=100):
        if type(after) is not int or not 0 <= after <= 9223372036854775807:
            raise EventCursorError("invalid_event_cursor")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_event_limit")
        task_id = UUID(str(task_id))
        with self.connection() as conn:
            # 先读状态，再读事件。若状态已终结，随后查询一定能读到同事务事件。
            task = conn.execute("SELECT state FROM investigation_tasks WHERE id=%s", (task_id,)).fetchone()
            if task is None:
                raise TaskNotFound("task_not_found")
            latest = conn.execute("SELECT COALESCE(MAX(sequence), 0) AS seq FROM task_events WHERE task_id=%s",
                                  (task_id,)).fetchone()["seq"]
            if after > latest:
                raise EventCursorError("event_cursor_ahead")
            events = conn.execute("""SELECT * FROM task_events WHERE task_id=%s AND sequence>%s
                ORDER BY sequence LIMIT %s""", (task_id, after, limit)).fetchall()
            return {"events": events, "terminal": task["state"] in ("completed", "failed", "cancelled")}

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

    def recover_expired(self):
        """最多回收100项；仅过期尝试重排，业务失败不重试，总领取上限3次。"""
        with self.connection() as conn:
            rows = conn.execute("""WITH expired AS (
                SELECT id FROM investigation_tasks WHERE state='running'
                  AND lease_expires_at <= clock_timestamp()
                ORDER BY lease_expires_at, id LIMIT 100 FOR UPDATE SKIP LOCKED
            ) UPDATE investigation_tasks AS task SET
                state=CASE WHEN attempt_count < 3 THEN 'queued' ELSE 'failed' END,
                error_code=CASE WHEN attempt_count < 3 THEN NULL ELSE 'lease_attempts_exhausted' END,
                finished_at=CASE WHEN attempt_count < 3 THEN NULL ELSE clock_timestamp() END,
                started_at=CASE WHEN attempt_count < 3 THEN NULL ELSE started_at END,
                worker_id=CASE WHEN attempt_count < 3 THEN NULL ELSE worker_id END,
                run_token=CASE WHEN attempt_count < 3 THEN NULL ELSE run_token END,
                lease_expires_at=NULL
                FROM expired WHERE task.id=expired.id RETURNING task.*""").fetchall()
            return [TaskRecord.model_validate(row) for row in rows]

    def renew(self, task_id, run_token, lease_seconds=30):
        validate_lease(lease_seconds)
        with self.connection() as conn:
            row = conn.execute("""UPDATE investigation_tasks
                SET lease_expires_at=clock_timestamp() + %s * interval '1 second'
                WHERE id=%s AND run_token=%s AND state='running'
                  AND lease_expires_at > clock_timestamp() RETURNING *""",
                (lease_seconds, UUID(str(task_id)), UUID(str(run_token)))).fetchone()
            if row is None:
                raise TaskConflict("task_lease_lost")
            return TaskRecord.model_validate(row)

    def claim(self, worker_id, lease_seconds=30):
        validate_label(worker_id)
        validate_lease(lease_seconds)
        with self.connection() as conn:
            row = conn.execute("""WITH candidate AS (
                SELECT id FROM investigation_tasks WHERE state='queued'
                ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1
            ) UPDATE investigation_tasks AS task SET state='running', worker_id=%s,
                run_token=%s, started_at=clock_timestamp(), attempt_count=attempt_count+1,
                lease_expires_at=clock_timestamp() + %s * interval '1 second'
              FROM candidate WHERE task.id=candidate.id RETURNING task.*""",
                (worker_id, uuid4(), lease_seconds)).fetchone()
            return TaskRecord.model_validate(row) if row else None

    def finish(self, task_id, run_token, report):
        # 报告绑定快照，不允许把另一个调查的结果写进该任务。
        payload = report.model_dump(mode="json")
        state = "completed" if report.termination == "completed" else (
            "cancelled" if report.termination == "cancelled" else "failed")
        with self.connection() as conn:
            row = conn.execute("""UPDATE investigation_tasks SET state=%s, result=%s,
                error_code=%s, finished_at=clock_timestamp(), lease_expires_at=NULL
                WHERE id=%s AND run_token=%s AND state='running' AND request->>'snapshot_id'=%s
                  AND lease_expires_at > clock_timestamp()
                RETURNING *""", (state, Jsonb(payload), None if state == "completed" else report.termination,
                UUID(str(task_id)), UUID(str(run_token)), report.snapshot_id)).fetchone()
            if row is None:
                raise TaskConflict("task_ownership_or_state_conflict")
            return TaskRecord.model_validate(row)

    def fail(self, task_id, run_token, *, cancelled=False):
        with self.connection() as conn:
            row = conn.execute("""UPDATE investigation_tasks SET state=%s, error_code=%s,
                finished_at=clock_timestamp(), lease_expires_at=NULL
                WHERE id=%s AND run_token=%s AND state='running'
                  AND lease_expires_at > clock_timestamp() RETURNING *""",
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
