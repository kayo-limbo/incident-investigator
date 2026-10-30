"""假设调查的LangGraph编排；图状态是数据，客户端和时钟只存在于单次运行环境。"""

import asyncio
import json
from time import monotonic
from typing import Any, TypedDict

from langgraph.graph import StateGraph, START, END
from langsmith import tracing_context

from .agent import ModelFailure
from .budget import InvestigationDeadlineExceeded
from .hypotheses import HypothesisLedger
from .hypothesis_agent import HYPOTHESIS_ACTION, HypothesisReport, QueryTask
from .hypothesis_steps import PreparedAction, prepare_action, commit_action, execute_query
from .models import Evidence
from .usage import Usage, CallUsage, token_usage
from .window_investigation import RepeatedQueryDetected


class GraphState(TypedDict):
    schema_version: int
    snapshot_id: str
    evidence: list[dict]
    usage: dict
    history: list[dict]
    queries: list[dict]
    turn: int
    raw: Any
    prepared: dict | None
    report: dict | None


class HypothesisGraphRun:
    """每次调查独占；latest仅用于取消时生成部分报告，不是持久化检查点。"""

    def __init__(self, domain, model, budget, tokens):
        self.domain, self.model, self.budget, self.tokens = domain, model, budget, tokens

    def export(self):
        return dict(evidence=[e.model_dump(mode="json") for e in self.domain.session.evidence],
                    usage=self.domain.session.usage.model_dump(mode="json"),
                    history=[h.model_dump(mode="json") for h in self.domain.ledger.history],
                    queries=[q.model_dump(mode="json") for q in self.domain.tasks])

    def restore(self, state):
        if state["schema_version"] != 1 or state["snapshot_id"] != self.domain.session.snapshot_id:
            raise ValueError("graph_state_binding_mismatch")
        self.domain.session.evidence = [Evidence.model_validate_json(json.dumps(e)) for e in state["evidence"]]
        self.domain.session.usage = Usage.model_validate(state["usage"])
        self.domain.ledger = HypothesisLedger.from_history(state["history"])
        self.domain.tasks = [QueryTask.model_validate(q) for q in state["queries"]]

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

    async def validate(self, state):
        self.restore(state)
        self.budget.check()
        prepared = prepare_action(self.domain, state["raw"], state["turn"], self.budget)
        if isinstance(prepared, HypothesisReport):
            return self.output(prepared, raw=None, prepared=None)
        data = {"action": prepared.action.model_dump(mode="json"),
                "history": [h.model_dump(mode="json") for h in prepared.candidate.history],
                "versions": prepared.versions, "turn": prepared.turn}
        return self.output(raw=None, prepared=data)

    def prepared(self, state):
        item = state["prepared"]
        return PreparedAction(HYPOTHESIS_ACTION.validate_python(item["action"]),
                              HypothesisLedger.from_history(item["history"]), item["versions"], item["turn"])

    async def commit(self, state):
        self.restore(state)
        return self.output(commit_action(self.domain, self.prepared(state), self.budget))

    async def tool(self, state):
        self.restore(state)
        return self.output(execute_query(self.domain, self.prepared(state), self.budget), prepared=None)

    def compile(self):
        graph = StateGraph(GraphState)
        for name, node in (("decide", self.decide), ("validate", self.validate),
                           ("commit", self.commit), ("tool", self.tool)):
            graph.add_node(name, node)
        graph.add_edge(START, "decide")
        for source, target in (("decide", "validate"), ("validate", "commit"),
                               ("commit", "tool"), ("tool", "decide")):
            graph.add_conditional_edges(source, lambda state: "end" if state["report"] is not None else "next",
                                        {"end": END, "next": target})
        return graph.compile()  # 本阶段不配置checkpointer或自动节点重试。

    async def run(self):
        from .async_investigation import InvestigationCancelled
        try:
            self.budget.check()
            async with asyncio.timeout(self.budget.remaining_seconds()):
                if self.domain.session._payload is None:
                    return self.report(termination="source_error", reason="快照无法读取或无效。")
                initial = {**self.export(), "schema_version": 1, "snapshot_id": self.domain.session.snapshot_id,
                           "turn": 0, "raw": None, "prepared": None, "report": None}
                # 明确禁用框架远程追踪，避免继承环境变量后上传证据。
                with tracing_context(enabled=False):
                    result = await self.compile().ainvoke(initial,
                        config={"recursion_limit": 4 * self.domain.session.max_tool_calls + 2, "callbacks": []})
                return HypothesisReport.model_validate_json(json.dumps(result["report"]))
        except RepeatedQueryDetected as exc:
            result = self.report(termination="repeated_query", reason="重复读取同一冻结Trace不会增加证据，停止调查。")
            result.repeated_query = exc.detail.model_copy(deep=True)
            return result
        except (TimeoutError, InvestigationDeadlineExceeded):
            return self.report(termination="deadline_exceeded", reason="调查总时限耗尽，已停止本地等待并保留证据。")
        except asyncio.CancelledError as exc:
            raise InvestigationCancelled(self.report(termination="cancelled", reason="调查被取消，保留已有证据。")) from exc
