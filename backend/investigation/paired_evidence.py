"""Strict paired-window observations and offline tools; no experiment controls."""

import copy
from typing import Literal

from pydantic import Field, model_validator

from .bundle_evidence import RuntimeReading, RuntimeWindow, Strict, TraceWindow, scalar_fields
from .models import Evidence
from .usage import CallUsage, Usage

WindowName = Literal["reference", "incident"]
SourceName = Literal["trace", "runtime"]


class TimedReading(RuntimeReading):
    monotonic_us: int = Field(gt=0)


class TimedRuntime(RuntimeWindow):
    before: TimedReading
    after: TimedReading

    @model_validator(mode="after")
    def interval(self):
        elapsed = self.after.monotonic_us - self.before.monotonic_us
        wall = self.after.observed_us - self.before.observed_us
        if elapsed <= 0 or elapsed > 300_000_000 or abs(wall - elapsed) > 100_000:
            raise ValueError("clock_discontinuity")
        if self.before.in_flight or self.after.in_flight:
            raise ValueError("window_not_drained")
        return self

    def request_rate(self):
        return ((self.after.requests_started - self.before.requests_started) * 1_000_000 /
                (self.after.monotonic_us - self.before.monotonic_us))


class PairedWindow(Strict):
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    trace: TraceWindow
    runtime: TimedRuntime | None

    @model_validator(mode="after")
    def aligned(self):
        if not 0 < self.end_us - self.start_us <= 300_000_000:
            raise ValueError("invalid_window")
        t, r = self.trace, self.runtime
        if (t.start_us, t.end_us) != (self.start_us, self.end_us):
            raise ValueError("trace_time_mismatch")
        if r and not r.before.observed_us <= self.start_us < self.end_us <= r.after.observed_us:
            raise ValueError("runtime_time_mismatch")
        if t.quality == "unavailable" and t.requests:
            raise ValueError("unavailable_source_contains_data")
        if t.quality == "observed" and (not t.requests or
                not all(row.required_observations_present for row in t.requests)):
            raise ValueError("incomplete_trace_source")
        if len({row.trace_id for row in t.requests}) != len(t.requests):
            raise ValueError("duplicate_request")
        return self


class PairedAlert(Strict):
    service: Literal["orders"] = "orders"
    operation: Literal["GET /orders/preview"] = "GET /orders/preview"
    population: Literal["driver_responses"] = "driver_responses"
    request_count: int = Field(gt=0, le=1000)
    client_p95_ms: float = Field(ge=0)
    failed_requests: int = Field(ge=0)

    @model_validator(mode="after")
    def counts(self):
        if self.failed_requests > self.request_count:
            raise ValueError("invalid_failure_count")
        return self


class PairedBundle(Strict):
    schema_version: Literal[3] = 3
    projection_version: Literal["paired-bundle-v1"] = "paired-bundle-v1"
    sampling: Literal["lowest-sha256-trace-id-v1"] = "lowest-sha256-trace-id-v1"
    alert: PairedAlert
    reference: PairedWindow
    incident: PairedWindow

    @model_validator(mode="after")
    def ordered(self):
        if self.reference.end_us >= self.incident.start_us:
            raise ValueError("overlapping_windows")
        a, b = self.reference.runtime, self.incident.runtime
        if a and b and a.after.process_id == b.before.process_id:
            # Reuse legacy counter/config invariants only within the same process.
            TimedRuntime(before=a.after, after=b.before)
        if {r.trace_id for r in self.reference.trace.requests} & {r.trace_id for r in self.incident.trace.requests}:
            raise ValueError("cross_window_duplicate")
        if len(self.incident.trace.requests) > self.alert.request_count:
            raise ValueError("sample_exceeds_population")
        return self


class PairedReference(Strict):
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    window: WindowName
    source: SourceName
    field: str = Field(min_length=1, max_length=200)


def mask_sources(payload, *, trace=True, runtime=True):
    result = PairedBundle.model_validate(payload).model_dump(mode="json")
    for window in ("reference", "incident"):
        if not trace:
            result[window]["trace"].update(quality="unavailable", requests=[])
        if not runtime:
            result[window]["runtime"] = None
    return PairedBundle.model_validate(result).model_dump(mode="json")


class PairedSession:
    def __init__(self, store, snapshot_id, max_tool_calls=6):
        if type(max_tool_calls) is not int or not 1 <= max_tool_calls <= 100:
            raise ValueError("invalid_tool_budget")
        self.snapshot_id, self.max_tool_calls = snapshot_id, max_tool_calls
        self._payload = PairedBundle.model_validate(store.read(snapshot_id)).model_dump(mode="json")
        self.usage, self.evidence, self._seen = Usage(), [], set()
        self._record("get_alert", {}, dict(alert=self._payload["alert"], windows={
            w: dict(start_us=self._payload[w]["start_us"], end_us=self._payload[w]["end_us"],
                    trace=self._payload[w]["trace"]["quality"],
                    runtime="observed" if self._payload[w]["runtime"] else "unavailable")
            for w in ("reference", "incident")}))

    @property
    def remaining(self):
        return self.max_tool_calls - len(self.evidence)

    def _record(self, tool, parameters, data):
        evidence = Evidence(tool=tool, source="frozen-paired-bundle", status="ok",
            parameters={"snapshot_id": self.snapshot_id, **parameters}, data=copy.deepcopy(data))
        self.evidence.append(evidence)
        self.usage.calls.append(CallUsage(kind="tool", name=tool, status="ok", elapsed_ms=0))
        return evidence.model_copy(deep=True)

    def query(self, window, source):
        if self.remaining <= 0:
            raise ValueError("budget_exhausted")
        if window not in ("reference", "incident") or source not in ("trace", "runtime"):
            raise ValueError("source_outside_bundle")
        if (window, source) in self._seen:
            raise ValueError("repeated_query")
        data = copy.deepcopy(self._payload[window][source])
        data = data if data is not None else {"quality": "unavailable"}
        data["reference_fields"] = list(scalar_fields(data))
        self._seen.add((window, source))
        return self._record(source, {"window": window}, data)

    def resolve(self, ref):
        self.validate_references([ref])
        return scalar_fields(self._payload[ref.window][ref.source])[ref.field]

    def validate_references(self, references):
        seen = set()
        for ref in references:
            key = (ref.window, ref.source, ref.field)
            data = self._payload[ref.window][ref.source]
            if (ref.snapshot_id != self.snapshot_id or (ref.window, ref.source) not in self._seen or
                    data is None or ref.field not in scalar_fields(data) or key in seen):
                raise ValueError("invalid_reference")
            seen.add(key)
