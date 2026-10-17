"""调查输入与证据结构；不包含故障场景名称或注入参数。"""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class TraceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class Evidence(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    tool: str
    source: str
    parameters: dict = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: Literal["ok", "error"] = "ok"
    error_code: str | None = None
    data: dict = Field(default_factory=dict)


class Finding(BaseModel):
    code: Literal["no_anomaly_observed", "inventory_latency", "endpoint_mismatch", "insufficient_evidence"]
    summary: str
    evidence_ids: list[str]
    limitations: list[str]


class Investigation(BaseModel):
    trace_id: str
    strategy: str = "fixed_workflow_v1"
    finding: Finding
    evidence: list[Evidence]
    termination: Literal["completed", "model_error", "invalid_action", "invalid_finding", "budget_exhausted"] = "completed"
