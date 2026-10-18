"""供规则与模型策略共享的受限窗口工具；不导入 lab/evals 的场景数据。"""

import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from time import monotonic

import httpx

from .evidence_models import WindowQuery
from .models import Evidence, TraceQuery
from .snapshot_store import SnapshotStore
from .tools import EvidenceTools, ToolFailure, project_trace

POOL_TAGS = {"client.pre_send_ms", "client.connect_ms", "client.transport_ms",
             "client.in_flight_at_start", "client.max_connections", "client.pool_timeout_ms",
             "client.request_headers_started"}


def project_window(raw: dict, query: WindowQuery) -> dict:
    entries = raw.get("data")
    if not isinstance(entries, list):
        raise ToolFailure("invalid_response")
    if len(entries) > query.limit:
        raise ToolFailure("too_large")
    traces, rejected, excluded = {}, 0, 0
    for entry in entries:
        try:
            trace_id = TraceQuery(trace_id=entry["traceID"]).trace_id
            if trace_id in traces:
                raise ToolFailure("duplicate_trace")
            projected = project_trace({"data": [entry]}, trace_id)
            roots = [s for s in projected["spans"] if s["service"] == query.service
                     and s["operation"] == query.operation and s["tags"].get("span.kind") == "server"]
            if len(roots) != 1:
                raise ToolFailure("invalid_root")
            # Jaeger 可能采用闭区间；本地统一以订单 SERVER 开始时间定义 [start,end)。
            if not query.start_us <= roots[0]["start_us"] < query.end_us:
                excluded += 1
                continue
            ids = [s["span_id"] for s in projected["spans"]]
            if len(ids) != len(set(ids)):
                raise ToolFailure("duplicate_span")
            for source, target in zip(entry["spans"], projected["spans"]):
                for tag in source["tags"]:
                    if tag["key"] not in POOL_TAGS:
                        continue
                    value = tag["value"]
                    if tag["key"] == "client.request_headers_started":
                        valid = type(value) is bool
                    else:
                        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
                    if not valid:
                        raise ToolFailure("invalid_pool_observation")
                    target["tags"][tag["key"]] = value
            traces[trace_id] = projected
        except (ValueError, KeyError, TypeError, AttributeError, ToolFailure):
            rejected += 1
    quality = "partial" if rejected or raw.get("errors") or len(entries) >= query.limit else (
        "observed" if traces else "empty")
    return {"traces": traces, "quality": quality, "received": len(entries),
            "rejected": rejected, "excluded": excluded, "limit_reached": len(entries) >= query.limit,
            "source_reported_errors": bool(raw.get("errors")), "coverage": "unknown"}


def summarize(payload: dict) -> dict:
    statuses, errors = Counter(), Counter()
    durations = []
    measurements = {key: [] for key in ("inventory_ms", "pre_send_ms", "transport_ms")}
    for trace in payload["traces"].values():
        for span in trace["spans"]:
            if span["service"] == "orders" and span["operation"] == "GET /orders/preview" and span["tags"].get("span.kind") == "server":
                statuses[str(span["tags"].get("http.response.status_code", "unknown"))] += 1
                durations.append(span["duration_ms"])
            if span["tags"].get("span.kind") == "client" and "error.type" in span["tags"]:
                errors[span["tags"]["error.type"]] += 1
            if span["service"] == "inventory" and span["tags"].get("span.kind") == "server":
                measurements["inventory_ms"].append(span["duration_ms"])
            if span["service"] == "orders" and span["tags"].get("span.kind") == "client":
                for field in ("pre_send_ms", "transport_ms"):
                    if "client." + field in span["tags"]:
                        measurements[field].append(span["tags"]["client." + field])
    return {"request_count": len(payload["traces"]), "http_status_counts": dict(statuses),
            "client_error_counts": dict(errors),
            "orders_p95_ms": sorted(durations)[math.ceil(.95 * len(durations)) - 1] if durations else None,
            "measurements": {key: {"count": len(values), "p95_ms":
                sorted(values)[math.ceil(.95 * len(values)) - 1] if values else None}
                for key, values in measurements.items()},
            "trace_ids": sorted(payload["traces"])}


class WindowEvidenceTools(EvidenceTools):
    def __init__(self, store: SnapshotStore):
        self.store = store

    def _fetch_window(self, source: str, query: WindowQuery):
        started = monotonic()
        params = {"service": query.service, "operation": query.operation,
                  "start": query.start_us, "end": query.end_us, "limit": query.limit}
        chunks, size = [], 0
        with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", source, params=params) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 8_000_000:
                        raise ToolFailure("too_large")
                    if monotonic() - started > 5:
                        raise ToolFailure("timeout")
                    chunks.append(chunk)
        return json.loads(b"".join(chunks))

    def query_window(self, query: WindowQuery) -> Evidence:
        query = WindowQuery.model_validate(query)
        source = f"http://127.0.0.1:{int(os.getenv('JAEGER_PORT', '16686'))}/api/traces"
        started = monotonic()

        def read():
            payload = project_window(self._fetch_window(source, query), query)
            payload.update(schema_version=1, projection_version="pool-window-v1", source=source,
                           scope="local-compose:orders-preview:readonly",
                           query=query.model_dump(), collected_at=datetime.now(timezone.utc).isoformat())
            try:
                snapshot_id = self.store.freeze(payload)
            except (OSError, ValueError):
                raise ToolFailure("snapshot_write_failed") from None
            return {"snapshot_id": snapshot_id, "summary": summarize(payload),
                    **{k: v for k, v in payload.items() if k != "traces"},
                    "limitations": ["仅统计已采集样本，不保证全量或最终完整性。",
                                     "发送前耗时包含建连与调度，不能等同纯连接池等待。",
                                     "配置来自请求 span，不代表当前运行配置。"]}

        evidence = self._read("query_window", source, query.model_dump(), read)
        evidence.data["usage"] = {"tool_calls": 1, "elapsed_ms": (monotonic() - started) * 1000}
        if evidence.status == "error":
            evidence.data["quality"] = "unavailable"
        return evidence

    def get_snapshot_trace(self, snapshot_id: str, trace_id: str) -> Evidence:
        TraceQuery(trace_id=trace_id)

        def read():
            try:
                payload = self.store.read(snapshot_id)
            except FileNotFoundError:
                raise ToolFailure("snapshot_not_found") from None
            except ValueError:
                raise ToolFailure("snapshot_invalid") from None
            if trace_id not in payload["traces"]:
                raise ToolFailure("trace_not_in_snapshot")
            return {"snapshot_id": snapshot_id, "query": payload["query"],
                    "collected_at": payload["collected_at"], **payload["traces"][trace_id]}

        return self._read("get_snapshot_trace", "frozen-window", {"snapshot_id": snapshot_id,
                                                                  "trace_id": trace_id}, read)
