"""Versioned frozen observations. No network, experiment plan or truth imports."""

import copy
import math
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Evidence
from .usage import Usage, CallUsage
from .window_investigation import trace_index

SourceName = Literal["trace_window", "client_runtime"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class RuntimeReading(Strict):
    process_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    started_us: int = Field(ge=0)
    observed_us: int = Field(gt=0)
    max_connections: int = Field(gt=0)
    pool_timeout_ms: int = Field(gt=0)
    requests_started: int = Field(ge=0)
    requests_finished: int = Field(ge=0)
    in_flight: int = Field(ge=0)
    peak_in_flight: int = Field(ge=0)

    @model_validator(mode="after")
    def consistent(self):
        if (self.observed_us < self.started_us or
                self.requests_started - self.requests_finished != self.in_flight or
                self.peak_in_flight < self.in_flight):
            raise ValueError("invalid_runtime_counters")
        return self


class RuntimeWindow(Strict):
    before: RuntimeReading
    after: RuntimeReading

    @model_validator(mode="after")
    def consistent(self):
        a, b = self.before, self.after
        if (a.process_id != b.process_id or a.started_us != b.started_us or
                a.observed_us >= b.observed_us or a.max_connections != b.max_connections or
                a.pool_timeout_ms != b.pool_timeout_ms or
                any(getattr(a, k) > getattr(b, k) for k in
                    ("requests_started", "requests_finished", "peak_in_flight"))):
            raise ValueError("runtime_window_changed")
        return self


class RequestRow(Strict):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: int
    orders_ms: float = Field(ge=0)
    inventory_ms: float | None = Field(ge=0)
    pre_send_ms: float | None = Field(ge=0)
    error_type: Literal["PoolTimeout"] | None
    max_connections: int | float | None
    pool_timeout_ms: int | float | None
    required_observations_present: bool


class TraceWindow(Strict):
    source: Literal["jaeger/orders-preview"] = "jaeger/orders-preview"
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    quality: Literal["observed", "partial", "unavailable"]
    coverage: Literal["sampled"] = "sampled"
    requests: list[RequestRow] = Field(max_length=200)


class Alert(Strict):
    service: Literal["orders"] = "orders"
    operation: Literal["GET /orders/preview"] = "GET /orders/preview"
    request_count: int = Field(gt=0, le=200)
    orders_p95_ms: float = Field(ge=0)
    failed_requests: int = Field(ge=0)


class EvidenceBundle(Strict):
    schema_version: Literal[2] = 2
    projection_version: Literal["investigation-bundle-v1"] = "investigation-bundle-v1"
    alert: Alert
    trace_window: TraceWindow
    client_runtime: RuntimeWindow

    @model_validator(mode="after")
    def consistent(self):
        t, r = self.trace_window, self.client_runtime
        if not r.before.observed_us <= t.start_us < t.end_us <= r.after.observed_us:
            raise ValueError("source_time_mismatch")
        if t.end_us - t.start_us > 300_000_000:
            raise ValueError("window_too_large")
        if t.quality == "unavailable" and t.requests:
            raise ValueError("unavailable_source_contains_data")
        if t.quality == "observed" and (not t.requests or
                not all(row.required_observations_present for row in t.requests)):
            raise ValueError("incomplete_trace_source")
        if len({row.trace_id for row in t.requests}) != len(t.requests):
            raise ValueError("duplicate_request")
        if t.requests and len(t.requests) != self.alert.request_count:
            raise ValueError("alert_membership_mismatch")
        return self


class BundleReference(Strict):
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: SourceName
    field: str = Field(min_length=1, max_length=200)


def scalar_fields(value, prefix=""):
    result = {}
    items = enumerate(value) if isinstance(value, list) else value.items()
    for key, item in items:
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, (dict, list)):
            result.update(scalar_fields(item, path))
        elif item is not None:
            result[path] = item
    return result


def build_bundle(snapshot, before, after, *, traces_available=True):
    """Project only allowlisted observations; masked bundles contain no original key."""
    if snapshot["schema_version"] != 1 or snapshot["projection_version"] != "pool-window-v1":
        raise ValueError("unsupported_source")
    rows = trace_index(snapshot)
    if not rows:
        raise ValueError("empty_source")
    durations = sorted(row["orders_ms"] for row in rows)
    query = snapshot["query"]
    payload = dict(alert=dict(request_count=len(rows),
        orders_p95_ms=durations[math.ceil(.95 * len(rows)) - 1],
        failed_requests=sum(row["status"] != 200 for row in rows)),
        trace_window=dict(start_us=query["start_us"], end_us=query["end_us"],
            quality=("observed" if snapshot["quality"] == "observed" and
                     all(row["required_observations_present"] for row in rows) else "partial")
                     if traces_available else "unavailable",
            requests=rows if traces_available else []),
        client_runtime=dict(before=before, after=after))
    return EvidenceBundle.model_validate(payload).model_dump(mode="json")


class BundleSession:
    def __init__(self, store, snapshot_id, max_tool_calls=6):
        if type(max_tool_calls) is not int or not 1 <= max_tool_calls <= 100:
            raise ValueError("invalid_tool_budget")
        self.snapshot_id, self.max_tool_calls = snapshot_id, max_tool_calls
        self.started, self.usage, self.evidence = monotonic(), Usage(), []
        self._payload = None
        try:
            self._payload = EvidenceBundle.model_validate(store.read(snapshot_id)).model_dump(mode="json")
            self._record("get_alert", {"alert": self._payload["alert"],
                "sources": ["trace_window", "client_runtime"],
                "start_us": self._payload["trace_window"]["start_us"],
                "end_us": self._payload["trace_window"]["end_us"]})
        except (ValueError, OSError, TypeError):
            self._payload = None
            self._record("get_alert", {}, status="error")

    @property
    def remaining(self):
        return self.max_tool_calls - sum(c.kind == "tool" for c in self.usage.calls)

    def _record(self, tool, data, status="ok"):
        evidence = Evidence(tool=tool, source="frozen-bundle", status=status,
            parameters={"snapshot_id": self.snapshot_id}, data=copy.deepcopy(data))
        self.evidence.append(evidence)
        self.usage.calls.append(CallUsage(kind="tool", name=tool, status=status, elapsed_ms=0))
        return evidence.model_copy(deep=True)

    def check_query(self, source):
        if self.remaining <= 0:
            raise ValueError("budget_exhausted")
        if source not in ("trace_window", "client_runtime") or self._payload is None:
            raise ValueError("source_outside_bundle")
        if any(e.tool == source for e in self.evidence):
            raise ValueError("repeated_query")

    def query(self, source):
        self.check_query(source)
        data = copy.deepcopy(self._payload[source])
        data["reference_fields"] = list(scalar_fields(data))
        return self._record(source, data)

    def validate_references(self, references):
        seen = {e.tool for e in self.evidence if e.status == "ok"}
        keys = set()
        for ref in references:
            key = (ref.snapshot_id, ref.source, ref.field)
            if (key in keys or ref.snapshot_id != self.snapshot_id or ref.source not in seen or
                    ref.field not in scalar_fields(self._payload[ref.source])):
                raise ValueError("invalid_reference")
            keys.add(key)

    def resolve(self, ref):
        self.validate_references([ref])
        return scalar_fields(self._payload[ref.source])[ref.field]
