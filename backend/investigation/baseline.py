"""固定顺序、有限调用的基线；不使用 LLM，也不读取实验真值。"""

import time

from .models import Finding, Investigation, TraceQuery
from .tools import EvidenceTools


def investigate(trace_id: str, tools: EvidenceTools | None = None) -> Investigation:
    TraceQuery(trace_id=trace_id)
    tools = tools or EvidenceTools()
    evidence = []

    def finish(code, summary, *limits):
        return Investigation(trace_id=trace_id, evidence=evidence, finding=Finding(
            code=code, summary=summary, evidence_ids=[e.id for e in evidence],
            limitations=["仅分析单次请求，不代表系统整体状态。", *limits]))

    def unknown(reason):
        return finish("insufficient_evidence", reason)

    trace = tools.get_trace(trace_id)
    evidence.append(trace)
    if trace.status != "ok":
        return unknown("无法读取完整调用链；不能根据数据源错误推断业务故障。")
    spans = trace.data["spans"]
    roots = [s for s in spans if s["service"] == "orders" and s["tags"].get("span.kind") == "server"]
    children = [s for s in spans if s["service"] == "orders" and s["tags"].get("span.kind") == "client"]
    leaves = [s for s in spans if s["service"] == "inventory" and s["tags"].get("span.kind") == "server"]
    if len(roots) != 1 or len(children) != 1 or len({s["span_id"] for s in spans}) != len(spans):
        return unknown("链路不符合当前双服务基线的结构要求。")
    root, child = roots[0], children[0]
    if child["parents"] != [root["span_id"]] or root["operation"] != "GET /orders/preview":
        return unknown("父子关系或入口不符合当前排查范围。")
    status = root["tags"].get("http.response.status_code")
    if len(leaves) == 1 and len(spans) == 3:
        leaf = leaves[0]
        if leaf["parents"] != [child["span_id"]] or leaf["tags"].get("http.response.status_code") != 200:
            return unknown("库存响应或父子关系异常，当前规则无法定位具体原因。")
        # 教学用固定阈值，并非生产 SLO 或自动学习得到的阈值。
        if status == 200 and root["duration_ms"] >= 300 and leaf["duration_ms"] >= 300:
            if leaf["duration_ms"] >= root["duration_ms"] * 0.7:
                return finish("inventory_latency", "该请求主要耗时发生在库存服务内。",
                              "阈值为 300 ms、占比 70%；只能定位耗时环节，不能确定库存内部根因。")
        if status == 200 and root["duration_ms"] < 300:
            return finish("no_anomaly_observed", "该请求成功，且未超过基线的 300 ms 延迟阈值。")
        return unknown("观察到耗时或响应异常，但不满足当前定位规则。")
    if leaves or len(spans) != 2 or status != 503 or child["tags"].get("error") is not True:
        return unknown("链路不完整或异常形态不受当前规则支持。")
    # 当前配置不能反推很久以前的请求；限定实时调查窗口。
    age = time.time() - root["start_us"] / 1_000_000
    if not -5 <= age <= 120:
        return unknown("链路超过 120 秒实时窗口，不能用当前配置判定历史根因。")
    topology = tools.get_service_topology()
    evidence.append(topology)
    runtime = tools.get_runtime_config()
    evidence.append(runtime)
    if topology.status != "ok" or runtime.status != "ok":
        return unknown("无法取得服务契约或当前运行配置。")
    expected = topology.data["expected_endpoint"]
    actual = runtime.data
    target = {"host": child["tags"].get("server.address"), "port": child["tags"].get("server.port")}
    if target != {"host": actual["host"], "port": actual["port"]}:
        return unknown("当前配置与请求实际目标不同，可能已经发生变更。")
    if actual == expected:
        return unknown("配置符合服务契约；连接失败仍可能来自服务停止或网络问题。")
    probe = tools.probe_inventory()
    evidence.append(probe)
    if probe.status != "ok" or not probe.data.get("business_ok"):
        return unknown("目标存在差异，但正确地址的业务可达性尚未证实。")
    return finish("endpoint_mismatch", "订单请求目标与库存服务契约不符，正确地址仍可处理业务请求。",
                  "配置与探针来自调查时刻；只适用于当前隔离实验环境，不能证明所有历史请求的原因。")
