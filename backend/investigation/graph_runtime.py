"""串行调查图的共享调用、预算与恢复运行环境；领域节点由策略定义。"""

import asyncio
import json
from time import monotonic
from typing import Any, TypedDict

from langsmith import tracing_context
from langgraph.errors import NodeCancelledError

from .agent import ModelFailure
from .budget import InvestigationDeadlineExceeded
from .models import Evidence
from .usage import Usage, CallUsage, token_usage
from .window_investigation import RepeatedQueryDetected


class InvestigationGraphState(TypedDict):
    schema_version: int
    snapshot_id: str
    evidence: list[dict]
    usage: dict
    turn: int
    raw: Any
    prepared: dict | None
    report: dict | None


class SerialGraphRun:
    """每次运行独占；内存领域状态仅用于节点执行和取消部分报告。"""

    nodes_per_turn = 3

    def __init__(self, domain, model, budget, tokens, *, checkpointer=None, uncertain_turn=None):
        self.domain, self.model, self.budget, self.tokens = domain, model, budget, tokens
        self.checkpointer, self.uncertain_turn = checkpointer, uncertain_turn

    def export(self):
        return dict(evidence=[e.model_dump(mode="json") for e in self.domain.session.evidence],
                    usage=self.domain.session.usage.model_dump(mode="json"))

    def restore(self, state):
        if state["schema_version"] != 1 or state["snapshot_id"] != self.domain.session.snapshot_id:
            raise ValueError("graph_state_binding_mismatch")
        self.domain.session.evidence = [Evidence.model_validate_json(json.dumps(e)) for e in state["evidence"]]
        self.domain.session.usage = Usage.model_validate(state["usage"])

    def report(self, **kwargs):
        result = self.domain.report(**kwargs)
        result.token_budget = self.tokens.snapshot(self.domain.session.usage)
        return result

    def output(self, result=None, **updates):
        if result is not None:
            result.token_budget = self.tokens.snapshot(self.domain.session.usage)
        return {**self.export(), "report": result.model_dump(mode="json") if result else None, **updates}

    def token_stop(self):
        status = self.tokens.snapshot(self.domain.session.usage)
        if status is not None and status.status == "unknown":
            return self.report(termination="usage_unavailable", reason="累计token用量不完整，停止继续调查。")
        if status is not None and status.status == "exhausted":
            return self.report(termination="token_budget_exhausted", reason="累计token预算已达到，停止执行本次动作。")

    async def decide(self, state):
        self.restore(state)
        self.budget.check()
        stopped = self.token_stop()
        if stopped is not None:
            return self.output(stopped)
        if state["turn"] >= self.domain.session.max_tool_calls:
            return self.output(self.report(termination="budget_exhausted", reason="模型调用预算耗尽。"))
        if self.checkpointer is not None:
            await asyncio.to_thread(self.checkpointer.begin_call, state["turn"] + 1)
        started, status, error, raw = monotonic(), "ok", None, None
        try:
            raw = await self.model.decide(self.domain.context())
        except asyncio.CancelledError:
            status, error = "cancelled", "call_cancelled"
            raise
        except ModelFailure as exc:
            status, error = "error", exc.code
        except Exception:
            status, error = "error", "adapter_error"
        finally:
            # 取消中的节点未返回图更新，但已经发生的调用仍进入部分报告。
            self.domain.session.usage.calls.append(CallUsage(kind="model", name="decide", status=status,
                error_code=error, elapsed_ms=(monotonic() - started) * 1000,
                **token_usage(getattr(self.model, "last_usage", None))))
        await asyncio.sleep(0)
        self.budget.check()
        if status == "error":
            return self.output(self.report(termination="model_error", reason="模型调用失败，保留已有证据。"))
        stopped = self.token_stop()
        return self.output(stopped, raw=None if stopped else raw, prepared=None, turn=state["turn"] + 1)

    async def run(self):
        from .async_investigation import InvestigationCancelled
        try:
            config = {"recursion_limit": self.nodes_per_turn * self.domain.session.max_tool_calls + 2, "callbacks": []}
            checkpoint = None
            if self.checkpointer is not None:
                config.update(self.checkpointer.config)
                checkpoint = await self.checkpointer.aget_tuple(config)
                if checkpoint:
                    values = checkpoint.checkpoint["channel_values"]
                    saved = values if "schema_version" in values else values.get("__start__")
                    if saved:
                        self.restore(saved)
                        if saved.get("report") is not None:
                            return self.report_type.model_validate_json(json.dumps(saved["report"]))
                if self.uncertain_turn is not None:
                    self.domain.session.usage.calls.append(CallUsage(kind="model", name="decide",
                        status="error", error_code="interrupted_call_usage_unknown", elapsed_ms=0))
                    return self.report(termination="usage_unavailable",
                                       reason="上次模型调用结果未可靠落库，停止自动重发；用量未知。")
            self.budget.check()
            async with asyncio.timeout(self.budget.remaining_seconds()):
                if self.domain.session._payload is None:
                    return self.report(termination="source_error", reason="快照无法读取或无效。")
                initial = {**self.export(), "schema_version": 1, "snapshot_id": self.domain.session.snapshot_id,
                           "turn": 0, "raw": None, "prepared": None, "report": None}
                # 明确禁用框架远程追踪，避免继承环境变量后上传证据。
                with tracing_context(enabled=False):
                    result = await self.compile().ainvoke(None if checkpoint else initial,
                        config=config, **({"durability": "sync"} if self.checkpointer is not None else {}))
                return self.report_type.model_validate_json(json.dumps(result["report"]))
        except RepeatedQueryDetected as exc:
            result = self.report(termination="repeated_query", reason="重复读取同一冻结Trace不会增加证据，停止调查。")
            result.repeated_query = exc.detail.model_copy(deep=True)
            return result
        except (TimeoutError, InvestigationDeadlineExceeded):
            return self.report(termination="deadline_exceeded", reason="调查总时限耗尽，已停止本地等待并保留证据。")
        except (asyncio.CancelledError, NodeCancelledError) as exc:
            raise InvestigationCancelled(self.report(termination="cancelled", reason="调查被取消，保留已有证据。")) from exc
