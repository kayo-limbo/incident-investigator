"""显式假设策略：同一冻结窗口、调用上限及 HTTP 适配器，增加可审查状态。"""

from time import monotonic
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .agent import ModelFailure
from .hypotheses import HypothesisId, HypothesisLedger, HypothesisRevision, HypothesisState, HypothesisUpdate, ShortText
from .usage import CallUsage, token_usage
from .window_investigation import FrozenWindowSession, WindowContext, WindowFinding, WindowReport


class HypothesisAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    updates: list[HypothesisState] = Field(max_length=6)
    hypothesis_ids: list[HypothesisId] = Field(max_length=6)


class HypothesisQuery(HypothesisAction):
    kind: Literal["tool"]
    tool: Literal["get_snapshot_trace"]
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    purpose: ShortText
    hypothesis_ids: list[HypothesisId] = Field(min_length=1, max_length=6)


class HypothesisFinish(HypothesisAction):
    kind: Literal["finish"]
    finding: WindowFinding


HYPOTHESIS_ACTION = TypeAdapter(Annotated[HypothesisQuery | HypothesisFinish, Field(discriminator="kind")])


class QueryTask(BaseModel):
    turn: int
    trace_id: str
    purpose: str
    hypothesis_versions: dict[str, int]
    status: Literal["completed", "failed"]
    evidence_id: str | None = None
    error_code: str | None = None
    cost_tool_calls: int


class HypothesisContext(WindowContext):
    hypotheses: list[HypothesisState]
    query_tasks: list[dict]


class HypothesisReport(WindowReport):
    hypothesis_history: list[HypothesisRevision]
    hypotheses: list[HypothesisRevision]
    query_tasks: list[QueryTask]
    claim_hypothesis_versions: dict[str, int]
    validation_error: str | None = None
    validation_issues: list[str] = Field(default_factory=list)
    rejected_action: HypothesisQuery | HypothesisFinish | None = None


class HypothesisInvestigationState:
    """单调查状态；同步和异步入口共享同一套候选校验与提交规则。"""

    def __init__(self, session, strategy="window_hypotheses_v1"):
        self.session = session
        self.strategy = strategy
        self.ledger = HypothesisLedger()
        self.tasks = []

    def report(self, finding=None, termination="completed", reason="证据不足", error=None, versions=None):
        base = self.session.report(self.strategy, finding, termination, reason)
        return HypothesisReport(**base.model_dump(), hypothesis_history=self.ledger.history,
            hypotheses=list(self.ledger.current.values()), query_tasks=self.tasks,
            claim_hypothesis_versions=versions or {}, validation_error=error).model_copy(deep=True)

    def context(self):
        before = self.ledger.current
        return HypothesisContext(**self.session.context().model_dump(),
            hypotheses=[HypothesisState(**h.model_dump(include=set(HypothesisState.model_fields))) for h in before.values()],
            query_tasks=[{**t.model_dump(exclude={"hypothesis_versions"}), "hypothesis_ids": list(t.hypothesis_versions)}
                         for t in self.tasks]).model_copy(deep=True)

    def apply(self, raw, turn, budget=None):
        session = self.session
        base_versions = {key: h.version for key, h in self.ledger.current.items()}
        try:
            action = HYPOTHESIS_ACTION.validate_python(raw)
        except ValidationError as exc:
            result = self.report(termination="invalid_action", reason="模型动作不符合假设协议。", error="invalid_schema")
            # 仅记录位置与错误类型，不保存供应商原文、input 或上下文值。
            result.validation_issues = [".".join(map(str, issue["loc"])) + ":" + issue["type"]
                                        for issue in exc.errors(include_input=False, include_context=False)][:24]
            return result
        try:
            updates = [HypothesisUpdate(**h.model_dump(), expected_version=base_versions.get(h.id, 0)) for h in action.updates]
            candidate = self.ledger.propose(updates, session, turn)
            versions = candidate.versions(action.hypothesis_ids)
            if isinstance(action, HypothesisFinish):
                session.validate_finding(action.finding)
                versions = candidate.validate_claim(action.finding, action.hypothesis_ids)
            elif session.remaining <= 0:
                return self.report(termination="budget_exhausted", reason="工具调用预算耗尽。")
            elif action.trace_id not in session._payload["traces"]:
                raise ValueError("trace_outside_snapshot")
        except ValueError as exc:
            result = self.report(termination="invalid_finding" if isinstance(action, HypothesisFinish) else "invalid_action",
                            reason="状态或证据引用校验失败，保留上一个有效版本。", error=str(exc))
            if isinstance(action, HypothesisFinish):
                result.rejected_finding = action.finding.model_copy(deep=True)
            result.rejected_action = action.model_copy(deep=True)
            return result
        if budget is not None:
            budget.check()
        if isinstance(action, HypothesisQuery):
            # 整个重复查询动作（包括随附的假设更新）都不提交。
            session.check_repeated_query(action.trace_id)
        self.ledger = candidate
        if isinstance(action, HypothesisFinish):
            return self.report(action.finding, versions=versions)
        task = QueryTask(turn=turn, trace_id=action.trace_id, purpose=action.purpose,
                         hypothesis_versions=versions, status="failed", cost_tool_calls=0)
        self.tasks.append(task)
        try:
            evidence = session.drilldown(action.trace_id)
        except ValueError as exc:
            task.error_code = str(exc)
            return self.report(termination="source_error", reason="下钻失败，保留查询记录。")
        task.status, task.evidence_id, task.cost_tool_calls = "completed", evidence.id, 1
        if budget is not None:
            budget.check()


def investigate_hypotheses(store, snapshot_id, model, max_tool_calls=6):
    session = FrozenWindowSession(store, snapshot_id, max_tool_calls)
    state = HypothesisInvestigationState(session)
    if session._payload is None:
        return state.report(termination="source_error", reason="快照无法读取或无效。")
    for turn in range(1, max_tool_calls + 1):
        started, status, error = monotonic(), "ok", None
        try:
            raw = model.decide(state.context())
        except ModelFailure as exc:
            status, error = "error", exc.code
        except Exception:
            status, error = "error", "adapter_error"
        finally:
            session.usage.calls.append(CallUsage(kind="model", name="decide", status=status,
                elapsed_ms=(monotonic() - started) * 1000, error_code=error,
                **token_usage(getattr(model, "last_usage", None))))
        if status == "error":
            return state.report(termination="model_error", reason="模型调用失败，保留假设与证据。")
        result = state.apply(raw, turn)
        if result is not None:
            return result
    raise AssertionError("unreachable")
