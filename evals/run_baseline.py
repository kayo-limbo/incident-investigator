"""评测层持有真值和控制权；调查进程只接收一个 Trace ID。"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import httpx

from lab.run_delay_experiment import ROOT, compose
from lab.run_trace_experiment import wait_for_trace


def main():
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "cases": []}
    output = ROOT / "docs" / "experiments" / "baseline-results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    jaeger = f"http://127.0.0.1:{os.getenv('JAEGER_PORT', '16686')}"
    orders = f"http://127.0.0.1:{os.getenv('ORDERS_PORT', '8000')}/orders/preview?sku=demo"
    scenarios = [("healthy", 0, 8001, "no_anomaly_observed"),
                 ("latency", 500, 8001, "inventory_latency"),
                 ("wrong_port", 0, 8002, "endpoint_mismatch"),
                 ("stopped_inventory", 0, 8001, "insufficient_evidence"),
                 ("recovered", 0, 8001, "no_anomaly_observed")]
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            for name, delay, port, expected in scenarios:
                compose("up", "-d", "--wait", delay=delay, inventory_url=f"http://inventory:{port}")
                if name == "stopped_inventory":
                    compose("stop", "inventory", delay=0)
                response = client.get(orders)
                expected_status = 503 if name in ("wrong_port", "stopped_inventory") else 200
                assert response.status_code == expected_status, response.text
                trace_id = response.headers["X-Trace-ID"]
                wait_for_trace(client, jaeger, trace_id, 2 if expected_status == 503 else 3)
                # 不传场景名或预期答案，不把故障控制函数交给调查器。
                run = subprocess.run([sys.executable, "-m", "backend.investigation", trace_id],
                                     cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                     env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=45, check=True)
                investigation = json.loads(run.stdout)
                actual = investigation["finding"]["code"]
                case = {"scenario": name, "expected": expected, "actual": actual,
                        "passed": actual == expected, "investigation": investigation}
                report["cases"].append(case)
                print(f"{name}: {actual} ({'PASS' if case['passed'] else 'FAIL'})", flush=True)
                assert case["passed"], case
    finally:
        try:
            compose("up", "-d", "--wait", delay=0)
            with httpx.Client(timeout=5, trust_env=False) as client:
                response = client.get(orders)
                report["recovery_verified"] = response.status_code == 200 and response.json().get("can_order") is True
                assert report["recovery_verified"]
        finally:
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved: {output}")


if __name__ == "__main__":
    main()
