"""冻结窗口调查协议、只读会话与受限执行器。规则与模型使用同一接口。"""

import copy
import math
from time import monotonic
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .evidence_models import ObservationRef, WindowQuery
from .agent import ModelFailure
from .models import Evidence, TraceQuery
from .snapshot_store import SnapshotStore
from .usage import CallUsage, Usage, token_usage
from .window_queries import summarize

Code = Literal["no_anomaly_observed", "inventory_latency", "pre_send_delay",
               "pool_contention", "inventory_latency_with_pool_contention", "insufficient_evidence"]
Termination = Literal["completed", "source_error", "model_error", "invalid_action",
                      "invalid_finding", "budget_exhausted"]
LIMITATIONS = ["结论仅适用于冻结窗口中的已采集样本，不保证全量覆盖。",
               "发送前耗时包含建连和调度，不能视为纯连接池等待。",
               "引用校验不证明语义正确或唯一根因；请求配置不等于当前配置。"]


class WindowReference(ObservationRef):
    field: str = Field(max_length=256, pattern=r"^(duration_ms|start_us|service|operation|tags\..+)$",
                       description="复制下钻 span.reference_fields 中的完整路径；标签必须保留 tags. 前缀")


class WindowFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: Code
    summary: str = Field(min_length=1, max_length=2000)
    references: list[WindowReference] = Field(max_length=24)
    limitations: list[str] = Field(max_length=20)


