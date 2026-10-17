"""评测层持有真值和控制权；调查进程只接收一个 Trace ID。"""

import json
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx

from lab.run_delay_experiment import ROOT, compose
from lab.run_trace_experiment import wait_for_trace
from backend.investigation.models import Investigation
from backend.investigation.model import ModelConfig, ModelConfigError


def score(investigation: dict, expected: str) -> bool:
    """退出错误不能冒充合理拒答；评分只核对类别和基本证据完整性。"""
    parsed = Investigation.model_validate(investigation)
    known = {e.id: e for e in parsed.evidence}
    refs = parsed.finding.evidence_ids
    return (parsed.termination == "completed" and parsed.finding.code == expected
            and len(known) == len(parsed.evidence) and len(refs) == len(set(refs))
            and bool(refs) and all(ref in known for ref in refs)
            and any(known[ref].tool == "get_trace" and known[ref].status == "ok" for ref in refs))


def main():
    parser = argparse.ArgumentParser(description="五个本地故障验收案例；持有真值，不向调查器传递")
    parser.add_argument("--strategy", choices=("baseline", "agent"), default="baseline")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-tool-calls", type=int, default=6)
    args = parser.parse_args()
    if not 0 <= args.max_tool_calls <= 100:
        parser.error("工具次数须为 0–100")
    config = None
    if args.strategy == "agent":
        try:
            config = ModelConfig.from_env()
        except ModelConfigError as exc:
            parser.error(str(exc))
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "strategy": args.strategy,
              "model": config.model if config else None, "cases": [], "completed": False}
    output = args.output or ROOT / "docs" / "experiments" / (
        "agent-results.private.json" if config else "baseline-results.json")
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
                started = perf_counter()
                # 排除实验开关；环境隔离只是接口边界，并非操作系统沙箱。
                child_env = {k: v for k, v in os.environ.items()
                             if k not in {"INVENTORY_DELAY_MS", "INVENTORY_BASE_URL"}}
                try:
                    run = subprocess.run([sys.executable, "-m", "backend.investigation", trace_id,
                                          "--strategy", args.strategy, "--max-tool-calls", str(args.max_tool_calls)],
                                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                         env={**child_env, "PYTHONIOENCODING": "utf-8"},
                                         timeout=300 if config else 45, check=True)
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                    report["cases"].append({"scenario": name, "passed": False,
                                            "execution_error": type(exc).__name__})
                    raise RuntimeError("调查子进程失败，详见脱敏评测记录") from None
                investigation = json.loads(run.stdout)
                actual = investigation["finding"]["code"]
                case = {"scenario": name, "expected": expected, "actual": actual,
                        "passed": score(investigation, expected), "investigation": investigation,
                        "duration_ms": round((perf_counter() - started) * 1000, 2),
                        "tool_calls": len(investigation["evidence"])}
                report["cases"].append(case)
                print(f"{name}: {actual} ({'PASS' if case['passed'] else 'FAIL'})", flush=True)
            report["completed"] = True
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
    if not all(case["passed"] for case in report["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
