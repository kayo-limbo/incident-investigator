"""任务参数与持久化状态；不存储模型凭据。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy: Literal["fixed", "agent", "hypotheses"] = "fixed"
    max_tool_calls: int = Field(default=6, ge=1, le=100)
    timeout_seconds: float = Field(default=120, gt=0, le=3600, allow_inf_nan=False)
    max_total_tokens: int | None = Field(default=None, gt=0)
    repeat_query_policy: Literal["stop", "allow"] = "stop"

    @model_validator(mode="after")
    def fixed_options(self):
        if self.strategy == "fixed" and (self.timeout_seconds != 120 or self.max_total_tokens is not None
                                         or self.repeat_query_policy != "stop"):
            raise ValueError("固定规则只支持max_tool_calls，不接受模型预算或重复策略配置")
        return self


class TaskRecord(BaseModel):
    id: UUID
    idempotency_key: str
    request: TaskRequest
    request_hash: str
    state: Literal["queued", "running", "completed", "failed", "cancelled"]
    worker_id: str | None
    run_token: UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict | None
    error_code: str | None
