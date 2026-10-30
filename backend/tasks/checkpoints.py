"""针对串行假设图的检查点适配器；任务行锁使租约验证与写入原子化。"""

import asyncio
from contextlib import contextmanager
from time import monotonic

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from psycopg.types.json import Jsonb

from .store import TaskConflict


class LeaseCheckpointSaver(BaseCheckpointSaver):
    PROTOCOL = "hypotheses-graph-v1"

    def __init__(self, store, task):
        super().__init__()
        self.store, self.task = store, task
        self.config = {"configurable": {"thread_id": str(task.id), "checkpoint_ns": ""}}

    def validate_config(self, config):
        values = config["configurable"]
        if values["thread_id"] != str(self.task.id) or values.get("checkpoint_ns", "") != "":
            raise TaskConflict("graph_identity_mismatch")

    @contextmanager
    def owned(self):
        with self.store.connection() as conn:
            conn.execute("SELECT id FROM investigation_tasks WHERE id=%s FOR UPDATE", (self.task.id,))
            def check():
                if not conn.execute("""SELECT id FROM investigation_tasks WHERE id=%s AND run_token=%s
                    AND state='running' AND lease_expires_at>clock_timestamp()""",
                    (self.task.id, self.task.run_token)).fetchone():
                    raise TaskConflict("task_lease_lost")
            check()
            yield conn
            check()

    def start(self):
        with self.owned() as conn:
            conn.execute("""INSERT INTO task_graph_runs(task_id, protocol, request_hash, deadline)
                VALUES (%s,%s,%s,clock_timestamp()+%s*interval '1 second') ON CONFLICT DO NOTHING""",
                (self.task.id, self.PROTOCOL, self.task.request_hash, self.task.request.timeout_seconds))
            row = conn.execute("""SELECT *, EXTRACT(EPOCH FROM deadline-clock_timestamp()) AS remaining
                FROM task_graph_runs WHERE task_id=%s""", (self.task.id,)).fetchone()
            if row["protocol"] != self.PROTOCOL or row["request_hash"] != self.task.request_hash:
                raise TaskConflict("graph_protocol_or_request_mismatch")
            return row

    def begin_call(self, turn):
        with self.owned() as conn:
            row = conn.execute("""UPDATE task_graph_runs SET inflight_turn=%s
                WHERE task_id=%s AND inflight_turn IS NULL RETURNING task_id""", (turn, self.task.id)).fetchone()
            if row is None:
                raise TaskConflict("graph_call_already_inflight")

    def get_tuple(self, config):
        self.validate_config(config)
        with self.owned() as conn:
            checkpoint_id = config["configurable"].get("checkpoint_id")
            if checkpoint_id:
                row = conn.execute("SELECT * FROM task_graph_checkpoints WHERE task_id=%s AND checkpoint_id=%s",
                                   (self.task.id, checkpoint_id)).fetchone()
            else:
                row = conn.execute("""SELECT * FROM task_graph_checkpoints WHERE task_id=%s
                    ORDER BY position DESC LIMIT 1""", (self.task.id,)).fetchone()
        if row is None:
            return None
        def config_for(key):
            return {"configurable": {**self.config["configurable"], "checkpoint_id": key}}
        return CheckpointTuple(config_for(row["checkpoint_id"]), row["checkpoint"], row["metadata"],
                               config_for(row["parent_id"]) if row["parent_id"] else None, [])

    def put(self, config, checkpoint, metadata, new_versions):
        self.validate_config(config)
        with self.owned() as conn:
            conn.execute("""INSERT INTO task_graph_checkpoints(task_id,checkpoint_id,parent_id,checkpoint,metadata)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (self.task.id, checkpoint["id"], config["configurable"].get("checkpoint_id"),
                 Jsonb(checkpoint), Jsonb(metadata)))
            # 调用结果/用量和清除未确认标记必须同事务提交。
            calls = checkpoint.get("channel_values", {}).get("usage", {}).get("calls", [])
            count = sum(call["kind"] == "model" for call in calls)
            conn.execute("""UPDATE task_graph_runs SET inflight_turn=NULL
                WHERE task_id=%s AND inflight_turn<=%s""", (self.task.id, count))
        return {"configurable": {**self.config["configurable"], "checkpoint_id": checkpoint["id"]}}

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        # 本串行图仅恢复完整super-step；不保存pending writes，不支持并行分支/子图。
        # 模型调用跨步骤的不确定性由inflight_turn保守阻止自动重发。
        self.validate_config(config)


async def execute_persistent(store, task, snapshots):
    from contextlib import AsyncExitStack
    from backend.investigation.async_model import AsyncHypothesisChatModel
    from backend.investigation.model import ModelConfig
    from backend.investigation.budget import InvestigationBudget, ObservedTokenBudget
    from backend.investigation.window_investigation import FrozenWindowSession
    from backend.investigation.hypothesis_agent import HypothesisInvestigationState
    from backend.investigation.hypothesis_graph import HypothesisGraphRun

    saver = LeaseCheckpointSaver(store, task)
    started = monotonic()
    run_info = await asyncio.to_thread(saver.start)
    # 首次执行后预算不重置，停机时间算入；已到期用极小正数进入标准超时报表路径。
    budget = InvestigationBudget(max(float(run_info["remaining"]) - (monotonic() - started), 1e-12))
    request = task.request
    session = FrozenWindowSession(snapshots, request.snapshot_id, request.max_tool_calls,
                                  repeat_query_policy=request.repeat_query_policy)
    domain = HypothesisInvestigationState(session, "window_hypotheses_langgraph_v1")
    async with AsyncExitStack() as stack:
        class LazyModel:
            adapter = None
            last_usage = None

            async def decide(self, context):
                if self.adapter is None:
                    self.adapter = await stack.enter_async_context(AsyncHypothesisChatModel(ModelConfig.from_env()))
                try:
                    return await self.adapter.decide(context)
                finally:
                    self.last_usage = self.adapter.last_usage

        return await HypothesisGraphRun(domain, LazyModel(), budget,
            ObservedTokenBudget(request.max_total_tokens), checkpointer=saver,
            uncertain_turn=run_info["inflight_turn"]).run()
