"""Strong deterministic comparator: same four source/window tools, no eval imports."""

import math

from .paired_agent import PairedDomain, PairedFinding, InvestigationSession
from .paired_evidence import ArrivalRuntime, PairedReference


def statistics(trace):
    if not trace or trace["quality"] != "observed" or len(trace["requests"]) < 5:
        return None
    result = {}
    for field in ("inventory_ms", "pre_send_ms", "orders_ms"):
        values = sorted((row[field], i) for i, row in enumerate(trace["requests"]) if row[field] is not None)
        if len(values) < 5:
            return None
        result[field], result[field + "_index"] = values[math.ceil(.95 * len(values)) - 1]
    result["pool_timeout"] = any(r["error_type"] == "PoolTimeout" for r in trace["requests"])
    result["failed"] = any(r["status"] != 200 for r in trace["requests"])
    return result


def stage(stats):
    if stats is None:
        return "unknown"
    slow = stats["inventory_ms"] >= 250
    wait = stats["pre_send_ms"] >= 150 or stats["pool_timeout"]
    if slow:
        return "mixed" if wait else "downstream"
    if wait:
        return "pre_send"
    if stats["inventory_ms"] < 150 and stats["pre_send_ms"] < 50 and stats["orders_ms"] < 150 and not stats["failed"]:
        return "no_anomaly"
    return "unknown"


def investigate_paired_fixed(store, key, max_tool_calls=6):
    domain = PairedDomain(InvestigationSession(store, key, max_tool_calls), "paired_fixed_v1")
    session = domain.session
    if session._payload is None:
        return domain.report(termination="source_error")
    data = {}
    for window in ("reference", "incident"):
        data[window] = {}
        for source in ("trace", "runtime"):
            if session.remaining <= 0:
                return domain.report(termination="budget_exhausted")
            data[window][source] = session.query(window, source).data
    a, b = (statistics(data[w]["trace"]) for w in ("reference", "incident"))
    phase = stage(b)
    code = "insufficient_evidence"
    runtimes = []
    for window in ("reference", "incident"):
        value = {k: v for k, v in data[window]["runtime"].items() if k != "reference_fields"}
        runtimes.append(None if value.get("quality") == "unavailable" else ArrivalRuntime.model_validate(value))
    ra, rb = runtimes
    if stage(a) == "no_anomaly" and b is not None:
        slow = b["inventory_ms"] >= 250 and b["inventory_ms"] - a["inventory_ms"] >= 100
        ratio = rb.request_rate() / ra.request_rate() if ra and rb and ra.request_rate() > 0 else None
        capacity = bool(ra and rb and rb.after.max_connections < ra.after.max_connections)
        arrival = ratio is not None and ratio >= 1.5
        same_timeout = bool(ra and rb and ra.after.pool_timeout_ms == rb.after.pool_timeout_ms)
        if phase == "no_anomaly" and session._payload["alert"]["failed_requests"] == 0:
            code = "no_anomaly_observed"
        elif phase != "unknown" and sum((slow, capacity, arrival)) >= 2:
            code = "competing_changes"
        elif slow:
            code = "downstream_change"
        elif phase == "pre_send" and b["inventory_ms"] < 150 and b["orders_ms"] - a["orders_ms"] >= 100:
            if capacity and ratio is not None and .9 <= ratio <= 1.1 and same_timeout:
                code = "capacity_change"
            elif arrival and ra.after.max_connections == rb.after.max_connections and same_timeout:
                code = "arrival_change"
    refs = []
    for window, stats in (("reference", a), ("incident", b)):
        if stats:
            for field in ("inventory_ms", "pre_send_ms", "orders_ms"):
                refs.append(PairedReference(snapshot_id=key, window=window, source="trace",
                    field=f"requests.{stats[field + '_index']}.{field}"))
            if stats["pool_timeout"]:
                i = next(i for i, row in enumerate(data[window]["trace"]["requests"]) if row["error_type"] == "PoolTimeout")
                refs.append(PairedReference(snapshot_id=key, window=window, source="trace", field=f"requests.{i}.error_type"))
        if runtimes[0 if window == "reference" else 1]:
            for field in ("before.requests_started", "send_end.requests_started", "before.monotonic_us",
                          "send_end.monotonic_us", "after.max_connections", "after.pool_timeout_ms"):
                refs.append(PairedReference(snapshot_id=key, window=window, source="runtime", field=field))
    messages = dict(no_anomaly_observed="两个窗口的已采样阶段未见阈值异常。",
        downstream_change="库存样本耗时较参考窗口升高，支持下游变化解释，不能排除其他并存原因。",
        capacity_change="发送前耗时升高，池容量下降且发送区间入口速率受控，支持容量变化解释。",
        arrival_change="发送前耗时升高，池容量未变且发送区间入口速率上升，支持入口变化解释。",
        competing_changes="观测到多个同时变化的候选因素，保留竞争解释，不能归为唯一原因。",
        insufficient_evidence="已保留可定位的事件阶段，但比较证据不足或不满足已定义变化条件。")
    finding = PairedFinding(code=code, stage=phase, summary=messages[code], references=refs, limitations=[])
    domain.validate_finding(finding)
    return domain.report(finding=finding)
