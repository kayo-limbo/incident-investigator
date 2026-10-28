"""一次领取一个任务；事务在调查开始前结束，不自动重试模型调用。"""

import asyncio

from backend.investigation.async_investigation import (
    InvestigationCancelled, investigate_window_async, investigate_hypotheses_async,
)
from backend.investigation.async_model import AsyncWindowChatModel, AsyncHypothesisChatModel
from backend.investigation.model import ModelConfig
from backend.investigation.window_baseline import investigate_window_fixed


class WorkerCancelled(asyncio.CancelledError):
    def __init__(self, task):
        super().__init__("worker_cancelled")
        self.task = task


async def execute(request, snapshots):
    if request.strategy == "fixed":
        return investigate_window_fixed(snapshots, request.snapshot_id, request.max_tool_calls)
    adapter, runner = {
        "agent": (AsyncWindowChatModel, investigate_window_async),
        "hypotheses": (AsyncHypothesisChatModel, investigate_hypotheses_async),
    }[request.strategy]
    async with adapter(ModelConfig.from_env()) as model:
        return await runner(snapshots, request.snapshot_id, model, request.max_tool_calls,
                            timeout_seconds=request.timeout_seconds,
                            max_total_tokens=request.max_total_tokens,
                            repeat_query_policy=request.repeat_query_policy)


async def run_once(store, snapshots, worker_id):
    # 独立单任务进程使用同步短事务；不要直接放入 Web 服务事件循环。
    task = store.claim(worker_id)
    if task is None:
        return None
    try:
        report = await execute(task.request, snapshots)
    except InvestigationCancelled as exc:
        saved = store.finish(task.id, task.run_token, exc.report)
        raise WorkerCancelled(saved) from None
    except asyncio.CancelledError:
        saved = store.fail(task.id, task.run_token, cancelled=True)
        raise WorkerCancelled(saved) from None
    except Exception:
        # 不把 SDK 异常文本（可能包含地址或凭据）写入任务结果。
        return store.fail(task.id, task.run_token)
    # 持久化失败向上传播，不能当作调查失败再次覆盖结果或重新调用模型。
    return store.finish(task.id, task.run_token, report)
