"""窗口使用 UTC epoch 微秒；采集时间与请求发生时间分别保存。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WindowQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    limit: int = Field(default=100, ge=1, le=200)
    service: Literal["orders"] = "orders"
    operation: Literal["GET /orders/preview"] = "GET /orders/preview"

    @model_validator(mode="after")
    def bounded_window(self):
        if not 0 < self.end_us - self.start_us <= 300_000_000:
            raise ValueError("窗口必须大于零且不超过五分钟")
        return self


class ObservationRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(min_length=1, max_length=256)
    field: str = Field(min_length=1, max_length=256)