class Drilldown(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["tool"]
    tool: Literal["get_snapshot_trace"]
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class Finish(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["finish"]
    finding: WindowFinding


WINDOW_ACTION = TypeAdapter(Annotated[Drilldown | Finish, Field(discriminator="kind")])


class WindowContext(BaseModel):
    snapshot_id: str
    evidence: list[Evidence]
    remaining_tool_calls: int


class WindowReport(BaseModel):
    snapshot_id: str
    strategy: str
    finding: WindowFinding
    evidence: list[Evidence]
    termination: Termination
    usage: Usage
    usage_totals: dict
    rejected_finding: WindowFinding | None = None


def trace_index(payload):
    rows = []
    for trace_id, trace in sorted(payload["traces"].items()):
        spans = trace["spans"]
        roots = [s for s in spans if s["service"] == "orders" and s["operation"] == "GET /orders/preview"
                 and s["tags"].get("span.kind") == "server"]
        clients = [s for s in spans if s["service"] == "orders" and s["tags"].get("span.kind") == "client"]
        leaves = [s for s in spans if s["service"] == "inventory" and s["tags"].get("span.kind") == "server"]
        root = roots[0] if len(roots) == 1 else {}
        child = clients[0] if len(clients) == 1 else {}
        leaf = leaves[0] if len(leaves) == 1 else {}
        tags = child.get("tags", {})
        status = root.get("tags", {}).get("http.response.status_code")
        def number(value):
            return type(value) in (int, float) and math.isfinite(value) and value >= 0
        linked = bool(root and child and root["span_id"] in child["parents"])
        complete = linked and all(number(tags.get(k)) for k in (
            "client.transport_ms", "client.max_connections", "client.pool_timeout_ms"))
        if status == 200:
            complete = complete and bool(leaf and child["span_id"] in leaf["parents"] and
                tags.get("client.request_headers_started") is True and
                number(tags.get("client.pre_send_ms")) and not tags.get("error.type"))
        elif status == 503 and tags.get("error.type") == "PoolTimeout":
            complete = complete and not leaves and tags.get("client.request_headers_started") is False
        else:
            complete = False
        rows.append({"trace_id": trace_id, "status": status, "orders_ms": root.get("duration_ms"),
                     "inventory_ms": leaf.get("duration_ms"), "pre_send_ms": tags.get("client.pre_send_ms"),
                     "error_type": tags.get("error.type"), "max_connections": tags.get("client.max_connections"),
                     "pool_timeout_ms": tags.get("client.pool_timeout_ms"), "required_observations_present": bool(complete)})
    return rows


class FrozenWindowSession:
    """每次调查独占会话。对外只返回深拷贝；没有实时查询或更换快照接口。"""

    def __init__(self, store: SnapshotStore, snapshot_id: str, max_tool_calls: int = 6):
        if type(max_tool_calls) is not int or not 1 <= max_tool_calls <= 100:
            raise ValueError("max_tool_calls 必须为 1 到 100 的整数，包含摘要读取")
        self.snapshot_id, self.max_tool_calls = snapshot_id, max_tool_calls
        self.usage, self.evidence = Usage(), []
        self.started = monotonic()
        self._payload = None
        self.rows = []
        evidence = Evidence(tool="get_snapshot_summary", source="frozen-window", parameters={"snapshot_id": snapshot_id})
        started = monotonic()
        try:
            payload = store.read(snapshot_id)
            query = WindowQuery.model_validate(payload["query"])
            if payload["schema_version"] != 1 or payload["projection_version"] != "pool-window-v1":
                raise ValueError("unsupported_snapshot")
            if len(payload["traces"]) > query.limit:
                raise ValueError("invalid_snapshot")
            self.rows = trace_index(payload)
            for trace_id, trace in payload["traces"].items():
                TraceQuery(trace_id=trace_id)
                if trace["trace_id"] != trace_id:
                    raise ValueError("invalid_snapshot")
                roots = [s for s in trace["spans"] if s["service"] == query.service and
                         s["operation"] == query.operation and s["tags"].get("span.kind") == "server"]
                if len(roots) != 1 or not query.start_us <= roots[0]["start_us"] < query.end_us:
                    raise ValueError("invalid_snapshot")
            self._payload = payload
            evidence.data = {"snapshot_id": snapshot_id, "query": query.model_dump(),
                             "collected_at": payload["collected_at"], "quality": payload["quality"],
                             "coverage": payload["coverage"], "summary": summarize(payload),
                             "trace_index": self.rows, "limitations": LIMITATIONS}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self._payload, self.rows = None, []
            evidence.status, evidence.error_code = "error", "snapshot_unavailable_or_invalid"
        self.evidence.append(evidence.model_copy(deep=True))
        self.usage.calls.append(CallUsage(kind="tool", name=evidence.tool, status=evidence.status,
                                         elapsed_ms=(monotonic() - started) * 1000))

    @property
    def remaining(self):
        return self.max_tool_calls - sum(c.kind == "tool" for c in self.usage.calls)

    @property
    def usable(self):
        return bool(self._payload and self._payload["quality"] == "observed" and self.rows and
                    all(row["required_observations_present"] for row in self.rows))

    def context(self):
        return WindowContext(snapshot_id=self.snapshot_id, evidence=self.evidence,
                             remaining_tool_calls=self.remaining).model_copy(deep=True)

    def drilldown(self, trace_id):
        if self.remaining <= 0:
            raise ValueError("budget_exhausted")
        if self._payload is None or trace_id not in self._payload["traces"]:
            raise ValueError("trace_outside_snapshot")
        started = monotonic()
        evidence = Evidence(tool="get_snapshot_trace", source="frozen-window",
                            parameters={"snapshot_id": self.snapshot_id, "trace_id": trace_id},
                            data={"snapshot_id": self.snapshot_id, **copy.deepcopy(self._payload["traces"][trace_id])})
        for span in evidence.data["spans"]:
            span["reference_fields"] = ["duration_ms", "start_us", "service", "operation"] + [
                "tags." + name for name in sorted(span["tags"])]
        self.evidence.append(evidence.model_copy(deep=True))
        self.usage.calls.append(CallUsage(kind="tool", name=evidence.tool, status="ok",
                                         elapsed_ms=(monotonic() - started) * 1000))
        return evidence

    def validate_finding(self, finding):
        if finding.code != "insufficient_evidence" and (not self.usable or not finding.references):
            raise ValueError("insufficient_observations")
        self.validate_references(finding.references)

    def validate_references(self, references):
        """仅允许本会话已下钻、绑定快照中真实存在且不重复的字段。"""
        seen = {e.data["trace_id"] for e in self.evidence if e.tool == "get_snapshot_trace" and e.status == "ok"}
        keys = set()
        for ref in references:
            key = (ref.snapshot_id, ref.trace_id, ref.span_id, ref.field)
            if key in keys or ref.snapshot_id != self.snapshot_id or ref.trace_id not in seen:
                raise ValueError("invalid_reference")
            keys.add(key)
            spans = [s for s in self._payload["traces"][ref.trace_id]["spans"] if s["span_id"] == ref.span_id]
            if len(spans) != 1:
                raise ValueError("invalid_reference")
            if ref.field.startswith("tags.") and ref.field[5:] in spans[0]["tags"]:
                continue
            if ref.field not in {"duration_ms", "start_us", "service", "operation"}:
                raise ValueError("invalid_reference")

    def report(self, strategy, finding=None, termination="completed", reason="证据不足"):
        if finding is None:
            finding = WindowFinding(code="insufficient_evidence", summary=reason, references=[], limitations=[])
        finding = finding.model_copy(deep=True)
        finding.limitations = list(dict.fromkeys(finding.limitations + LIMITATIONS))
        self.usage.elapsed_ms = (monotonic() - self.started) * 1000
        return WindowReport(snapshot_id=self.snapshot_id, strategy=strategy, finding=finding,
                            evidence=self.evidence, termination=termination,
                            usage=self.usage, usage_totals=self.usage.totals()).model_copy(deep=True)


def investigate_window_agent(store, snapshot_id, model, max_tool_calls=6):
    session = FrozenWindowSession(store, snapshot_id, max_tool_calls)
    strategy = "window_react_v1"
    if session._payload is None:
        return session.report(strategy, termination="source_error", reason="快照无法读取或无效。")
    for _ in range(max_tool_calls):
        started, status, error_code = monotonic(), "ok", None
        try:
            raw = model.decide(session.context())
        except ModelFailure as exc:
            status, error_code = "error", exc.code
        except Exception:
            status, error_code = "error", "adapter_error"
        finally:
            usage = token_usage(getattr(model, "last_usage", None))
            session.usage.calls.append(CallUsage(kind="model", name="decide", status=status,
                elapsed_ms=(monotonic() - started) * 1000, error_code=error_code, **usage))
        if status == "error":
            return session.report(strategy, termination="model_error", reason="模型调用失败，保留已有证据。")
        try:
            action = WINDOW_ACTION.validate_python(raw)
        except ValidationError:
            return session.report(strategy, termination="invalid_action", reason="模型动作不符合窗口协议。")
        if isinstance(action, Finish):
            try:
                session.validate_finding(action.finding)
            except ValueError as exc:
                result = session.report(strategy, termination="invalid_finding", reason=f"结论校验失败：{exc}。")
                # 仅保存已通过 Schema 校验的候选，显式与接受的 finding 分离，便于复盘。
                result.rejected_finding = action.finding.model_copy(deep=True)
                return result
            return session.report(strategy, action.finding)
        if session.remaining <= 0:
            return session.report(strategy, termination="budget_exhausted", reason="工具调用预算耗尽。")
        try:
            session.drilldown(action.trace_id)
        except ValueError:
            return session.report(strategy, termination="invalid_action", reason="请求不属于绑定快照。")
    raise AssertionError("unreachable")
