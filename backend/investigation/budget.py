"""调查操作边界上的时间预算；不抢占同步 I/O。"""

import math
from time import monotonic


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
