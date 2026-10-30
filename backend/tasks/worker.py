"""一次领取一个任务；短事务、租约心跳与过期尝试有限接管。"""

import asyncio
import psycopg

from .store import TaskConflict, validate_label, validate_lease

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


async def execute_with_lease(store, task, snapshots, lease_seconds):
    async def heartbeat():
        while True:
            await asyncio.sleep(lease_seconds / 3)
            store.renew(task.id, task.run_token, lease_seconds)

    if task.request.strategy == "hypotheses":
        from .checkpoints import execute_persistent
        execution = asyncio.create_task(execute_persistent(store, task, snapshots))
    else:
        execution = asyncio.create_task(execute(task.request, snapshots))
    renewal = asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait((execution, renewal), return_when=asyncio.FIRST_COMPLETED)
        # 续租失败优先处理，不再信任该尝试的执行结果。
        if renewal in done:
            renewal.result()
        return execution.result()
    except asyncio.CancelledError:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        # gather 会把已取消 Task 的异常转成基础 CancelledError；从 Task 取回原报告。
        try:
            execution.result()
        except InvestigationCancelled:
            raise
        except BaseException:
            pass
        raise
    finally:
        execution.cancel()
        renewal.cancel()
        await asyncio.gather(execution, renewal, return_exceptions=True)


async def run_once(store, snapshots, worker_id, lease_seconds=30):
    # 独立单任务进程使用同步短事务；不要直接放入 Web 服务事件循环。
    validate_lease(lease_seconds)
    validate_label(worker_id)
    store.recover_expired()
    task = store.claim(worker_id, lease_seconds)
    if task is None:
        return None
    try:
        report = await execute_with_lease(store, task, snapshots, lease_seconds)
    except (TaskConflict, psycopg.Error):
        # 失去所有权或数据库不可用：停止本地执行，不冒险覆盖或重新提交失败结果。
        raise
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
