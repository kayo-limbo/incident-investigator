"""从项目根目录运行：python -m lab.run_delay_experiment。

对当前 Compose 实验环境依次施加 0/500/0 ms 延迟，最后恢复为 0。
"""

import json
import os
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


def compose(*args: str, delay: int, inventory_url: str = "http://inventory:8001",
            max_connections: int = 100, pool_timeout_ms: int = 2000) -> str:
    result = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT,
        env={**os.environ, "INVENTORY_DELAY_MS": str(delay), "INVENTORY_BASE_URL": inventory_url,
             "INVENTORY_MAX_CONNECTIONS": str(max_connections),
             "INVENTORY_POOL_TIMEOUT_MS": str(pool_timeout_ms)},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def main():
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(),
              "requests_per_phase": 10, "concurrency": 1, "phases": []}
    port = os.getenv("ORDERS_PORT", "8000")
    url = f"http://127.0.0.1:{port}/orders/preview?sku=demo"
    output = ROOT / "docs" / "experiments" / "delay-results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            for name, delay in (("baseline", 0), ("delayed", 500), ("recovered", 0)):
                compose("up", "-d", "--wait", "--wait-timeout", "60", delay=delay)
                client.get(url).raise_for_status()  # 每组同样预热一次，不计入样本。
                samples = []
                for _ in range(10):
                    request_id = uuid4().hex
                    started = perf_counter()
                    response = client.get(url, headers={"X-Request-ID": request_id})
                    elapsed = (perf_counter() - started) * 1000
                    assert response.status_code == 200, response.text
                    assert response.json() == {"sku": "demo", "can_order": True}
                    assert response.headers["X-Request-ID"] == request_id
                    samples.append({"request_id": request_id, "client_ms": round(elapsed, 3)})
                # 在下一组重建库存容器前保存日志，并按请求 ID 匹配三条观测。
                events = {}
                logs = compose("logs", "--no-color", "--no-log-prefix", "orders", "inventory", delay=delay)
                for line in logs.splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and "request_id" in event:
                        events.setdefault(event["request_id"], []).append(event)
                for sample in samples:
                    matched = events.get(sample["request_id"], [])
                    for service, event, key in (
                        ("inventory", "request_completed", "inventory_ms"),
                        ("orders", "inventory_call", "inventory_call_ms"),
                        ("orders", "request_completed", "orders_ms"),
                    ):
                        found = [e for e in matched if e["service"] == service and e["event"] == event]
                        assert len(found) == 1, f"Missing or duplicate log: {sample['request_id']} {key}"
                        sample[key] = found[0]["duration_ms"]
                medians = {key: round(statistics.median(s[key] for s in samples), 3)
                           for key in ("client_ms", "inventory_ms", "inventory_call_ms", "orders_ms")}
                report["phases"].append({"name": name, "injected_delay_ms": delay,
                                         "medians": medians, "samples": samples})
                print(name, medians, flush=True)
    finally:
        # 即使断言失败也移除注入的延迟；不删除用户的容器或镜像。
        try:
            compose("up", "-d", "--wait", "--wait-timeout", "60", delay=0)
        finally:
            output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved: {output}")


if __name__ == "__main__":
    main()
