import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.tasks.models import TaskRequest
from backend.tasks.store import TaskStore, TaskConflict
from backend.tasks.checkpoints import LeaseCheckpointSaver
from backend.tasks.worker import run_once


@unittest.skipUnless(os.environ.get("TASK_TEST_DATABASE_URL"), "requires TASK_TEST_DATABASE_URL")
class GraphRecoveryTests(unittest.TestCase):
    def setUp(self):
        from test_async_hypotheses import setup_snapshot
        self.store = TaskStore(os.environ["TASK_TEST_DATABASE_URL"], schema="test_graph_" + uuid4().hex)
        self.store.initialize()
        self.addCleanup(self.drop_schema)
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.snapshots, self.key = setup_snapshot(self.directory)

    def drop_schema(self):
        from psycopg import sql
        with self.store.connection() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.store.schema)))

    def crash(self, mode="tool", tokens=None):
        request = TaskRequest(snapshot_id=self.key, strategy="hypotheses", max_total_tokens=tokens)
        task = self.store.submit(request, "crash")
        script = '''
import asyncio,os,sys
sys.path.insert(0,'tests')
from unittest.mock import patch
from backend.tasks.store import TaskStore
from backend.tasks.checkpoints import execute_persistent
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.hypothesis_graph import HypothesisGraphRun
from test_hypotheses import query,update
from test_window_investigation import finish
s=TaskStore(os.environ['TASK_TEST_DATABASE_URL'],schema=sys.argv[1])
t=s.claim('crashed',30)
mode=sys.argv[3]
class Model:
    last_usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}
    calls=0
    async def __aenter__(self): return self
    async def __aexit__(self,*a): pass
    async def decide(self,context):
        self.calls+=1
        if mode=='inflight': os._exit(31)
        if self.calls==1: return query([update()])
        ref=dict(snapshot_id=t.request.snapshot_id,trace_id='a'*32,span_id='3',field='duration_ms')
        return {**finish(t.request.snapshot_id,'inventory_latency',[ref]),
            'updates':[{k:v for k,v in update(version=1,status='supported',support=[ref]).items() if k!='expected_version'}],
            'hypothesis_ids':['slow']}
original=HypothesisGraphRun.tool
async def tool(self,state):
    if mode=='tool': os._exit(32)
    return await original(self,state)
with patch('backend.investigation.model.ModelConfig.from_env'), patch('backend.investigation.async_model.AsyncHypothesisChatModel',return_value=Model()), patch.object(HypothesisGraphRun,'tool',tool):
    asyncio.run(execute_persistent(s,t,SnapshotStore(sys.argv[2])))
os._exit(33)
'''
        result = subprocess.run([sys.executable, "-c", script, self.store.schema, self.directory, mode],
                                timeout=25, capture_output=True, text=True)
        self.assertEqual(result.returncode, {"tool": 32, "inflight": 31, "finished": 33}[mode], result.stderr)
        old = self.store.get(task.id)
        with self.store.connection() as conn:
            conn.execute("UPDATE investigation_tasks SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                         (task.id,))
        return old

    def resume(self):
        from test_hypotheses import update
        from test_window_investigation import finish
        key = self.key
        class Model:
            calls = 0
            last_usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def decide(self, context):
                self.calls += 1
                ref = dict(snapshot_id=key, trace_id="a" * 32, span_id="3", field="duration_ms")
                return {**finish(key, "inventory_latency", [ref]),
                    "updates": [{k: v for k, v in update(version=1, status="supported", support=[ref]).items()
                                 if k != "expected_version"}], "hypothesis_ids": ["slow"]}
        model = Model()
        with patch("backend.investigation.model.ModelConfig.from_env"), \
                patch("backend.investigation.async_model.AsyncHypothesisChatModel", return_value=model):
            saved = asyncio.run(run_once(self.store, self.snapshots, "replacement"))
        return saved, model.calls

    def test_process_crash_resumes_after_committed_step(self):
        old = self.crash()
        saved, calls = self.resume()
        self.assertEqual(saved.state, "completed", saved.error_code)
        self.assertEqual(saved.attempt_count, 2)
        self.assertEqual(calls, 1)
        self.assertEqual(saved.result["usage_totals"]["model_calls"], 2)
        self.assertEqual(saved.result["usage_totals"]["total_tokens"], 6)
        self.assertEqual(saved.result["usage_totals"]["drilldown_calls"], 1)
        self.assertNotEqual(old.run_token, saved.run_token)

    def test_uncertain_model_call_is_not_automatically_reissued(self):
        self.crash("inflight")
        saved, calls = self.resume()
        self.assertEqual(calls, 0)
        self.assertEqual(saved.error_code, "usage_unavailable")
        self.assertIsNone(saved.result["usage_totals"]["total_tokens"])
        self.assertEqual(saved.result["usage_totals"]["unknown_usage_calls"], 1)

    def test_token_budget_accumulates_across_recovery(self):
        self.crash(tokens=6)
        saved, calls = self.resume()
        self.assertEqual(calls, 1)
        self.assertEqual(saved.error_code, "token_budget_exhausted")
        self.assertEqual(saved.result["token_budget"]["total_tokens"], 6)

    def test_completed_checkpoint_is_reconciled_without_model(self):
        self.crash("finished")
        saved, calls = self.resume()
        self.assertEqual(saved.state, "completed")
        self.assertEqual(calls, 0)

    def test_expired_budget_retains_previous_state_without_model(self):
        old = self.crash()
        with self.store.connection() as conn:
            conn.execute("UPDATE task_graph_runs SET deadline=clock_timestamp()-interval '1 second' WHERE task_id=%s",
                         (old.id,))
        saved, calls = self.resume()
        self.assertEqual(calls, 0)
        self.assertEqual(saved.error_code, "deadline_exceeded")
        self.assertEqual(saved.result["usage_totals"]["model_calls"], 1)
        self.assertEqual(saved.result["hypotheses"][0]["version"], 1)

    def test_stale_owner_cannot_write_checkpoint(self):
        old = self.crash()
        self.store.recover_expired()
        new = self.store.claim("replacement")
        current = LeaseCheckpointSaver(self.store, new)
        before = current.get_tuple(current.config)
        stale = LeaseCheckpointSaver(self.store, old)
        with self.assertRaises(TaskConflict):
            stale.put(stale.config, before.checkpoint, before.metadata, {})
        with self.assertRaises(TaskConflict):
            stale.begin_call(2)
        self.assertEqual(current.get_tuple(current.config).checkpoint, before.checkpoint)

    def test_lease_loss_before_commit_rolls_back_writes(self):
        task = self.store.submit(TaskRequest(snapshot_id=self.key, strategy="hypotheses"), "rollback")
        task = self.store.claim("owner")
        saver = LeaseCheckpointSaver(self.store, task)
        saver.start()
        with self.assertRaises(TaskConflict):
            with saver.owned() as conn:
                conn.execute("UPDATE task_graph_runs SET inflight_turn=1 WHERE task_id=%s", (task.id,))
                conn.execute("UPDATE investigation_tasks SET lease_expires_at=clock_timestamp() WHERE id=%s",
                             (task.id,))
        self.assertIsNone(saver.start()["inflight_turn"])

    def test_incompatible_protocol_is_rejected(self):
        self.store.submit(TaskRequest(snapshot_id=self.key, strategy="hypotheses"), "protocol")
        task = self.store.claim("owner")
        saver = LeaseCheckpointSaver(self.store, task)
        saver.start()
        with self.store.connection() as conn:
            conn.execute("UPDATE task_graph_runs SET protocol='future-version' WHERE task_id=%s", (task.id,))
        with self.assertRaisesRegex(TaskConflict, "graph_protocol_or_request_mismatch"):
            saver.start()
