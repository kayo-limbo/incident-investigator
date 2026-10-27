"""统一调用账本；缺失供应商用量不是零，失败调用同样计数。"""

from typing import Literal

from pydantic import BaseModel, Field


def token_usage(raw) -> dict[str, int | None]:
    names = ("prompt_tokens", "completion_tokens", "total_tokens")
    result = {name: None for name in names}
    if isinstance(raw, dict):
        for name in names:
            value = raw.get(name)
            if type(value) is int and value >= 0:
                result[name] = value
        if all(result[name] is not None for name in names) and (
            result["total_tokens"] != result["prompt_tokens"] + result["completion_tokens"]
        ):
            return {name: None for name in names}
        total = result["total_tokens"]
        if total is not None and any(result[name] is not None and result[name] > total
                                     for name in ("prompt_tokens", "completion_tokens")):
            return {name: None for name in names}
    return result


class CallUsage(BaseModel):
    kind: Literal["model", "tool"]
    name: str
    status: str
    error_code: str | None = None
    elapsed_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class Usage(BaseModel):
    calls: list[CallUsage] = Field(default_factory=list)
    elapsed_ms: float = 0

    def totals(self) -> dict:
        models = [c for c in self.calls if c.kind == "model"]
        result = {"model_calls": len(models), "tool_calls": sum(c.kind == "tool" for c in self.calls),
                  "drilldown_calls": sum(c.name == "get_snapshot_trace" for c in self.calls),
                  "elapsed_ms": self.elapsed_ms}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            values = [getattr(c, name) for c in models]
            result[name] = sum(values) if all(v is not None for v in values) else None
        result["unknown_usage_calls"] = sum(c.total_tokens is None for c in models)
        return result
