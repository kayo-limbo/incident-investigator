"""共享假设动作阶段；旧循环与图节点复用同一套业务校验。"""

from dataclasses import dataclass
from pydantic import ValidationError
from .hypothesis_agent import HYPOTHESIS_ACTION, HypothesisFinish, HypothesisQuery, HypothesisReport, QueryTask
from .hypotheses import HypothesisLedger, HypothesisUpdate


@dataclass
class PreparedAction:
    action: HypothesisQuery | HypothesisFinish
    candidate: HypothesisLedger
    versions: dict[str, int]
    turn: int


def prepare_action(state, raw, turn, budget=None):
    session = state.session
    base_versions = {key: h.version for key, h in state.ledger.current.items()}
    try:
        action = HYPOTHESIS_ACTION.validate_python(raw)
    except ValidationError as exc:
        result = state.report(termination="invalid_action", reason="模型动作不符合假设协议。", error="invalid_schema")
        # 仅记录位置与错误类型，不保存供应商原文、input 或上下文值。
        result.validation_issues = [".".join(map(str, issue["loc"])) + ":" + issue["type"]
                                    for issue in exc.errors(include_input=False, include_context=False)][:24]
        return result
    try:
        updates = [HypothesisUpdate(**h.model_dump(), expected_version=base_versions.get(h.id, 0)) for h in action.updates]
        candidate = state.ledger.propose(updates, session, turn)
        versions = candidate.versions(action.hypothesis_ids)
        if isinstance(action, HypothesisFinish):
            session.validate_finding(action.finding)
            versions = candidate.validate_claim(action.finding, action.hypothesis_ids)
        elif session.remaining <= 0:
            return state.report(termination="budget_exhausted", reason="工具调用预算耗尽。")
        elif action.trace_id not in session._payload["traces"]:
            raise ValueError("trace_outside_snapshot")
    except ValueError as exc:
        result = state.report(termination="invalid_finding" if isinstance(action, HypothesisFinish) else "invalid_action",
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
    return PreparedAction(action, candidate, versions, turn)


def commit_action(state, prepared, budget=None):
    # 图调度在校验和接纳之间也可能到期；不能提交迟到候选。
    if budget is not None:
        budget.check()
    action = prepared.action
    if isinstance(action, HypothesisQuery):
        state.session.check_repeated_query(action.trace_id)
    state.ledger = prepared.candidate
    if isinstance(action, HypothesisFinish):
        return state.report(action.finding, versions=prepared.versions)
    state.tasks.append(QueryTask(turn=prepared.turn, trace_id=action.trace_id, purpose=action.purpose,
        hypothesis_versions=prepared.versions, status="failed", cost_tool_calls=0))


def execute_query(state, prepared, budget=None):
    if budget is not None:
        budget.check()
    task = state.tasks[-1]
    try:
        evidence = state.session.drilldown(prepared.action.trace_id)
    except ValueError as exc:
        task.error_code = str(exc)
        return state.report(termination="source_error", reason="下钻失败，保留查询记录。")
    task.status, task.evidence_id, task.cost_tool_calls = "completed", evidence.id, 1
    if budget is not None:
        budget.check()
