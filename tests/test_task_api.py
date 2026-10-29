import asyncio
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
import psycopg
from fastapi.testclient import TestClient

from backend.tasks.api import create_app
from backend.tasks.models import TaskRecord, TaskRequest
from backend.tasks.store import TaskConflict, TaskNotFound, TaskStore


def record():
    return TaskRecord(id=uuid4(), idempotency_key="private-key",
        request=TaskRequest(snapshot_id="a" * 64), request_hash="private-hash",
        state="queued", worker_id="private-worker", run_token=uuid4(),
        created_at=datetime.now(timezone.utc), started_at=None, finished_at=None,
        result=None, error_code=None, lease_expires_at=None, attempt_count=0)


class TaskApiTests(unittest.TestCase):
    def setUp(self):
        self.store, self.snapshots = Mock(), Mock()
        self.task = record()
        self.store.submit.return_value = self.task
        self.store.get.return_value = self.task
        self.client = self.enterContext(TestClient(create_app(self.store, self.snapshots)))

    def submit(self, **kwargs):
        return self.client.post("/tasks", json=kwargs or self.task.request.model_dump(),
                                headers={"Idempotency-Key": "request-1"})

    def test_acceptance_location_and_private_field_projection(self):
        response = self.submit()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["location"], f"/tasks/{self.task.id}")
        self.assertEqual(response.json()["state"], "queued")
        self.assertNotIn("private-", response.text)
        self.assertNotIn("run_token", response.json())
        self.assertNotIn("lease_expires_at", response.json())
        self.assertEqual(self.client.get(response.headers["location"]).json(), response.json())
        self.store.claim.assert_not_called()

    def test_invalid_request_never_reaches_storage_or_echoes_input(self):
        cases = [self.task.request.model_dump() | {"api_key": "secret-value"},
                 {"snapshot_id": "../secret-value"},
                 self.task.request.model_dump() | {"max_tool_calls": True}]
        for payload in cases:
            response = self.submit(**payload)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("secret-value", response.text)
        for headers in ({}, {"Idempotency-Key": "spaces invalid"}, {"Idempotency-Key": "a" * 101}):
            response = self.client.post("/tasks", json=self.task.request.model_dump(), headers=headers)
            self.assertEqual(response.status_code, 422)
        self.store.submit.assert_not_called()
        self.snapshots.read.assert_not_called()

    def test_snapshot_errors_do_not_enqueue(self):
        for exc, status, code in [(FileNotFoundError("private-path"), 404, "snapshot_not_found"),
                                  (ValueError("private-path"), 422, "snapshot_invalid"),
                                  (PermissionError("private-path"), 503, "snapshot_unavailable")]:
            self.snapshots.read.side_effect = exc
            response = self.submit()
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json(), {"error": {"code": code}})
        self.store.submit.assert_not_called()

    def test_conflicts_and_database_failure_are_structured(self):
        for exc, status, code in [(TaskConflict("idempotency_key_conflict"), 409, "idempotency_key_conflict"),
                                  (psycopg.OperationalError("password=secret"), 503, "database_unavailable")]:
            self.store.submit.side_effect = exc
            response = self.submit()
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json(), {"error": {"code": code}})

    def test_unknown_task_and_invalid_uuid(self):
        self.store.get.side_effect = TaskNotFound("task_not_found")
        for method, path in [(self.client.get, f"/tasks/{uuid4()}"),
                             (self.client.post, f"/tasks/{uuid4()}/cancel")]:
            self.assertEqual(method(path).status_code, 404)
        self.assertEqual(self.client.get("/tasks/not-a-uuid").status_code, 422)
        self.store.cancel_queued.assert_not_called()

    def test_cancel_racing_with_claim_returns_conflict(self):
        self.store.cancel_queued.side_effect = TaskConflict("task_not_queued")
        response = self.client.post(f"/tasks/{self.task.id}/cancel")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "task_not_queued")

    def test_factory_does_not_migrate_or_require_model_configuration(self):
        with patch.dict(os.environ, {"TASK_DATABASE_URL": "postgresql://unused"}), \
                patch("backend.tasks.api.TaskStore") as factory:
            create_app(snapshots=self.snapshots)
            factory.return_value.initialize.assert_not_called()


class TaskApiConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_database_call_does_not_block_other_request(self):
        entered, release = threading.Event(), threading.Event()
        slow_id = uuid4()
        task = record()
        store = Mock()

        def get(task_id):
            if task_id == slow_id:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test_release_timeout")
            return task

        store.get.side_effect = get
        app = create_app(store, Mock())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            slow = asyncio.create_task(client.get(f"/tasks/{slow_id}"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                fast = await asyncio.wait_for(client.get(f"/tasks/{task.id}"), 2)
                self.assertEqual(fast.status_code, 200)
                self.assertFalse(slow.done())
            finally:
                release.set()
                await slow


DSN = os.environ.get("TASK_TEST_DATABASE_URL")


@unittest.skipUnless(DSN, "requires TASK_TEST_DATABASE_URL")
class TaskApiDatabaseTests(unittest.TestCase):
    def setUp(self):
        from test_async_hypotheses import setup_snapshot
        self.store = TaskStore(DSN, schema="test_api_" + uuid4().hex)
        self.store.initialize()
        self.addCleanup(self.drop_schema)
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.snapshots, self.key = setup_snapshot(temp)
        self.app = create_app(self.store, self.snapshots)
        self.client = self.enterContext(TestClient(self.app))
        self.payload = {"snapshot_id": self.key}

    def drop_schema(self):
        from psycopg import sql
        with self.store.connection() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.store.schema)))

    def submit(self, key="same-key", payload=None):
        return self.client.post("/tasks", json=payload or self.payload, headers={"Idempotency-Key": key})

    def test_http_submit_worker_result_and_terminal_replay(self):
        from backend.tasks.worker import run_once
        response = self.submit()
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["id"]
        self.assertEqual(self.submit().json()["id"], task_id)
        result = asyncio.run(run_once(self.store, self.snapshots, "api-test-worker"))
        self.assertEqual(str(result.id), task_id)
        fetched = self.client.get(response.headers["location"])
        self.assertEqual(fetched.json()["state"], "completed")
        self.assertEqual(fetched.json()["result"]["snapshot_id"], self.key)
        replay = self.submit()
        self.assertEqual(replay.json(), fetched.json())
        self.assertEqual(replay.json()["attempt_count"], 1)
        self.assertIsNone(self.store.claim("no-duplicate"))
        self.assertEqual(self.client.post(f"/tasks/{task_id}/cancel").status_code, 409)

    def test_parallel_http_idempotency_and_parameter_conflict(self):
        barrier = threading.Barrier(6)

        def submit(_):
            barrier.wait()
            return self.submit()

        with ThreadPoolExecutor(6) as pool:
            responses = list(pool.map(submit, range(6)))
        self.assertEqual([r.status_code for r in responses], [202] * 6)
        self.assertEqual(len({r.json()["id"] for r in responses}), 1)
        self.assertEqual(self.submit(payload=self.payload | {"max_tool_calls": 2}).status_code, 409)
        self.assertIsNotNone(self.store.claim("only-one"))
        self.assertIsNone(self.store.claim("empty"))

    def test_cancel_and_worker_claim_have_only_one_winner(self):
        # 重复竞争只覆盖两种合法结果，不声称测量概率或吞吐量。
        for index in range(8):
            response = self.submit(key=f"race-{index}")
            task_id = response.json()["id"]
            barrier = threading.Barrier(2)

            def cancel():
                barrier.wait()
                return self.client.post(f"/tasks/{task_id}/cancel")

            def claim():
                barrier.wait()
                return self.store.claim("race-worker")

            with ThreadPoolExecutor(2) as pool:
                cancellation, claiming = pool.submit(cancel), pool.submit(claim)
                cancelled, claimed = cancellation.result(), claiming.result()
            if claimed is None:
                self.assertEqual(cancelled.status_code, 200)
                self.assertEqual(self.store.get(task_id).state, "cancelled")
            else:
                self.assertEqual(str(claimed.id), task_id)
                self.assertEqual(cancelled.status_code, 409)
                self.assertEqual(self.store.get(task_id).state, "running")
                self.store.fail(claimed.id, claimed.run_token)
