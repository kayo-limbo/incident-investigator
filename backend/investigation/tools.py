"""仅四个有边界的观测工具。没有任意 URL、Shell 或文件读取接口。"""

import json
import math
import os
import subprocess
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx

from .models import Evidence, TraceQuery

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {"scheme": "http", "host": "inventory", "port": 8001}
TAGS = {"span.kind", "http.response.status_code", "http.request.method", "http.route",
        "server.address", "server.port", "error", "error.type"}


class ToolFailure(Exception):
    pass


def project_trace(raw: dict, trace_id: str) -> dict:
    """不返回进程环境、异常堆栈、任意标签或日志原文。超限直接拒绝。"""
    traces = raw.get("data", [])
    if not traces:
        raise ToolFailure("not_found")
    if len(traces) != 1 or traces[0]["traceID"] != trace_id:
        raise ToolFailure("invalid_response")
    trace = traces[0]
    if len(trace["spans"]) > 50:
        raise ToolFailure("too_large")
    result = []
    for span in trace["spans"]:
        if span["traceID"] != trace_id:
            raise ToolFailure("invalid_response")
        tags = {t["key"]: t["value"] for t in span["tags"] if t["key"] in TAGS}
        if any(not isinstance(v, (str, int, float, bool)) or len(str(v)) > 256 for v in tags.values()):
            raise ToolFailure("invalid_response")
        for value in (span["startTime"], span["duration"]):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ToolFailure("invalid_response")
        service = trace["processes"][span["processID"]]["serviceName"]
        if any(not isinstance(value, str) or len(value) > 256
               for value in (span["spanID"], span["operationName"], service)):
            raise ToolFailure("invalid_response")
        refs = [r["spanID"] for r in span["references"]
                if r["refType"] == "CHILD_OF" and r["traceID"] == trace_id]
        result.append({"span_id": span["spanID"], "parents": refs,
                       "service": service,
                       "operation": span["operationName"], "start_us": span["startTime"],
                       "duration_ms": span["duration"] / 1000, "tags": tags})
    if len(json.dumps(result)) > 32000:
        raise ToolFailure("too_large")
    return {"trace_id": trace_id, "spans": result}


class EvidenceTools:
    def _read(self, tool, source, parameters, action):
        evidence = Evidence(tool=tool, source=source, parameters=parameters)
        try:
            evidence.data = action()
        except ToolFailure as exc:
            evidence.status, evidence.error_code = "error", str(exc)
        except (httpx.TimeoutException, subprocess.TimeoutExpired):
            evidence.status, evidence.error_code = "error", "timeout"
        except (httpx.HTTPError, OSError, subprocess.CalledProcessError):
            evidence.status, evidence.error_code = "error", "source_unavailable"
        except (ValueError, KeyError, TypeError, AttributeError):
            evidence.status, evidence.error_code = "error", "invalid_response"
        return evidence

    def get_trace(self, trace_id: str) -> Evidence:
        query = TraceQuery(trace_id=trace_id)
        port = int(os.getenv("JAEGER_PORT", "16686"))
        source = f"http://127.0.0.1:{port}/api/traces/{query.trace_id}"

        def read():
            started = monotonic()
            chunks, size = [], 0
            with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as client:
                with client.stream("GET", source) as response:
                    if response.status_code == 404:
                        raise ToolFailure("not_found")
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 1_000_000:
                            raise ToolFailure("too_large")
                        if monotonic() - started > 5:
                            raise ToolFailure("timeout")
                        chunks.append(chunk)
            return project_trace(json.loads(b"".join(chunks)), trace_id)

        return self._read("get_trace", source, query.model_dump(), read)

    def get_service_topology(self) -> Evidence:
        return Evidence(tool="get_service_topology", source="lab-contract:v1",
                        data={"caller": "orders", "callee": "inventory", "expected_endpoint": EXPECTED.copy()})

    def _docker(self, *args):
        # 命令只由下面两个固定适配器调用，无用户参数拼接，不使用 shell。
        result = subprocess.run(["docker", "compose", *args], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=10, check=True)
        if len(result.stdout) > 8192:
            raise ToolFailure("too_large")
        return result.stdout.strip()

    def get_runtime_config(self) -> Evidence:
        def read():
            # 只读这一项，不枚举全环境变量；去掉用户信息、路径、查询串。
            value = self._docker("exec", "-T", "orders", "printenv", "INVENTORY_BASE_URL")
            url = urlsplit(value)
            if url.scheme not in ("http", "https") or not url.hostname:
                raise ToolFailure("invalid_response")
            return {"scheme": url.scheme, "host": url.hostname,
                    "port": url.port or (443 if url.scheme == "https" else 80)}

        return self._read("get_runtime_config", "docker-compose:orders/INVENTORY_BASE_URL", {}, read)

    def probe_inventory(self) -> Evidence:
        def read():
            # 地址固定为服务契约地址，不能借此访问任意 URL；GET 不修改业务状态。
            code = (
                "import httpx,json; "
                "r=httpx.get('http://inventory:8001/inventory/demo',timeout=3,trust_env=False); "
                "print(json.dumps({'status_code':r.status_code,'body':r.json()}))"
            )
            result = json.loads(self._docker("exec", "-T", "orders", "python", "-c", code))
            return {"status_code": result["status_code"],
                    "business_ok": result["status_code"] == 200 and result["body"] == {"sku": "demo", "available": 10}}

        return self._read("probe_inventory", "orders-network:http://inventory:8001/inventory/demo", {}, read)
