"""固定原因码的失败诊断；不接收模型原文、异常文本或评测真值。"""

from typing import Literal
from pydantic import BaseModel, Field


PARSE_DETAILS = {"response_json_invalid", "response_shape_invalid", "response_finish_invalid",
                 "action_content_invalid", "action_json_invalid", "action_not_object"}
REQUEST_CODES = {"context_too_large", "authentication_failed", "rate_limited", "http_error",
                 "timeout", "transport_error"}
RESPONSE_CODES = {"invalid_response", "response_too_large", "refused", "output_truncated"}
REFERENCE_CODES = {"invalid_reference", "insufficient_observations"}
STATE_CODES = {"hypothesis_limit_or_duplicate", "hypothesis_version_conflict",
               "hypothesis_identity_changed", "support_required", "counter_evidence_required"}
LINK_CODES = {"unknown_or_duplicate_hypothesis", "claim_hypothesis_required", "claim_hypothesis_unresolved",
              "claim_support_required", "claim_reference_not_linked", "claim_hypothesis_not_cited"}


class FailureDiagnostic(BaseModel):
    stage: Literal["decide", "validate", "tool", "source"]
    category: Literal["model_request", "model_response", "action_schema", "evidence_reference",
                      "hypothesis_state", "hypothesis_link", "tool_arguments", "source", "internal"]
    code: str
    # 一轮对应一次模型调用；0表示模型调用前失败。用量在同报告usage.calls中查阅。
    turn: int = Field(ge=0)
    issue_types: list[str] = Field(default_factory=list, max_length=24)


def diagnostic(code, turn, *, stage="validate", issues=None):
    if code in REQUEST_CODES:
        category = "model_request"
    elif code in RESPONSE_CODES or code in PARSE_DETAILS:
        category = "model_response"
    elif code == "invalid_schema":
        category = "action_schema"
    elif code in REFERENCE_CODES:
        category = "evidence_reference"
    elif code in STATE_CODES:
        category = "hypothesis_state"
    elif code in LINK_CODES:
        category = "hypothesis_link"
    elif code == "trace_outside_snapshot":
        category = "tool_arguments"
    elif code == "snapshot_unavailable":
        category = "source"
    else:
        category, code = "internal", "unclassified_error"
    return FailureDiagnostic(stage=stage, category=category, code=code, turn=turn,
                             issue_types=issues or [])


def validation_diagnostic(report, code, exc=None, *, stage="validate"):
    # 不保存loc中的未知字段名（可能包含敏感值）、input、msg或ctx。
    issues = [item["type"] for item in exc.errors(include_input=False, include_context=False)][:24] if exc else []
    report.diagnostic = diagnostic(code, report.usage_totals["model_calls"], stage=stage, issues=issues)
    return report
