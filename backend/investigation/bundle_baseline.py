"""Fixed-query comparator using exactly the same frozen bundle tools."""

import math

from .bundle_agent import BundleDomain, BundleFinding
from .bundle_evidence import BundleReference, BundleSession


def investigate_bundle_fixed(store, key, max_tool_calls=6):
    domain = BundleDomain(BundleSession(store, key, max_tool_calls), "bundle_fixed_v1")
    session = domain.session
    if session._payload is None:
        return domain.report(termination="source_error")
    if session.remaining < 2:
        return domain.report(termination="budget_exhausted")
    # Fixed order; it is allowed to inspect all observations just like the agents.
    trace = session.query("trace_window").data
    session.query("client_runtime")
    if trace["quality"] != "observed":
        return domain.report(reason="已知订单症状，但缺少请求分段证据，无法区分库存慢与调用方等待。")
    rows = trace["requests"]
    def p95(field):
        values = sorted(r[field] for r in rows if r[field] is not None)
        return values[math.ceil(.95 * len(values)) - 1] if values else None
    slow_value, wait_value = p95("inventory_ms"), p95("pre_send_ms")
    if slow_value is None:
        return domain.report(reason="没有库存耗时样本。")
    slow, wait = slow_value >= 250, wait_value is not None and wait_value >= 150
    pool = any(r["error_type"] == "PoolTimeout" for r in rows)
    if slow and (wait or pool):
        code = "inventory_latency_with_pool_contention"
    elif slow:
        code = "inventory_latency"
    elif pool:
        code = "pool_contention"
    elif wait:
        code = "pre_send_delay"
    elif p95("orders_ms") < 250 and all(r["status"] == 200 for r in rows):
        code = "no_anomaly_observed"
    else:
        return domain.report()
    refs = []
    for field in ("inventory_ms", "pre_send_ms", "orders_ms"):
        available = [i for i, r in enumerate(rows) if r[field] is not None]
        if available:
            index = max(available, key=lambda i: rows[i][field])
            refs.append(BundleReference(snapshot_id=key, source="trace_window", field=f"requests.{index}.{field}"))
    if pool:
        index = next(i for i, r in enumerate(rows) if r["error_type"] == "PoolTimeout")
        refs.append(BundleReference(snapshot_id=key, source="trace_window", field=f"requests.{index}.error_type"))
    refs.append(BundleReference(snapshot_id=key, source="client_runtime", field="after.max_connections"))
    text = {"inventory_latency": "库存处理耗时升高。", "pre_send_delay": "发送前等待升高，库存耗时未超阈值。",
        "pool_contention": "观察到连接池获取超时。", "no_anomaly_observed": "已采样请求未见阈值异常。",
        "inventory_latency_with_pool_contention": "库存耗时与等待或池超时并存，不能证明独立双根因。"}[code]
    finding = BundleFinding(code=code, summary=text, references=refs, limitations=[])
    domain.validate_finding(finding)
    return domain.report(finding)
