"""教学用结构化日志：关联请求并使用单调时钟测量耗时。"""

import json
import re
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from opentelemetry.trace import SpanKind, Status, StatusCode
from lab.tracing import propagator, trace_fields


def emit(service: str, event: str, **fields) -> None:
    print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(),
                      "service": service, "event": event, **trace_fields(), **fields}), flush=True)


def install_request_logging(app: FastAPI, service: str) -> None:
    @app.middleware("http")
    async def log_request(request: Request, call_next):
        if request.url.path in ("/health", "/docs", "/openapi.json", "/redoc"):
            return await call_next(request)
        candidate = request.headers.get("X-Request-ID", "")
        # 限制外部输入，避免把任意内容写进关联字段。
        request_id = candidate if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", candidate) else uuid4().hex
        request.state.request_id = request_id
        # extract 恢复远端父节点；请求 ID 只是日志标识，不负责链路传播。
        parent = propagator.extract(dict(request.headers))
        with request.app.state.tracer.start_as_current_span(
            request.method, context=parent, kind=SpanKind.SERVER,
        ) as span:
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("request.id", request_id)
            started = perf_counter()
            status = 500
            try:
                response = await call_next(request)
                status = response.status_code
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Trace-ID"] = trace_fields()["trace_id"]
                return response
            finally:
                route = request.scope.get("route")
                route_path = getattr(route, "path", None)
                if route_path:
                    span.update_name(f"{request.method} {route_path}")
                    span.set_attribute("http.route", route_path)
                span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR))
                emit(service, "request_completed", request_id=request_id,
                     status_code=status, duration_ms=round((perf_counter() - started) * 1000, 3))
