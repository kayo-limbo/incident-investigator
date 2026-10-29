import asyncio
import json
import os
import socket
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from uuid import uuid4

import httpx
import psycopg
import uvicorn
from fastapi.testclient import TestClient

from backend.tasks.api import create_app
from backend.tasks.events import stream_events
from backend.tasks.models import TaskRequest
from backend.tasks.store import TaskStore, TaskConflict


def event(sequence, state="queued"):
    return {"sequence": sequence, "state": state, "kind": "state_changed"}


class EventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_finishes_on_terminal_without_polling(self):
        store = Mock()
        parts = [part async for part in stream_events(store, uuid4(), 0,
            {"events": [event(1), event(2, "cancelled")], "terminal": True})]
        self.assertIn("id: 1\n", parts[1])
        self.assertIn("id: 2\n", parts[2])
        store.read_events.assert_not_called()

    async def test_stream_failure_preserves_cursor_and_hides_exception(self):
        store = Mock()
        store.read_events.side_effect = psycopg.OperationalError("password=secret")
        parts = [part async for part in stream_events(store, uuid4(), 1,
            {"events": [], "terminal": False}, poll_seconds=0)]
        self.assertIn("stream_error", parts[-1])
        self.assertNotIn("id:", parts[-1])
        self.assertNotIn("secret", "".join(parts))

    async def test_generator_close_stops_polling(self):
        store = Mock()
        stream = stream_events(store, uuid4(), 0, {"events": [], "terminal": False})
        await anext(stream)
        self.assertEqual(await anext(stream), ": keepalive\n\n")
        await stream.aclose()
        store.read_events.assert_not_called()

    async def test_connection_deadline_closes_after_initial_batch(self):
        store = Mock()
        parts = [part async for part in stream_events(store, uuid4(), 0,
            {"events": [event(1)], "terminal": False}, duration_seconds=0)]
        self.assertEqual(len(parts), 2)
        store.read_events.assert_not_called()


class EventApiTests(unittest.TestCase):
    def test_invalid_cursor_and_header_precedence(self):
        store = Mock()
        store.read_events.return_value = {"events": [], "terminal": True}
        with TestClient(create_app(store, Mock())) as client:
            url = f"/tasks/{uuid4()}/events"
            for header in ("-1", "abc", "1\t2", "1" * 20):
                self.assertEqual(client.get(url, headers={"Last-Event-ID": header}).status_code, 422)
            store.read_events.assert_not_called()
            self.assertEqual(client.get(url + "?after=0", headers={"Last-Event-ID": "2"}).status_code, 204)
            self.assertEqual(store.read_events.call_args.args[1], 2)


