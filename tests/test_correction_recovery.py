import asyncio
import hashlib
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from backend.tasks.models import TaskRequest
from backend.tasks.store import TaskConflict
from backend.tasks.checkpoints import LeaseCheckpointSaver
from backend.tasks.worker import run_once
import test_graph_recovery as fixtures
from test_window_investigation import finish


@unittest.skipUnless(os.environ.get("TASK_TEST_DATABASE_URL"), "requires TASK_TEST_DATABASE_URL")
class CorrectionRecoveryTests(unittest.TestCase):
    setUp = fixtures.GraphRecoveryTests.setUp
    drop_schema = fixtures.GraphRecoveryTests.drop_schema

    def crash(self, strategy, mode="pending", tokens=None):
        task = self.store.submit(TaskRequest(snapshot_id=self.key, strategy=strategy,
            max_corrections=1, max_total_tokens=tokens), strategy + mode)
        script = '''
import asyncio, os, sys
sys.path.insert(0, 'tests')
from unittest.mock import patch
from backend.tasks.store import TaskStore
from backend.tasks.checkpoints import execute_persistent
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_graph import WindowGraphRun
from backend.investigation.hypothesis_graph import HypothesisGraphRun
from test_window_investigation import finish
from test_hypotheses import query, update
s=TaskStore(os.environ['TASK_TEST_DATABASE_URL'],schema=sys.argv[1])
t=s.claim('crash',30)
mode=sys.argv[3]
agent=t.request.strategy=='agent'
Graph=WindowGraphRun if agent else HypothesisGraphRun
adapter='AsyncWindowChatModel' if agent else 'AsyncHypothesisChatModel'
class Model:
    last_usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}
    calls=0
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    async def decide(self,context):
        self.calls+=1
        if self.calls==1: return {'kind':'bad'}
        if mode=='inflight': os._exit(42)
        if self.calls==2:
            assert context.correction_feedback is not None
            return dict(kind='tool',tool='get_snapshot_trace',trace_id='a'*32) if agent else query([update()])
        end=finish(t.request.snapshot_id,'insufficient_evidence',[])
        return end if agent else {**end,'updates':[],'hypothesis_ids':[]}
original_decide=Graph.decide
original_tool=Graph.tool
async def decide(self,state):
    if mode=='pending' and state.get('pending_feedback'): os._exit(41)
    result=await original_decide(self,state)
    if mode=='result_uncommitted' and state.get('pending_feedback'): os._exit(44)
    return result
async def tool(self,state):
    if mode=='decision': os._exit(43)
    return await original_tool(self,state)
with patch('backend.investigation.model.ModelConfig.from_env'), patch('backend.investigation.async_model.'+adapter,return_value=Model()), patch.object(Graph,'decide',decide), patch.object(Graph,'tool',tool):
    asyncio.run(execute_persistent(s,t,SnapshotStore(sys.argv[2])))
os._exit(45)
'''
        result = subprocess.run([sys.executable, "-c", script, self.store.schema, self.directory, mode],
                                capture_output=True, text=True, timeout=30)
        expected = dict(pending=41, inflight=42, decision=43, result_uncommitted=44, finished=45)
        self.assertEqual(result.returncode, expected[mode], result.stderr)
        with self.store.connection() as conn:
            conn.execute("UPDATE investigation_tasks SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                         (task.id,))
        return task

    def resume(self, strategy, invalid=False):
        contexts = []
        end = finish(self.key, "insufficient_evidence", [])
        if strategy == "hypotheses":
            end = {**end, "updates": [], "hypothesis_ids": []}
        class Model:
            last_usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def decide(self, context):
                contexts.append(context.model_dump(mode="json"))
                return {"kind": "bad"} if invalid else end
        adapter = "AsyncWindowChatModel" if strategy == "agent" else "AsyncHypothesisChatModel"
        with patch("backend.investigation.model.ModelConfig.from_env"), \
                patch("backend.investigation.async_model." + adapter, return_value=Model()):
            saved = asyncio.run(run_once(self.store, self.snapshots, "replacement"))
        return saved, contexts

    def test_pending_feedback_is_restored_and_allowance_is_not_refilled(self):
        for strategy in ("agent", "hypotheses"):
            self.crash(strategy)
            saved, contexts = self.resume(strategy, invalid=True)
            self.assertEqual(saved.error_code, "invalid_action")
            self.assertEqual(len(contexts), 1)
            self.assertEqual(contexts[0]["correction_feedback"]["diagnostic"]["turn"], 1)
            self.assertEqual(saved.result["correction"]["used"], 1)
            self.assertEqual(len(saved.result["correction"]["failures"]), 2)
            self.assertEqual(saved.result["usage_totals"]["model_calls"], 2)

    def test_unknown_correction_request_or_result_is_never_reissued(self):
        for strategy in ("agent", "hypotheses"):
            for mode in ("inflight", "result_uncommitted"):
                self.crash(strategy, mode)
                saved, contexts = self.resume(strategy)
                self.assertEqual(contexts, [])
                self.assertEqual(saved.error_code, "usage_unavailable")
                self.assertEqual(saved.result["correction"]["used"], 1)
                self.assertEqual(len(saved.result["correction"]["failures"]), 1)
                self.assertIsNone(saved.result["usage_totals"]["total_tokens"])

    def test_committed_correction_decision_is_not_replayed(self):
        for strategy in ("agent", "hypotheses"):
            self.crash(strategy, "decision")
            saved, contexts = self.resume(strategy)
            self.assertEqual(saved.state, "completed")
            self.assertEqual(len(contexts), 1)
            self.assertNotIn("correction_feedback", contexts[0])
            self.assertEqual(saved.result["usage_totals"]["model_calls"], 3)
            self.assertEqual(saved.result["usage_totals"]["tool_calls"], 2)
            self.assertEqual(saved.result["correction"]["used"], 1)

    def test_completed_correction_report_is_reconciled_without_model(self):
        for strategy in ("agent", "hypotheses"):
            self.crash(strategy, "finished")
            saved, contexts = self.resume(strategy)
            self.assertEqual(saved.state, "completed")
            self.assertEqual(contexts, [])
            self.assertEqual(saved.result["correction"]["used"], 1)
            self.assertEqual(len(saved.result["correction"]["failures"]), 1)

    def test_token_budget_and_deadline_survive_pending_correction(self):
        for strategy in ("agent", "hypotheses"):
            self.crash(strategy, tokens=6)
            saved, contexts = self.resume(strategy)
            self.assertEqual(saved.error_code, "token_budget_exhausted")
            self.assertEqual(saved.result["token_budget"]["total_tokens"], 6)
            self.assertEqual(len(contexts), 1)
        # A different key avoids reusing the earlier task's idempotency key.
        task = self.crash("agent", "decision")
        with self.store.connection() as conn:
            conn.execute("UPDATE task_graph_runs SET deadline=clock_timestamp()-interval '1 second' WHERE task_id=%s",
                         (task.id,))
        saved, contexts = self.resume("agent")
        self.assertEqual(saved.error_code, "deadline_exceeded")
        self.assertEqual(contexts, [])
        self.assertEqual(saved.result["correction"]["used"], 1)

    def test_protocol_isolation_and_old_idempotency_payload(self):
        for strategy in ("agent", "hypotheses"):
            request = TaskRequest(snapshot_id=self.key, strategy=strategy)
            task = self.store.submit(request, strategy)
            old_payload = request.model_dump(mode="json", exclude={"max_corrections"})
            old_digest = hashlib.sha256(json.dumps(old_payload, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            self.assertEqual(task.request_hash, old_digest)
            self.assertEqual(self.store.submit(request, strategy).id, task.id)
            with self.assertRaises(TaskConflict):
                self.store.submit(request.model_copy(update={"max_corrections": 1}), strategy)
            claimed = self.store.claim("owner")
            saver = LeaseCheckpointSaver(self.store, claimed)
            self.assertEqual(saver.start()["protocol"], strategy + "-graph-v1")
            altered = claimed.model_copy(update={"request": request.model_copy(update={"max_corrections": 1})})
            with self.assertRaisesRegex(TaskConflict, "graph_protocol_or_request_mismatch"):
                LeaseCheckpointSaver(self.store, altered).start()
            self.store.fail(claimed.id, claimed.run_token)
