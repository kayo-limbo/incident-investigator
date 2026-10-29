import asyncio
import os
import signal
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import psycopg

from backend.tasks.daemon import run_forever, run_service
from backend.tasks.models import TaskRequest
from backend.tasks.store import TaskConflict, TaskStore


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_stop_wakes_without_next_claim(self):
        stop = asyncio.Event()
        entered = asyncio.Event()

        async def idle(*args):
            entered.set()
            return None

        with patch("backend.tasks.daemon.run_once", side_effect=idle) as once:
            task = asyncio.create_task(run_forever(None, None, "w", stop=stop, poll_seconds=60, emit=Mock()))
            await entered.wait()
            stop.set()
            self.assertEqual(await asyncio.wait_for(task, 2), 0)
            once.assert_awaited_once()

    async def test_stop_drains_active_work_without_claiming_next(self):
        stop, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        finished = SimpleNamespace(id=uuid4(), state="completed", attempt_count=1, error_code=None)

        async def execute(*args):
            entered.set()
            await release.wait()
            return finished

        with patch("backend.tasks.daemon.run_once", side_effect=execute) as once:
            events = []
            task = asyncio.create_task(run_forever(None, None, "w", stop=stop, emit=events.append))
            await entered.wait()
            stop.set()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            self.assertEqual(await task, 0)
            once.assert_awaited_once()
        self.assertEqual(events[-2]["event"], "task_finished")
        self.assertEqual(events[-1]["processed"], 1)

    async def test_retry_cap_exponential_backoff_and_no_secret_output(self):
        events, delays = [], []

        async def wait(stop, seconds):
            delays.append(seconds)

        with patch("backend.tasks.daemon.run_once", side_effect=psycopg.OperationalError("password=secret")) as once, \
                patch("backend.tasks.daemon.random.uniform", return_value=1), \
                patch("backend.tasks.daemon.wait_or_stop", side_effect=wait):
            code = await run_forever(None, None, "w", stop=asyncio.Event(), max_errors=8, emit=events.append)
        self.assertEqual(code, 1)
        self.assertEqual(once.await_count, 8)
        self.assertEqual(delays, [1, 2, 4, 8, 16, 30, 30])
        self.assertNotIn("secret", str(events))
        self.assertEqual(events[-1]["reason"], "error_limit")

    async def test_successful_poll_resets_error_streak(self):
        events = []
        with patch("backend.tasks.daemon.run_once", side_effect=[TaskConflict("lost"), None,
                psycopg.InterfaceError("lost"), psycopg.OperationalError("lost")]) as once, \
                patch("backend.tasks.daemon.wait_or_stop", new_callable=AsyncMock):
            code = await run_forever(None, None, "w", stop=asyncio.Event(), max_errors=2, emit=events.append)
        self.assertEqual(code, 1)
        self.assertEqual(once.await_count, 4)
        self.assertEqual([e["consecutive_errors"] for e in events if e["event"] == "worker_retry"], [1, 1])

    async def test_permanent_database_error_is_not_retried(self):
        with patch("backend.tasks.daemon.run_once", side_effect=psycopg.ProgrammingError("missing table")) as once:
            with self.assertRaises(psycopg.ProgrammingError):
                await run_forever(None, None, "w", stop=asyncio.Event(), emit=Mock())
            once.assert_awaited_once()

    async def test_failed_task_does_not_stop_consumer(self):
        stop = asyncio.Event()
        finished = SimpleNamespace(id=uuid4(), state="failed", attempt_count=1, error_code="model_error")
        next_task = SimpleNamespace(id=uuid4(), state="completed", attempt_count=1, error_code=None)
        events = []

        def emit(event):
            events.append(event)
            if event["event"] == "task_finished" and event["state"] == "completed":
                stop.set()

        with patch("backend.tasks.daemon.run_once", side_effect=[finished, next_task]) as once:
            self.assertEqual(await run_forever(None, None, "w", stop=stop, emit=emit), 0)
            self.assertEqual(once.await_count, 2)
        self.assertEqual(events[-1]["processed"], 2)

    async def test_stop_interrupts_backoff(self):
        stop = asyncio.Event()

        def emit(event):
            if event["event"] == "worker_retry":
                stop.set()

        with patch("backend.tasks.daemon.run_once", side_effect=TaskConflict("lost")) as once:
            self.assertEqual(await asyncio.wait_for(run_forever(None, None, "w", stop=stop, emit=emit), 2), 0)
            once.assert_awaited_once()

    async def test_invalid_options_never_claim(self):
        with patch("backend.tasks.daemon.run_once") as once:
            for kwargs in ({"poll_seconds": float("nan")}, {"poll_seconds": True},
                           {"poll_seconds": 0}, {"max_errors": True}, {"max_errors": 0}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    await run_forever(None, None, "w", stop=asyncio.Event(), emit=Mock(), **kwargs)
            once.assert_not_called()

    async def test_signal_stops_loop_and_restores_previous_handlers(self):
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

        async def idle(*args):
            signal.raise_signal(signal.SIGTERM)
            return None

        with patch("backend.tasks.daemon.run_once", side_effect=idle):
            self.assertEqual(await asyncio.wait_for(run_service(None, None, "w", emit=Mock()), 2), 0)
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)

    async def test_signal_handlers_restored_after_failure(self):
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with patch("backend.tasks.daemon.run_once", side_effect=psycopg.ProgrammingError("missing table")):
            with self.assertRaises(psycopg.ProgrammingError):
                await run_service(None, None, "w", emit=Mock())
        self.assertEqual({sig: signal.getsignal(sig) for sig in before}, before)


