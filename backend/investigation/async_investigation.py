"""可取消的窗口调查。外部取消保留 CancelledError 语义并携带部分报告。"""

import asyncio
from time import monotonic

from .agent import ModelFailure
from .budget import InvestigationBudget, InvestigationDeadlineExceeded
from .usage import CallUsage, token_usage
from .window_investigation import FrozenWindowSession, apply_window_action


class InvestigationCancelled(asyncio.CancelledError):
    def __init__(self, report):
        super().__init__("investigation_cancelled")
        self.report = report


async def investigate_window_async(store, snapshot_id, model, max_tool_calls=6, *,
                                   timeout_seconds=120):
    budget = InvestigationBudget(timeout_seconds)
    session = FrozenWindowSession(store, snapshot_id, max_tool_calls)
    strategy = "window_react_async_v1"
    try:
        budget.check()
        # 只创建一次计时范围；模型/工具各轮共享剩余总时限。
        async with asyncio.timeout(budget.remaining_seconds()):
            if session._payload is None:
                return session.report(strategy, termination="source_error", reason="快照无法读取或无效。")
            for _ in range(max_tool_calls):
                await asyncio.sleep(0)  # 同步下钻之间也提供取消检查点。
                budget.check()
                started, status, error = monotonic(), "ok", None
                try:
                    raw = await model.decide(session.context())
                except asyncio.CancelledError:
                    status, error = "cancelled", "call_cancelled"
                    raise
                except ModelFailure as exc:
                    status, error = "error", exc.code
                except Exception:
                    status, error = "error", "adapter_error"
                finally:
                    session.usage.calls.append(CallUsage(
                        kind="model", name="decide", status=status, error_code=error,
                        elapsed_ms=(monotonic() - started) * 1000,
                        **token_usage(getattr(model, "last_usage", None))))
                # 即使同步解析阻塞了事件循环，迟到动作也不能生效。
                await asyncio.sleep(0)
                budget.check()
                if status == "error":
                    return session.report(strategy, termination="model_error", reason="模型调用失败，保留已有证据。")
                result = apply_window_action(session, raw, strategy, budget)
                if result is not None:
                    return result
            raise AssertionError("unreachable")
    except (TimeoutError, InvestigationDeadlineExceeded):
        return session.report(strategy, termination="deadline_exceeded",
                              reason="调查总时限耗尽，已停止本地等待并保留证据。")
    except asyncio.CancelledError as exc:
        # 不在库层吞掉取消，调用者仍能观察 task.cancelled()。
        report = session.report(strategy, termination="cancelled", reason="调查被取消，保留已有证据。")
        raise InvestigationCancelled(report) from exc
