import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

from pydantic import ValidationError
from backend.tasks.models import TaskRequest


class TaskRequestTests(unittest.TestCase):
    def test_reject_invalid_budget_and_unknown_fields(self):
        for options in ({"max_tool_calls": True}, {"max_tool_calls": 0},
                        {"strategy": "agent", "timeout_seconds": float("nan")},
                        {"max_total_tokens": 12}, {"api_key": "never-persist"}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                TaskRequest(snapshot_id="a" * 64, **options)

    def test_model_budget_is_accepted(self):
        request = TaskRequest(snapshot_id="a" * 64, strategy="hypotheses", max_total_tokens=200)
        self.assertEqual(request.max_total_tokens, 200)


DSN = os.environ.get("TASK_TEST_DATABASE_URL")


@unittest.skipUnless(importlib.util.find_spec("psycopg"), "requires requirements-tasks.txt")
class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.task = SimpleNamespace(id=uuid4(), run_token=uuid4(), request=TaskRequest(snapshot_id="a" * 64))
        self.store = Mock()
        self.store.claim.return_value = self.task

    async def test_idle_does_not_execute(self):
        from backend.tasks.worker import run_once
        self.store.claim.return_value = None
        with patch("backend.tasks.worker.execute", new_callable=AsyncMock) as execute:
            self.assertIsNone(await run_once(self.store, None, "worker"))
            execute.assert_not_awaited()

    async def test_persistence_failure_does_not_retry_or_overwrite(self):
        from backend.tasks.worker import run_once
        self.store.finish.side_effect = RuntimeError("database_unavailable")
        with patch("backend.tasks.worker.execute", new_callable=AsyncMock) as execute:
            with self.assertRaises(RuntimeError):
                await run_once(self.store, None, "worker")
            execute.assert_awaited_once()
        self.store.fail.assert_not_called()

    async def test_cancellation_without_report_is_saved_and_propagated(self):
        from backend.tasks.worker import run_once, WorkerCancelled
        with patch("backend.tasks.worker.execute", side_effect=asyncio.CancelledError):
            with self.assertRaises(WorkerCancelled):
                await run_once(self.store, None, "worker")
        self.store.fail.assert_called_once_with(self.task.id, self.task.run_token, cancelled=True)

    async def test_adapter_budget_forwarding_and_cleanup(self):
        from backend.tasks.worker import execute
        for strategy in ("agent", "hypotheses"):
            adapter_name = "AsyncWindowChatModel" if strategy == "agent" else "AsyncHypothesisChatModel"
            runner_name = "investigate_window_async" if strategy == "agent" else "investigate_hypotheses_async"
            request = TaskRequest(snapshot_id="a" * 64, strategy=strategy, timeout_seconds=10,
                                  max_total_tokens=200, repeat_query_policy="allow")
            with patch("backend.tasks.worker.ModelConfig.from_env"), \
                    patch("backend.tasks.worker." + adapter_name) as adapter, \
                    patch("backend.tasks.worker." + runner_name, new_callable=AsyncMock) as runner:
                await execute(request, "snapshots")
                runner.assert_awaited_once_with("snapshots", request.snapshot_id,
                    adapter.return_value.__aenter__.return_value, 6, timeout_seconds=10,
                    max_total_tokens=200, repeat_query_policy="allow")
                adapter.return_value.__aexit__.assert_awaited_once()

    async def test_lost_lease_cancels_execution_without_writing_failure(self):
        from backend.tasks.worker import run_once
        from backend.tasks.store import TaskConflict
        stopped = asyncio.Event()
        async def pending(*args):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.store.renew.side_effect = TaskConflict("task_lease_lost")
        with patch("backend.tasks.worker.execute", side_effect=pending):
            with self.assertRaises(TaskConflict):
                await asyncio.wait_for(run_once(self.store, None, "worker", 3), 5)
        self.assertTrue(stopped.is_set())
        self.store.finish.assert_not_called()
        self.store.fail.assert_not_called()


@unittest.skipUnless(DSN and importlib.util.find_spec("psycopg"), "requires TASK_TEST_DATABASE_URL and requirements-tasks.txt")
class TaskDatabaseTests(unittest.TestCase):
    def setUp(self):
        from backend.tasks.store import TaskStore
        from test_async_hypotheses import setup_snapshot
        self.schema = "test_tasks_" + uuid4().hex
        self.store = TaskStore(DSN, schema=self.schema)
        self.store.initialize()
        self.addCleanup(self.drop_schema)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.snapshots, self.key = setup_snapshot(temp.name)
        self.request = TaskRequest(snapshot_id=self.key)

    def drop_schema(self):
        from psycopg import sql
        with self.store.connection() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def report(self):
        from backend.investigation.window_baseline import investigate_window_fixed
        return investigate_window_fixed(self.snapshots, self.key)

    def test_concurrent_idempotency_and_conflict(self):
        from backend.tasks.store import TaskConflict
        barrier = Barrier(6)
        def submit(_):
            barrier.wait()
            return self.store.submit(self.request, "same-key").id
        with ThreadPoolExecutor(6) as pool:
            ids = list(pool.map(submit, range(6)))
        self.assertEqual(len(set(ids)), 1)
        with self.assertRaises(TaskConflict):
            self.store.submit(TaskRequest(snapshot_id=self.key, max_tool_calls=2), "same-key")

    def test_concurrent_claims_have_single_owner(self):
        ids = {self.store.submit(self.request, f"job-{i}").id for i in range(8)}
        barrier = Barrier(8)
        def claim(i):
            barrier.wait()
            return self.store.claim(f"worker-{i}")
        with ThreadPoolExecutor(8) as pool:
            tasks = list(pool.map(claim, range(8)))
        self.assertEqual({task.id for task in tasks}, ids)
        self.assertEqual(len({task.run_token for task in tasks}), 8)
        self.assertIsNone(self.store.claim("idle"))

    def test_locked_row_is_skipped(self):
        first = self.store.submit(self.request, "first")
        second = self.store.submit(self.request, "second")
        with self.store.connection() as conn:
            conn.execute("SELECT id FROM investigation_tasks WHERE id=%s FOR UPDATE", (first.id,))
            self.assertEqual(self.store.claim("worker").id, second.id)
        self.assertEqual(self.store.claim("worker").id, first.id)

    def test_finish_checks_token_snapshot_and_terminal_state(self):
        from backend.tasks.store import TaskConflict
        self.store.submit(self.request, "job")
        task = self.store.claim("worker")
        report = self.report()
        with self.assertRaises(TaskConflict):
            self.store.finish(task.id, uuid4(), report)
        with self.assertRaises(TaskConflict):
            self.store.finish(task.id, task.run_token, report.model_copy(update={"snapshot_id": "0" * 64}))
        result = self.store.finish(task.id, task.run_token, report)
        self.assertEqual(result.state, "completed")
        with self.assertRaises(TaskConflict):
            self.store.finish(task.id, task.run_token, report)

    def test_cancel_queued_and_reject_running_cancel(self):
        from backend.tasks.store import TaskConflict
        task = self.store.submit(self.request, "cancel")
        self.assertEqual(self.store.cancel_queued(task.id).state, "cancelled")
        self.assertIsNone(self.store.claim("worker"))
        self.store.submit(self.request, "running")
        running = self.store.claim("worker")
        with self.assertRaises(TaskConflict):
            self.store.cancel_queued(running.id)

    def test_worker_persists_report_across_store_instances(self):
        from backend.tasks.store import TaskStore
        from backend.tasks.worker import run_once
        task = self.store.submit(self.request, "job")
        result = asyncio.run(run_once(self.store, self.snapshots, "worker"))
        loaded = TaskStore(DSN, schema=self.schema).get(task.id)
        self.assertEqual(loaded.result, result.result)
        self.assertEqual(loaded.state, "completed")
        self.assertIsNone(asyncio.run(run_once(self.store, self.snapshots, "worker")))

    def test_worker_failure_is_sanitized(self):
        from backend.tasks.worker import run_once
        self.store.submit(self.request, "job")
        with patch("backend.tasks.worker.execute", side_effect=RuntimeError("sensitive-value")):
            result = asyncio.run(run_once(self.store, self.snapshots, "worker"))
        self.assertEqual(result.error_code, "worker_error")
        self.assertNotIn("sensitive-value", result.model_dump_json())

    def test_worker_preserves_cancelled_report_and_propagates_cancel(self):
        from backend.tasks.worker import run_once, WorkerCancelled
        from backend.investigation.async_investigation import InvestigationCancelled
        task = self.store.submit(self.request, "job")
        report = self.report().model_copy(update={"termination": "cancelled"})
        with patch("backend.tasks.worker.execute", side_effect=InvestigationCancelled(report)):
            with self.assertRaises(WorkerCancelled):
                asyncio.run(run_once(self.store, self.snapshots, "worker"))
        saved = self.store.get(task.id)
        self.assertEqual(saved.state, "cancelled")
        self.assertEqual(saved.result, report.model_dump(mode="json"))

    def test_execution_has_no_open_claim_transaction(self):
        from backend.tasks.worker import run_once, execute
        task = self.store.submit(self.request, "job")
        async def inspect(request, snapshots):
            with self.store.connection() as conn:
                row = conn.execute("SELECT state FROM investigation_tasks WHERE id=%s FOR UPDATE NOWAIT", (task.id,)).fetchone()
                self.assertEqual(row["state"], "running")
            return await execute(request, snapshots)
        with patch("backend.tasks.worker.execute", side_effect=inspect):
            self.assertEqual(asyncio.run(run_once(self.store, self.snapshots, "worker")).state, "completed")

    def test_cli_submit_execute_query_and_cancel(self):
        from backend.tasks.__main__ import main

        def invoke(*args):
            output = io.StringIO()
            # 只替换 schema 选择，仍使用真实 PostgreSQL 和真实固定调查执行器。
            with patch.dict(os.environ, {"TASK_DATABASE_URL": DSN}), \
                    patch("backend.tasks.__main__.TaskStore", return_value=self.store), \
                    redirect_stdout(output):
                code = main(["--snapshots", str(self.snapshots.directory), *args])
            return code, json.loads(output.getvalue())

        self.assertEqual(invoke("init"), (0, {"initialized": True}))
        code, task = invoke("submit", self.key, "--idempotency-key", "cli-job")
        self.assertEqual(code, 0)
        self.assertEqual(task["state"], "queued")
        self.assertEqual(invoke("submit", self.key, "--idempotency-key", "cli-job")[1]["id"], task["id"])
        self.assertEqual(invoke("submit", self.key, "--idempotency-key", "cli-job", "--max-tool-calls", "1"),
                         (1, {"error": "idempotency_key_conflict"}))
        code, completed = invoke("work-once", "--worker-id", "cli-worker")
        self.assertEqual(code, 0)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(invoke("get", task["id"]), (0, completed))
        self.assertEqual(invoke("work-once", "--worker-id", "cli-worker"), (0, {"idle": True}))
        _, queued = invoke("submit", self.key, "--idempotency-key", "cli-cancel")
        self.assertEqual(invoke("cancel", queued["id"])[1]["state"], "cancelled")
        self.assertEqual(invoke("cancel", task["id"]), (1, {"error": "task_not_queued"}))

    def test_noncompleted_report_is_saved_as_failed(self):
        from backend.tasks.worker import run_once
        self.store.submit(self.request, "budget-failure")
        report = self.report().model_copy(update={"termination": "deadline_exceeded"})
        with patch("backend.tasks.worker.execute", return_value=report):
            saved = asyncio.run(run_once(self.store, self.snapshots, "worker"))
        self.assertEqual(saved.state, "failed")
        self.assertEqual(saved.error_code, "deadline_exceeded")
        self.assertEqual(self.store.get(saved.id).result, report.model_dump(mode="json"))

    def expire(self, task):
        with self.store.connection() as conn:
            conn.execute("UPDATE investigation_tasks SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                         (task.id,))

    def test_expired_owner_cannot_renew_finish_or_fail_even_before_recovery(self):
        from backend.tasks.store import TaskConflict
        self.store.submit(self.request, "expired")
        old = self.store.claim("old")
        self.expire(old)
        for action in (lambda: self.store.renew(old.id, old.run_token),
                       lambda: self.store.finish(old.id, old.run_token, self.report()),
                       lambda: self.store.fail(old.id, old.run_token)):
            with self.assertRaises(TaskConflict):
                action()
        self.assertEqual(self.store.recover_expired()[0].state, "queued")
        new = self.store.claim("new")
        self.assertNotEqual(new.run_token, old.run_token)
        self.assertEqual(new.attempt_count, 2)
        with self.assertRaises(TaskConflict):
            self.store.finish(old.id, old.run_token, self.report())
        self.assertEqual(self.store.finish(new.id, new.run_token, self.report()).state, "completed")

    def test_recovery_is_concurrent_safe_and_attempts_are_bounded(self):
        self.store.submit(self.request, "bounded")
        for attempt in range(1, 4):
            task = self.store.claim("worker")
            self.assertEqual(task.attempt_count, attempt)
            self.expire(task)
            barrier = Barrier(4)
            def recover(_):
                barrier.wait()
                return self.store.recover_expired()
            with ThreadPoolExecutor(4) as pool:
                recovered = [item for batch in pool.map(recover, range(4)) for item in batch]
            self.assertEqual(len(recovered), 1)
        self.assertIsNone(self.store.claim("worker"))
        final = self.store.get(task.id)
        self.assertEqual(final.error_code, "lease_attempts_exhausted")
        self.assertEqual(final.state, "failed")

    def test_heartbeat_extends_live_lease_and_stops_after_completion(self):
        from backend.tasks.worker import run_once
        self.store.submit(self.request, "heartbeat")
        async def scenario():
            renewed = asyncio.Event()
            original_renew = self.store.renew
            def renew(*args):
                before = self.store.get(args[0]).lease_expires_at
                result = original_renew(*args)
                self.assertGreater(result.lease_expires_at, before)
                renewed.set()
                return result
            async def pending(*args):
                await asyncio.wait_for(renewed.wait(), 5)
                return self.report()
            with patch.object(self.store, "renew", side_effect=renew) as heartbeat, \
                    patch("backend.tasks.worker.execute", side_effect=pending):
                saved = await run_once(self.store, self.snapshots, "worker", 3)
                self.assertEqual(saved.state, "completed")
                self.assertIsNone(saved.lease_expires_at)
                heartbeat.assert_called_once()
                self.assertEqual(len(asyncio.all_tasks()), 1)
        asyncio.run(scenario())

    def test_external_cancel_preserves_partial_report(self):
        from backend.tasks.worker import run_once, WorkerCancelled
        from backend.investigation.async_investigation import InvestigationCancelled
        task = self.store.submit(self.request, "cancel-running")
        report = self.report().model_copy(update={"termination": "cancelled"})
        async def scenario():
            started = asyncio.Event()
            async def pending(*args):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise InvestigationCancelled(report)
            with patch("backend.tasks.worker.execute", side_effect=pending):
                running = asyncio.create_task(run_once(self.store, self.snapshots, "worker"))
                await started.wait()
                running.cancel()
                with self.assertRaises(WorkerCancelled):
                    await running
            self.assertEqual(len(asyncio.all_tasks()), 1)
        asyncio.run(scenario())
        self.assertEqual(self.store.get(task.id).result, report.model_dump(mode="json"))

    def test_process_exit_after_claim_is_recovered_by_next_worker(self):
        from backend.tasks.worker import run_once
        task = self.store.submit(self.request, "process-exit")
        script = ("import os,sys; from backend.tasks.store import TaskStore; "
                  "s=TaskStore(os.environ['TASK_TEST_DATABASE_URL'],schema=sys.argv[1]); "
                  "assert s.claim('crashed'); os._exit(0)")
        subprocess.run([sys.executable, "-c", script, self.schema], check=True, timeout=15)
        abandoned = self.store.get(task.id)
        self.assertEqual(abandoned.state, "running")
        # 控制数据库时钟边界，不等待30秒，也不声称测试了真实故障检测延迟。
        self.expire(abandoned)
        saved = asyncio.run(run_once(self.store, self.snapshots, "replacement"))
        self.assertEqual(saved.state, "completed")
        self.assertEqual(saved.attempt_count, 2)
        self.assertNotEqual(saved.run_token, abandoned.run_token)

    def test_upgrade_preserves_queued_and_quarantines_legacy_running(self):
        running = self.store.submit(self.request, "legacy-running")
        self.store.claim("legacy")
        queued = self.store.submit(self.request, "legacy-queued")
        with self.store.connection() as conn:
            conn.execute("ALTER TABLE investigation_tasks DROP COLUMN lease_expires_at, DROP COLUMN attempt_count")
            conn.execute("DROP TABLE task_schema_versions")
        self.store.initialize()
        self.store.initialize()
        self.assertEqual(self.store.get(running.id).error_code, "legacy_running_without_lease")
        self.assertEqual(self.store.get(queued.id).state, "queued")
        claimed = self.store.claim("new")
        self.store.initialize()
        self.assertEqual(self.store.get(claimed.id).run_token, claimed.run_token)
