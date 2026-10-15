"""验证 Jaeger 实际收到的跨服务链路，而不只检查业务响应。"""

import json
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from lab.run_delay_experiment import ROOT, compose


def wait_for_trace(client, jaeger_url, trace_id, count):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            response = client.get(f"{jaeger_url}/api/traces/{trace_id}")
        except httpx.TransportError:
            time.sleep(0.5)
            continue
        if response.status_code == 200:
            data = response.json().get("data", [])
            if data and len(data[0]["spans"]) >= count:
                return data[0]
        elif response.status_code != 404:
            response.raise_for_status()
        time.sleep(0.5)  # SDK 批量导出与 Jaeger 入库有短暂延迟。
    raise RuntimeError(f"Trace did not arrive: {trace_id}")


def verify_trace(data, request_id, expected_status):
    spans = data["spans"]
    processes = data["processes"]

    def tags(span):
        return {tag["key"]: tag["value"] for tag in span["tags"]}

    def select(service, kind):
        found = [s for s in spans if processes[s["processID"]]["serviceName"] == service
                 and tags(s).get("span.kind") == kind]
        assert len(found) == 1, (service, kind, spans)
        return found[0]

    server = select("orders", "server")
    downstream = select("orders", "client")
    assert tags(server)["http.response.status_code"] == expected_status
    assert any(r["refType"] == "CHILD_OF" and r["spanID"] == server["spanID"]
               for r in downstream["references"])
    assert all(tags(s).get("request.id") == request_id for s in spans)
    assert all(s["traceID"] == data["traceID"] for s in spans)
    result = {"orders_ms": server["duration"] / 1000,
              "inventory_call_ms": downstream["duration"] / 1000}
    if expected_status == 200:
        inventory = select("inventory", "server")
        assert len(spans) == 3
        assert any(r["refType"] == "CHILD_OF" and r["spanID"] == downstream["spanID"]
                   for r in inventory["references"])
        assert tags(inventory)["http.response.status_code"] == 200
        result["inventory_ms"] = inventory["duration"] / 1000
    else:
        assert len(spans) == 2
        assert tags(server).get("error") is True
        assert tags(downstream).get("error") is True
    return result


def main():
    jaeger_url = f"http://127.0.0.1:{os.getenv('JAEGER_PORT', '16686')}"
    order_url = f"http://127.0.0.1:{os.getenv('ORDERS_PORT', '8000')}"
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "cases": []}
    output = ROOT / "docs" / "experiments" / "trace-results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            for name, delay, status in (("baseline", 0, 200), ("delayed", 500, 200),
                                        ("unavailable", 0, 503), ("recovered", 0, 200)):
                compose("up", "-d", "--wait", delay=delay)
                if name == "unavailable":
                    compose("stop", "inventory", delay=0)
                request_id = uuid4().hex
                response = client.get(f"{order_url}/orders/preview?sku=demo",
                                      headers={"X-Request-ID": request_id})
                assert response.status_code == status, response.text
                trace_id = response.headers["X-Trace-ID"]
                data = wait_for_trace(client, jaeger_url, trace_id, 3 if status == 200 else 2)
                durations = verify_trace(data, request_id, status)
                logs = compose("logs", "--no-color", "--no-log-prefix", "orders", "inventory", delay=delay)
                matched = []
                for line in logs.splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("request_id") == request_id:
                        matched.append(event)
                assert len(matched) == (3 if status == 200 else 2)
                assert all(e.get("trace_id") == trace_id for e in matched)
                assert all(e.get("span_id") in {s["spanID"] for s in data["spans"]} for e in matched)
                report["cases"].append({"name": name, "injected_delay_ms": delay,
                                        "trace_id": trace_id, "durations": durations,
                                        "trace": data, "logs": matched})
                print(name, trace_id, durations, flush=True)
            health = client.get(f"{order_url}/health")
            assert health.status_code == 200 and "X-Trace-ID" not in health.headers
            # 验证的是显式注入的等待是否被观测到，不是机器性能门槛。
            delayed = report["cases"][1]["durations"]["inventory_ms"]
            assert delayed >= 450, delayed
    finally:
        try:
            compose("up", "-d", "--wait", delay=0)
        finally:
            output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved: {output}")


if __name__ == "__main__":
    main()
