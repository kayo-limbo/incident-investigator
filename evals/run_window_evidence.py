"""真实窗口工具验收；场景真值只在此验证器中使用，不传给调查工具。"""

import asyncio
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone

import httpx

from backend.investigation.evidence_models import ObservationRef, WindowQuery
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_queries import WindowEvidenceTools
from lab.run_delay_experiment import ROOT, compose
from lab.run_pool_experiment import load
from lab.run_trace_experiment import wait_for_trace


def main():
    store = SnapshotStore(ROOT / ".snapshots")
    tools = WindowEvidenceTools(store)
    orders = f"http://127.0.0.1:{os.getenv('ORDERS_PORT', '8000')}/orders/preview?sku=demo"
    jaeger = f"http://127.0.0.1:{os.getenv('JAEGER_PORT', '16686')}"
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "cases": [],
              "completed": False, "recovery_verified": False}
    try:
        for name, delay, connections in (("healthy", 80, 8), ("pool_wait", 80, 1),
                                          ("slow_and_pool_timeout", 500, 4)):
            compose("up", "-d", "--wait", "--wait-timeout", "60", delay=delay,
                    max_connections=connections, pool_timeout_ms=250)
            start = time.time_ns() // 1000
            samples = asyncio.run(load(orders, concurrency=8))
            end = time.time_ns() // 1000
            with httpx.Client(timeout=5, trust_env=False) as client:
                for sample in samples:
                    wait_for_trace(client, jaeger, sample["trace_id"], 3 if sample["status"] == 200 else 2)
            evidence = tools.query_window(WindowQuery(start_us=start, end_us=end))
            assert evidence.status == "ok", evidence.model_dump()
            assert evidence.data["quality"] == "observed", evidence.data
            key = evidence.data["snapshot_id"]
            payload = store.read(key)
            expected_ids = {s["trace_id"] for s in samples}
            # load 有一次预热：窗口包含它，而独立负载结果不包含它。
            assert expected_ids <= set(payload["traces"])
            assert len(payload["traces"]) == len(samples) + 1
            statuses = Counter(str(s["status"]) for s in samples)
            statuses["200"] += 1
            assert evidence.data["summary"]["http_status_counts"] == dict(statuses)
            for sample in samples:
                detail = tools.get_snapshot_trace(key, sample["trace_id"])
                assert detail.status == "ok"
                children = [s for s in detail.data["spans"] if s["tags"].get("span.kind") == "client"]
                assert len(children) == 1
                ref = ObservationRef(snapshot_id=key, trace_id=sample["trace_id"],
                                     span_id=children[0]["span_id"], field="tags.client.max_connections")
                assert store.resolve(ref) == connections
                if sample["status"] == 503:
                    assert children[0]["tags"]["error.type"] == "PoolTimeout"
            if name == "slow_and_pool_timeout":
                assert statuses["503"] > 0
            report["cases"].append({"name": name, "passed": True,
                                    "summary": evidence.data["summary"], "usage": evidence.data["usage"]})
            print(f"{name}: PASS ({len(samples)} requests + warmup)", flush=True)
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
            path = ROOT / "docs" / "experiments" / "window-evidence-results.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Window evidence checks passed; business recovery verified.")


if __name__ == "__main__":
    main()
