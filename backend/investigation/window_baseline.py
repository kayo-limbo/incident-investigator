"""窗口规则基线：识别观测模式，不把实验控制参数当作诊断输入。"""

from .window_investigation import FrozenWindowSession, WindowFinding, WindowReference

# 首版业务阈值，公开给两种策略；不是通用 SLO 或学习出的根因规则。
INVENTORY_SLOW_MS = 250
PRE_SEND_SLOW_MS = 150


def investigate_window_fixed(store, snapshot_id, max_tool_calls=6):
    session = FrozenWindowSession(store, snapshot_id, max_tool_calls)
    strategy = "window_fixed_v1"
    if session._payload is None:
        return session.report(strategy, termination="source_error", reason="快照无法读取或无效。")
    if not session.usable:
        return session.report(strategy, reason="窗口为空、部分数据或必要观测缺失。")
    context = session.context()
    summary = context.evidence[0].data["summary"]
    rows = context.evidence[0].data["trace_index"]
    slow = summary["measurements"]["inventory_ms"]["p95_ms"]
    wait = summary["measurements"]["pre_send_ms"]["p95_ms"]
    slow = slow is not None and slow >= INVENTORY_SLOW_MS
    wait = wait is not None and wait >= PRE_SEND_SLOW_MS
    pool = summary["client_error_counts"].get("PoolTimeout", 0) > 0
    if slow and (pool or wait):
        code, text = "inventory_latency_with_pool_contention", "库存耗时升高与连接竞争或发送前等待迹象并存，不能归为唯一根因。"
    elif slow:
        code, text = "inventory_latency", "窗口样本显示库存服务内耗时升高。"
    elif pool:
        code, text = "pool_contention", "出现获取连接超时，配置与负载的因果关系仍需后续调查。"
    elif wait:
        code, text = "pre_send_delay", "发送前耗时升高而库存耗时未超阈值；无法仅凭此确定纯连接池等待。"
    elif summary["orders_p95_ms"] >= 250:
        return session.report(strategy, reason="订单延迟升高但当前观测不能解释。")
    else:
        code, text = "no_anomaly_observed", "已采集窗口样本未观察到超过当前阈值的异常。"
    chosen = []
    for field in ("inventory_ms", "pre_send_ms"):
        candidates = [r for r in rows if r[field] is not None]
        if candidates:
            chosen.append(max(candidates, key=lambda r: r[field])["trace_id"])
    # 超时样本只取一个；两种耗时各选一个最大值用于字段级举证。
    selected = list(dict.fromkeys(chosen + ([next(r["trace_id"] for r in rows if r["error_type"] == "PoolTimeout")] if pool else [])))
    if len(selected) > session.remaining:
        return session.report(strategy, termination="budget_exhausted", reason="预算不足以读取所需证据。")
    refs = []
    for trace_id in selected:
        evidence = session.drilldown(trace_id)
        for span in evidence.data["spans"]:
            fields = []
            if span["service"] == "inventory" and span["tags"].get("span.kind") == "server":
                fields.append("duration_ms")
            if span["service"] == "orders" and span["tags"].get("span.kind") == "server":
                fields.append("tags.http.response.status_code")
            if span["service"] == "orders" and span["tags"].get("span.kind") == "client":
                fields += ["tags." + key for key in ("client.pre_send_ms", "error.type",
                    "client.max_connections", "client.pool_timeout_ms", "client.request_headers_started") if key in span["tags"]]
            refs.extend(WindowReference(snapshot_id=snapshot_id, trace_id=trace_id,
                                       span_id=span["span_id"], field=field) for field in fields)
    finding = WindowFinding(code=code, summary=text, references=refs, limitations=[])
    session.validate_finding(finding)
    return session.report(strategy, finding)