class DaemonCliTests(unittest.TestCase):
    def test_cli_passes_options_and_preserves_failure_exit_code(self):
        from backend.tasks.__main__ import main
        with patch.dict(os.environ, {"TASK_DATABASE_URL": "unused"}), \
                patch("backend.tasks.__main__.TaskStore") as store, \
                patch("backend.tasks.__main__.SnapshotStore") as snapshots, \
                patch("backend.tasks.__main__.run_service", new_callable=AsyncMock, return_value=1) as service:
            self.assertEqual(main(["work", "--worker-id", "daemon-1", "--lease-seconds", "45",
                                   "--poll-seconds", "2", "--max-errors", "3"]), 1)
            service.assert_awaited_once_with(store.return_value, snapshots.return_value, "daemon-1",
                                            lease_seconds=45, poll_seconds=2, max_errors=3)


@unittest.skipUnless(os.environ.get("TASK_TEST_DATABASE_URL"), "requires TASK_TEST_DATABASE_URL")
class DaemonDatabaseTests(unittest.TestCase):
    def setUp(self):
        from test_async_hypotheses import setup_snapshot
        self.store = TaskStore(os.environ["TASK_TEST_DATABASE_URL"], schema="test_daemon_" + uuid4().hex)
        self.store.initialize()
        self.addCleanup(self.drop_schema)
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.snapshots, self.key = setup_snapshot(directory)

    def drop_schema(self):
        from psycopg import sql
        with self.store.connection() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.store.schema)))

    def test_continuous_consumer_recovers_expired_and_drains_queue(self):
        jobs = [self.store.submit(TaskRequest(snapshot_id=self.key), f"job-{i}") for i in range(3)]
        abandoned = self.store.claim("old")
        with self.store.connection() as conn:
            conn.execute("UPDATE investigation_tasks SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                         (abandoned.id,))

        async def scenario():
            stop = asyncio.Event()
            events = []

            def emit(event):
                events.append(event)
                if sum(e["event"] == "task_finished" for e in events) == 3:
                    stop.set()

            self.assertEqual(await asyncio.wait_for(run_forever(self.store, self.snapshots, "daemon", stop=stop,
                                                               emit=emit), 10), 0)
            self.assertEqual(events[-1]["processed"], 3)

        asyncio.run(scenario())
        self.assertTrue(all(self.store.get(job.id).state == "completed" for job in jobs))
        self.assertEqual(self.store.get(abandoned.id).attempt_count, 2)
        self.assertIsNone(self.store.claim("empty"))

    def test_real_worker_drains_on_stop_and_leaves_next_queued(self):
        first = self.store.submit(TaskRequest(snapshot_id=self.key), "first")
        second = self.store.submit(TaskRequest(snapshot_id=self.key), "second")
        from backend.investigation.window_baseline import investigate_window_fixed
        report = investigate_window_fixed(self.snapshots, self.key)

        async def scenario():
            stop, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def execute(*args):
                entered.set()
                await release.wait()
                return report

            with patch("backend.tasks.worker.execute", side_effect=execute):
                running = asyncio.create_task(run_forever(self.store, self.snapshots, "daemon", stop=stop, emit=Mock()))
                await asyncio.wait_for(entered.wait(), 2)
                stop.set()
                release.set()
                self.assertEqual(await asyncio.wait_for(running, 5), 0)

        asyncio.run(scenario())
        self.assertEqual(self.store.get(first.id).state, "completed")
        self.assertEqual(self.store.get(second.id).state, "queued")
