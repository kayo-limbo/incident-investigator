"""可取消的窗口调查。外部取消保留 CancelledError 语义并携带部分报告。"""

import asyncio
from time import monotonic

from .agent import ModelFailure
from .budget import InvestigationBudget, InvestigationDeadlineExceeded, ObservedTokenBudget
from .usage import CallUsage, token_usage
from .window_investigation import FrozenWindowSession, RepeatedQueryDetected, apply_window_action
from .hypothesis_agent import HypothesisInvestigationState


class InvestigationCancelled(asyncio.CancelledError):
    def __init__(self, report):
        super().__init__("investigation_cancelled")
        self.report = report


async def investigate_window_async(store, snapshot_id, model, max_tool_calls=6, *,
                                   timeout_seconds=120, max_total_tokens=None, repeat_query_policy="stop"):
    budget = InvestigationBudget(timeout_seconds)
    tokens = ObservedTokenBudget(max_total_tokens)
    session = FrozenWindowSession(store, snapshot_id, max_tool_calls, repeat_query_policy=repeat_query_policy)
    return await _run_async(WindowInvestigationState(session), model, budget, tokens)


async def investigate_hypotheses_async(store, snapshot_id, model, max_tool_calls=6, *,
                                      timeout_seconds=120, max_total_tokens=None, repeat_query_policy="stop"):
    budget = InvestigationBudget(timeout_seconds)
    tokens = ObservedTokenBudget(max_total_tokens)
    session = FrozenWindowSession(store, snapshot_id, max_tool_calls, repeat_query_policy=repeat_query_policy)
    state = HypothesisInvestigationState(session, "window_hypotheses_async_v1")
    return await _run_async(state, model, budget, tokens)


class WindowInvestigationState:
    def __init__(self, session):
        self.session = session
        self.strategy = "window_react_async_v1"

    def context(self):
        return self.session.context()

    def report(self, **kwargs):
        return self.session.report(self.strategy, **kwargs)

    def apply(self, raw, turn, budget):
        return apply_window_action(self.session, raw, self.strategy, budget)


async def _run_async(state, model, budget, tokens):
    """共用调度和停止语义；策略状态负责上下文、动作及部分报告。"""
    session = state.session

    def annotate(report):
        report.token_budget = tokens.snapshot(session.usage)
        return report

    def report(**kwargs):
        return annotate(state.report(**kwargs))

    def token_stop():
        snapshot = tokens.snapshot(session.usage)
        if snapshot is not None and snapshot.status == "unknown":
            return report(termination="usage_unavailable", reason="累计token用量不完整，停止继续调查。")
        if snapshot is not None and snapshot.status == "exhausted":
            return report(termination="token_budget_exhausted", reason="累计token预算已达到，停止执行本次动作。")
        return None

    try:
        budget.check()
        # 只创建一次计时范围；模型/工具各轮共享剩余总时限。
        async with asyncio.timeout(budget.remaining_seconds()):
            if session._payload is None:
                return report(termination="source_error", reason="快照无法读取或无效。")
            for turn in range(1, session.max_tool_calls + 1):
                await asyncio.sleep(0)  # 同步下钻之间也提供取消检查点。
                budget.check()
                stopped = token_stop()
                if stopped is not None:
                    return stopped
                started, status, error = monotonic(), "ok", None
                try:
                    raw = await model.decide(state.context())
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
                    return report(termination="model_error", reason="模型调用失败，保留已有证据。")
                stopped = token_stop()
                if stopped is not None:
                    return stopped
                result = state.apply(raw, turn, budget)
                if result is not None:
                    return annotate(result)
            raise AssertionError("unreachable")
    except RepeatedQueryDetected as exc:
        stopped = report(termination="repeated_query", reason="重复读取同一冻结Trace不会增加证据，停止调查。")
        stopped.repeated_query = exc.detail.model_copy(deep=True)
        return stopped
    except (TimeoutError, InvestigationDeadlineExceeded):
        return report(termination="deadline_exceeded",
                              reason="调查总时限耗尽，已停止本地等待并保留证据。")
    except asyncio.CancelledError as exc:
        # 不在库层吞掉取消，调用者仍能观察 task.cancelled()。
        partial = report(termination="cancelled", reason="调查被取消，保留已有证据。")
        raise InvestigationCancelled(partial) from exc
