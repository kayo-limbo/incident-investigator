"""受限调查执行器；模型适配器只接收证据快照，不能直接执行工具。"""

from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .models import Evidence, Finding, Investigation, TraceQuery
from .tools import EvidenceTools


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["tool"]
    tool: Literal["get_trace", "get_service_topology", "get_runtime_config", "probe_inventory"]
    # 本阶段所有目标均由执行器绑定，模型不能指定 URL、命令或其他 Trace ID。


class FinalFinding(Finding):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(max_length=100)
    limitations: list[str] = Field(max_length=20)


class FinishAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["finish"]
    finding: FinalFinding


ACTION = TypeAdapter(Annotated[ToolAction | FinishAction, Field(discriminator="kind")])


class DecisionContext(BaseModel):
    trace_id: str
    evidence: list[Evidence]
    remaining_tool_calls: int


class DecisionModel(Protocol):
    def decide(self, context: DecisionContext) -> dict:
        """返回符合 ACTION.json_schema() 的动作；不得接收实验真值。"""
        ...


def investigate_agent(trace_id: str, model: DecisionModel,
                      tools: EvidenceTools | None = None,
                      max_tool_calls: int = 6) -> Investigation:
    TraceQuery(trace_id=trace_id)
    if type(max_tool_calls) is not int or not 0 <= max_tool_calls <= 100:
        raise ValueError("max_tool_calls 必须为 0 到 100 的整数")
    tools = tools or EvidenceTools()
    evidence: list[Evidence] = []
    dispatch = {
        "get_trace": lambda: tools.get_trace(trace_id),
        "get_service_topology": tools.get_service_topology,
        "get_runtime_config": tools.get_runtime_config,
        "probe_inventory": tools.probe_inventory,
    }

    def report(finding: Finding) -> Investigation:
        return Investigation(trace_id=trace_id, strategy="bounded_agent_v1",
                             evidence=evidence, finding=finding)

    def stop(reason: str) -> Investigation:
        return report(Finding(code="insufficient_evidence", summary=reason,
                              evidence_ids=[item.id for item in evidence],
                              limitations=["执行器未验证模型诊断的语义正确性。单次请求不代表系统整体状态。"] ))

    # 最后一个模型回合仅允许结束，确保用完工具预算后仍能提交结论。
    for used in range(max_tool_calls + 1):
        context = DecisionContext(trace_id=trace_id, evidence=evidence,
                                  remaining_tool_calls=max_tool_calls - used)
        try:
            raw = model.decide(context.model_copy(deep=True))
        except Exception:
            # 适配器异常可能含请求头和凭据，不能将原文写入调查结果。
            return stop("模型适配器调用失败。")
        try:
            action = ACTION.validate_python(raw)
        except ValidationError:
            return stop("模型动作不符合接口约束，已拒绝执行。")
        if isinstance(action, FinishAction):
            ids = action.finding.evidence_ids
            known = {item.id: item for item in evidence}
            if len(ids) != len(set(ids)) or any(ref not in known for ref in ids):
                return stop("结论包含无效或重复的证据引用。")
            if action.finding.code != "insufficient_evidence" and (
                not ids or not any(known[ref].status == "ok" for ref in ids)
            ):
                return stop("确定性结论必须引用本次调查的成功观测证据。")
            return report(action.finding)
        if used == max_tool_calls:
            return stop("工具调用预算耗尽，调查结束。")
        # EvidenceTools 将数据源故障转换为 Evidence(status='error')，同样占用预算。
        evidence.append(dispatch[action.tool]())
    raise AssertionError("unreachable")
