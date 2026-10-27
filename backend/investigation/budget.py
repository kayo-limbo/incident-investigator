"""调查操作边界上的时间预算；不抢占同步 I/O。"""

import math
from time import monotonic
from typing import Literal

from pydantic import BaseModel


class TokenBudgetStatus(BaseModel):
    mode: Literal["observed_usage"] = "observed_usage"
    max_total_tokens: int
    known_total_tokens: int
    total_tokens: int | None
    remaining_tokens: int | None
    exceeded_by: int | None
    unknown_usage_calls: int
    status: Literal["available", "exhausted", "unknown"]


class ObservedTokenBudget:
    """按响应后的供应商用量停止；无法预留未知输入成本或保证费用硬上限。"""

    def __init__(self, max_total_tokens=None):
        if max_total_tokens is not None and (type(max_total_tokens) is not int or max_total_tokens <= 0):
            raise ValueError("累计token预算必须是正整数")
        self.limit = max_total_tokens

    def snapshot(self, usage):
        if self.limit is None:
            return None
        calls = [call for call in usage.calls if call.kind == "model"]
        unknown = sum(call.total_tokens is None for call in calls)
        known = sum(call.total_tokens for call in calls if call.total_tokens is not None)
        return TokenBudgetStatus(
            max_total_tokens=self.limit, known_total_tokens=known,
            total_tokens=None if unknown else known,
            remaining_tokens=None if unknown else max(0, self.limit - known),
            exceeded_by=None if unknown else max(0, known - self.limit),
            unknown_usage_calls=unknown,
            status="unknown" if unknown else "exhausted" if known >= self.limit else "available")


class InvestigationDeadlineExceeded(Exception):
    """调查已达到截止时间。"""


class InvestigationBudget:
    def __init__(self, timeout_seconds, *, clock=monotonic):
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("调查时间预算必须是有限正数")
        self._clock = clock
        self._deadline = clock() + timeout_seconds

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def check(self) -> None:
        if self.remaining_seconds() <= 0:
            raise InvestigationDeadlineExceeded()
