import asyncio
import importlib.util
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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
