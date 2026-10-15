"""显式埋点：每个进程一个 Provider，使用 W3C traceparent 跨服务传播。"""

import asyncio
import os
from contextlib import asynccontextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

propagator = TraceContextTextMapPropagator()


@asynccontextmanager
async def tracing_lifespan(app, service: str):
    provider = TracerProvider(
        resource=Resource.create({"service.name": service}),
        sampler=ParentBased(ALWAYS_ON),
    )
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if endpoint:
        # 后台批量导出；Jaeger 不可用不会让业务请求等待导出。
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, timeout=2),
            schedule_delay_millis=500,
        ))
    app.state.tracer = provider.get_tracer("incident-investigator.lab")
    try:
        yield
    finally:
        # 正常关闭时排空队列，避免重建容器丢掉尾部 span。
        await asyncio.to_thread(provider.shutdown)


def trace_fields() -> dict:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {"trace_id": f"{context.trace_id:032x}", "span_id": f"{context.span_id:016x}"}


async def get_inventory_with_trace(client, tracer, sku: str, request_id: str):
    with tracer.start_as_current_span("GET inventory", kind=SpanKind.CLIENT) as span:
        span.set_attribute("http.request.method", "GET")
        span.set_attribute("server.address", client.base_url.host)
        span.set_attribute("request.id", request_id)
        headers = {"X-Request-ID": request_id}
        # 在 CLIENT span 内 inject，库存 SERVER span 才会成为它的子节点。
        propagator.inject(headers)
        response = await client.get(f"/inventory/{sku}", headers=headers)
        span.set_attribute("http.response.status_code", response.status_code)
        response.raise_for_status()
        return response