@unittest.skipUnless(os.environ.get("TASK_TEST_DATABASE_URL"), "requires TASK_TEST_DATABASE_URL")
class EventDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(os.environ["TASK_TEST_DATABASE_URL"], schema="test_events_" + uuid4().hex)
        self.store.initialize()
        self.addCleanup(self.drop_schema)
        self.request = TaskRequest(snapshot_id="a" * 64)

    def drop_schema(self):
        from psycopg import sql
        with self.store.connection() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.store.schema)))

    def test_idempotency_heartbeat_and_rejected_writes_emit_no_extra_events(self):
        task = self.store.submit(self.request, "once")
        self.store.submit(self.request, "once")
        running = self.store.claim("worker")
        self.store.renew(running.id, running.run_token)
        with self.assertRaises(TaskConflict):
            self.store.fail(task.id, uuid4())
        self.store.fail(task.id, running.run_token)
        batch = self.store.read_events(task.id)
        self.assertEqual([e["sequence"] for e in batch["events"]], [1, 2, 3])
        self.assertEqual([e["state"] for e in batch["events"]], ["queued", "running", "failed"])
        self.assertTrue(batch["terminal"])
        self.assertNotIn("run_token", str(batch))

    def test_event_write_failure_rolls_back_state_change(self):
        task = self.store.submit(self.request, "rollback")
        with self.store.connection() as conn:
            conn.execute("ALTER TABLE task_events ADD CONSTRAINT reject_running CHECK (state <> 'running')")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.store.claim("worker")
        self.assertEqual(self.store.get(task.id).state, "queued")
        self.assertEqual(len(self.store.read_events(task.id)["events"]), 1)

    def test_migration_backfills_only_snapshot_and_is_idempotent(self):
        task = self.store.submit(self.request, "old")
        with self.store.connection() as conn:
            conn.execute("DROP TRIGGER task_state_event ON investigation_tasks")
            conn.execute("DROP TABLE task_events")
            conn.execute("DELETE FROM task_schema_versions WHERE version=3")
        self.store.initialize()
        self.store.initialize()
        events = self.store.read_events(task.id)["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "snapshot")
        self.store.cancel_queued(task.id)
        self.assertEqual(self.store.read_events(task.id)["events"][-1]["sequence"], 2)

    def test_concurrent_submit_then_cancel_claim_race_preserves_sequence(self):
        with ThreadPoolExecutor(6) as pool:
            jobs = list(pool.map(lambda _: self.store.submit(self.request, "same"), range(6)))
        self.assertEqual(len({j.id for j in jobs}), 1)
        task_id = jobs[0].id
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait()
            return self.store.claim("worker")

        def cancel():
            barrier.wait()
            try:
                return self.store.cancel_queued(task_id)
            except TaskConflict:
                return None

        with ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(claim), pool.submit(cancel)
            running, cancelled = a.result(), b.result()
        self.assertNotEqual(running is None, cancelled is None)
        if running:
            self.store.fail(task_id, running.run_token)
        events = self.store.read_events(task_id)["events"]
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual([e["state"] for e in events],
                         ["queued", "running", "failed"] if running else ["queued", "cancelled"])

    def test_lease_recovery_is_a_persisted_transition(self):
        task = self.store.submit(self.request, "recover")
        running = self.store.claim("old")
        with self.store.connection() as conn:
            conn.execute("UPDATE investigation_tasks SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                         (task.id,))
        self.store.recover_expired()
        new = self.store.claim("new")
        self.store.fail(new.id, new.run_token)
        with self.assertRaises(TaskConflict):
            self.store.fail(task.id, running.run_token)
        events = self.store.read_events(task.id)["events"]
        self.assertEqual([e["state"] for e in events], ["queued", "running", "queued", "running", "failed"])
        self.assertEqual([e["attempt_count"] for e in events], [0, 1, 1, 2, 2])

    def test_sse_resume_and_cursor_errors(self):
        task = self.store.submit(self.request, "sse")
        self.store.cancel_queued(task.id)
        with TestClient(create_app(self.store, Mock())) as client:
            url = f"/tasks/{task.id}/events"
            response = client.get(url, headers={"Last-Event-ID": "1"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            self.assertIn("id: 2\n", response.text)
            self.assertNotIn("id: 1\n", response.text)
            self.assertEqual(client.get(url, headers={"Last-Event-ID": "2"}).status_code, 204)
            self.assertEqual(client.get(url + "?after=3").status_code, 409)
            self.assertEqual(client.get(f"/tasks/{uuid4()}/events").status_code, 404)

    def test_real_http_disconnect_then_resume(self):
        task = self.store.submit(self.request, "network")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(self.store, Mock()), log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                if time.monotonic() >= deadline:
                    self.fail("uvicorn_start_timeout")
                time.sleep(0.01)
            url = f"http://127.0.0.1:{port}/tasks/{task.id}/events"
            with httpx.Client(timeout=5) as client:
                with client.stream("GET", url) as response:
                    self.assertEqual(response.status_code, 200)
                    for line in response.iter_lines():
                        if line.startswith("data:"):
                            self.assertEqual(json.loads(line[5:])["sequence"], 1)
                            break
                # 首次连接真实关闭后发生状态变化，重连从数据库补齐。
                running = self.store.claim("worker")
                self.store.fail(task.id, running.run_token)
                response = client.get(url, headers={"Last-Event-ID": "1"})
                self.assertIn("id: 2\n", response.text)
                self.assertIn("id: 3\n", response.text)
                self.assertNotIn("id: 1\n", response.text)
        finally:
            server.should_exit = True
            thread.join(8)
            sock.close()
        self.assertFalse(thread.is_alive(), "stream left server waiting during shutdown")
