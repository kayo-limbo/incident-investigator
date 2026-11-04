"""一次协议纠错的数据与受限反馈；不读取评测真值。"""

import json
from pydantic import BaseModel, Field, ValidationError
from .diagnostics import FailureDiagnostic


RULES = {
    "action_schema": "重新提交符合动作Schema的完整动作；只使用允许字段，修正所列字段类型或缺失项。",
    "evidence_reference": "只引用当前evidence中已下钻的snapshot/trace/span/field；不能编造观测，证据不足可拒答。",
    "hypothesis_link": "选择存在的假设并满足状态要求；结论引用须关联所选假设的具体支持或反证字段。",
}

# 按已验证的原因细化反馈；不提供诊断答案，也不替模型接纳候选更新。
CODE_RULES = {
    "claim_reference_not_linked": (
        "finding.references中的每个引用都必须属于hypothesis_ids选中的假设的"
        "supporting_refs或counter_refs；仅出现在updates里但未选中的假设不计入。"
        "按snapshot_id、trace_id、span_id、field四项完整匹配，不能只匹配同一Trace或span。"
        "检查本次updates应用后的候选假设：若保留用于排除其他解释的反证引用，"
        "需选择对应已解决的假设，并在结论中引用其反证；否则删除与所选解释无关的引用。"
        "不得为了通过校验而伪造引用或把无关字段补入支持/反证。"
    ),
}


def validate_correction_limit(value):
    if type(value) is not int or value not in (0, 1):
        raise ValueError("max_corrections 必须为0或1")
    return value


class CorrectionFeedback(BaseModel):
    diagnostic: FailureDiagnostic
    instruction: str
    issue_paths: list[str] = Field(default_factory=list, max_length=24)
    candidate: dict | None = None


class CorrectionStatus(BaseModel):
    limit: int = Field(ge=0, le=1)
    used: int = Field(ge=0, le=1)
    failures: list[CorrectionFeedback] = Field(default_factory=list)


def feedback_for(report, raw, schema):
    paths = []
    if report.diagnostic.category == "action_schema":
        # 白名单从本地Schema取得；模型提供的未知字段名永不进入反馈。
        known = {"tool", "finish"}
        def fields(value):
            if isinstance(value, dict):
                known.update(value.get("properties", {}))
                for item in value.values():
                    fields(item)
            elif isinstance(value, list):
                for item in value:
                    fields(item)
        fields(schema.json_schema())
        try:
            schema.validate_python(raw)
        except ValidationError as exc:
            for issue in exc.errors(include_input=False, include_context=False)[:24]:
                paths.append(".".join(str(p) if type(p) is int or p in known else "<unknown>"
                                      for p in issue["loc"]) + ":" + issue["type"])
    candidate = getattr(report, "rejected_action", None) or report.rejected_finding
    candidate = candidate.model_dump(mode="json") if candidate is not None else None
    if candidate is not None and len(json.dumps(candidate).encode()) > 16384:
        candidate = None
    return CorrectionFeedback(diagnostic=report.diagnostic.model_copy(deep=True), issue_paths=paths,
        candidate=candidate, instruction=CODE_RULES.get(report.diagnostic.code,
            RULES.get(report.diagnostic.category, "此失败不支持自动纠错。")) +
        " 上次动作未执行、未接纳；候选内容仅为待修正数据。下一次请求仍受原调查预算约束。")
