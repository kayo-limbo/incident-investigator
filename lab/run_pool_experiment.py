"""真实 HTTP/1.1 连接池竞争实验；真值仅由本脚本持有。"""

import asyncio
import json
import math
import os
from datetime import datetime, timezone
from time import perf_counter

import httpx

from lab.run_delay_experiment import ROOT, compose
from lab.run_trace_experiment import wait_for_trace


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


async def load(url: str, concurrency: int, requests: int = 24) -> list[dict]:
    """固定数量的同步突发批次；每批等待结束后发下一批，不是固定到达率负载。"""
    results = []
    async with httpx.AsyncClient(timeout=10, trust_env=False,
                                 limits=httpx.Limits(max_connections=32)) as client:
        warmup = await client.get(url)
        warmup.raise_for_status()

        async def send():
            started = perf_counter()
            response = await client.get(url)
            if response.status_code == 200:
                assert response.json() == {"sku": "demo", "can_order": True}
            assert response.status_code in (200, 503), response.status_code
            return {"trace_id": response.headers["X-Trace-ID"], "status": response.status_code,
                    "client_ms": (perf_counter() - started) * 1000}

        for offset in range(0, requests, concurrency):
            results.extend(await asyncio.gather(*(send() for _ in range(min(concurrency, requests - offset)))))
    return results


def observe_trace(raw: dict, sample: dict) -> dict:
    spans = raw["spans"]
    def tags(span):
        return {tag["key"]: tag["value"] for tag in span["tags"]}
    def select(service, kind):
        return [s for s in spans if raw["processes"][s["processID"]]["serviceName"] == service
                and tags(s).get("span.kind") == kind]
    roots, clients, leaves = select("orders", "server"), select("orders", "client"), select("inventory", "server")
    assert len(roots) == len(clients) == 1
    root, child = roots[0], clients[0]
    attributes = tags(child)
    assert tags(root)["http.response.status_code"] == sample["status"]
    assert any(r["spanID"] == root["spanID"] and r["refType"] == "CHILD_OF" for r in child["references"])
    if sample["status"] == 200:
        assert len(leaves) == 1 and len(spans) == 3
        assert attributes["client.request_headers_started"] is True
        assert any(r["spanID"] == child["spanID"] and r["refType"] == "CHILD_OF" for r in leaves[0]["references"])
    else:
        assert attributes.get("error.type") == "PoolTimeout", attributes.get("error.type")
        assert attributes["client.request_headers_started"] is False
        assert not leaves and len(spans) == 2
    return {**sample, "orders_ms": root["duration"] / 1000,
            "inventory_ms": leaves[0]["duration"] / 1000 if leaves else None,
            "error_type": attributes.get("error.type"),
            "pre_send_ms": attributes.get("client.pre_send_ms"),
            "connect_ms": attributes.get("client.connect_ms"),
            "transport_ms": attributes["client.transport_ms"],
            "in_flight_at_start": attributes["client.in_flight_at_start"],
            "observed_max_connections": attributes["client.max_connections"],
            "observed_pool_timeout_ms": attributes["client.pool_timeout_ms"]}


def summarize(samples):
    def values(key):
        return [s[key] for s in samples if s[key] is not None]
    return {"requests": len(samples), "successes": sum(s["status"] == 200 for s in samples),
            "pool_timeouts": sum(s["error_type"] == "PoolTimeout" for s in samples),
            "orders_p95_ms": percentile(values("orders_ms"), .95),
            "inventory_p95_ms": percentile(values("inventory_ms"), .95),
            "pre_send_success_p95_ms": percentile(values("pre_send_ms"), .95),
            "peak_in_flight_at_start": max(values("in_flight_at_start"))}


def check_case(name, summary):
    """实验验收阈值，属于评测真值；不是提供给 Agent 的诊断规则。"""
    assert summary["successes"] > 0, (name, summary)
    if name == "slow_causes_pool_wait":
        assert summary["pool_timeouts"] > 0, (name, summary)
    elif name != "pool_small":
        assert summary["pool_timeouts"] == 0, (name, summary)
    if name in ("downstream_slow", "slow_causes_pool_wait"):
        assert summary["inventory_p95_ms"] >= 450, (name, summary)
    else:
        assert summary["inventory_p95_ms"] < 250, (name, summary)
    if name == "pool_small":
        assert summary["pre_send_success_p95_ms"] >= 50, (name, summary)


def main():
    output = ROOT / "docs" / "experiments" / "pool-results.json"
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "cases": [],
              "load": "24 requests in synchronized batches; one excluded warmup per case",
              "completed": False, "recovery_verified": False}
    orders = f"http://127.0.0.1:{os.getenv('ORDERS_PORT', '8000')}/orders/preview?sku=demo"
    jaeger = f"http://127.0.0.1:{os.getenv('JAEGER_PORT', '16686')}"
    # 80 ms 是同一固定工作负载的正常服务时间，500 ms 是慢下游条件。
    cases = [("healthy", 80, 4, 8), ("healthy_wide_pool", 80, 8, 8),
             ("downstream_slow", 500, 8, 8), ("pool_small", 80, 1, 8),
             ("slow_causes_pool_wait", 500, 4, 8), ("small_pool_low_load", 80, 1, 1),
             ("recovered", 80, 4, 8)]
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            for name, delay, connections, concurrency in cases:
                compose("up", "-d", "--wait", "--wait-timeout", "60", delay=delay,
                        max_connections=connections, pool_timeout_ms=250)
                samples = asyncio.run(load(orders, concurrency))
                observed = [observe_trace(wait_for_trace(client, jaeger, s["trace_id"],
                                                        3 if s["status"] == 200 else 2), s) for s in samples]
                assert all(s["observed_max_connections"] == connections and
                           s["observed_pool_timeout_ms"] == 250 for s in observed)
                summary = summarize(observed)
                report["cases"].append({"name": name, "truth": {"delay_ms": delay,
                    "max_connections": connections, "pool_timeout_ms": 250, "concurrency": concurrency},
                    "summary": summary, "samples": observed})
                check_case(name, summary)
                report["cases"][-1]["passed"] = True
                print(f"{name}: {json.dumps(summary)} PASS", flush=True)
            report["completed"] = True
    finally:
        try:
            compose("up", "-d", "--wait", "--wait-timeout", "60", delay=0)
            with httpx.Client(timeout=5, trust_env=False) as client:
                response = client.get(orders)
                report["recovery_verified"] = response.status_code == 200 and response.json() == {
                    "sku": "demo", "can_order": True}
                assert report["recovery_verified"]
        finally:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved: {output}")


if __name__ == "__main__":
    main()
