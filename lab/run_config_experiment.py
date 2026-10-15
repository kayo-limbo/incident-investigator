"""第二类故障：订单使用错误库存端口，但库存自身仍能正常处理请求。"""

import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from lab.run_delay_experiment import ROOT, compose
from lab.run_trace_experiment import verify_trace, wait_for_trace

GOOD_URL = "http://inventory:8001"
BAD_URL = "http://inventory:8002"


def probe_inventory_from_orders(inventory_url):
    # 从订单容器直接探测真实库存地址，排除宿主机与容器网络视角不同。
    code = (
        "import httpx,json; "
        "r=httpx.get('http://inventory:8001/inventory/demo',timeout=3,trust_env=False); "
        "print(json.dumps({'status_code':r.status_code,'body':r.json()}))"
    )
    result = compose("exec", "-T", "orders", "python", "-c", code,
                     delay=0, inventory_url=inventory_url)
    probe = json.loads(result)
    assert probe == {"status_code": 200, "body": {"sku": "demo", "available": 10}}, probe
    return probe


def main():
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "phases": []}
    output = ROOT / "docs" / "experiments" / "config-results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{os.getenv('ORDERS_PORT', '8000')}/orders/preview?sku=demo"
    jaeger = f"http://127.0.0.1:{os.getenv('JAEGER_PORT', '16686')}"
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            for name, target, expected in (("baseline", GOOD_URL, 200),
                                           ("wrong_port", BAD_URL, 503),
                                           ("recovered", GOOD_URL, 200)):
                compose("up", "-d", "--wait", "--wait-timeout", "60", delay=0, inventory_url=target)
                # 独立读取具体配置值，不把“设置动作成功”当成配置已经生效。
                actual = compose("exec", "-T", "orders", "printenv", "INVENTORY_BASE_URL",
                                 delay=0, inventory_url=target).strip()
                assert actual == target, actual
                phase = {"name": name, "configured_inventory_url": actual,
                         "inventory_probe": probe_inventory_from_orders(target), "samples": []}
                report["phases"].append(phase)
                for _ in range(3):
                    request_id = uuid4().hex
                    response = client.get(url, headers={"X-Request-ID": request_id})
                    assert response.status_code == expected, response.text
                    if expected == 200:
                        assert response.json() == {"sku": "demo", "can_order": True}
                    trace_id = response.headers["X-Trace-ID"]
                    data = wait_for_trace(client, jaeger, trace_id, 3 if expected == 200 else 2)
                    durations = verify_trace(data, request_id, expected)
                    downstream = next(s for s in data["spans"] if s["operationName"] == "GET inventory")
                    tags = {tag["key"]: tag["value"] for tag in downstream["tags"]}
                    assert tags["server.address"] == "inventory"
                    assert tags["server.port"] == (8002 if expected == 503 else 8001)
                    if expected == 503:
                        assert tags["error.type"] == "ConnectError", tags
                    phase["samples"].append({"request_id": request_id, "trace_id": trace_id,
                                             "status_code": response.status_code,
                                             "durations": durations, "trace": data})
                print(f"{name}: 3/3 HTTP {expected}; inventory direct probe HTTP 200", flush=True)
    finally:
        try:
            compose("up", "-d", "--wait", "--wait-timeout", "60", delay=0, inventory_url=GOOD_URL)
            report["restored_inventory_url"] = compose(
                "exec", "-T", "orders", "printenv", "INVENTORY_BASE_URL", delay=0).strip()
            assert report["restored_inventory_url"] == GOOD_URL
        finally:
            output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved: {output}")


if __name__ == "__main__":
    main()
