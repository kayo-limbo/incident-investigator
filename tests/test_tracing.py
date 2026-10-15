"""无需 Docker：验证真实 ASGI 调用中的上下文传播和错误标记。"""

import os
import unittest
from unittest.mock import patch

import httpx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from lab.inventory.main import app as inventory
from lab.orders.main import app as orders


class TracingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.exporter = InMemorySpanExporter()

        def provider(**kwargs):
            result = TracerProvider(**kwargs)
            result.add_span_processor(SimpleSpanProcessor(self.exporter))
            return result

        self.enterContext(patch.dict(os.environ, {"INVENTORY_DELAY_MS": "0",
                                                  "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": ""}))
        self.enterContext(patch("lab.tracing.TracerProvider", side_effect=provider))
        await self.enterAsyncContext(inventory.router.lifespan_context(inventory))
        await self.enterAsyncContext(orders.router.lifespan_context(orders))
        orders.state.inventory_client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=inventory), base_url="http://inventory:8001"))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=orders), base_url="http://orders"))

    async def test_parent_chain_and_response_id(self):
        response = await self.client.get("/orders/preview?sku=demo", headers={"X-Request-ID": "test-chain"})
        self.assertEqual(response.status_code, 200)
        spans = self.exporter.get_finished_spans()
        self.assertEqual(len(spans), 3)
        root = next(s for s in spans if s.kind == SpanKind.SERVER and s.resource.attributes["service.name"] == "orders")
        child = next(s for s in spans if s.kind == SpanKind.CLIENT)
        leaf = next(s for s in spans if s.resource.attributes["service.name"] == "inventory")
        self.assertIsNone(root.parent)
        self.assertEqual(child.parent.span_id, root.context.span_id)
        self.assertEqual(leaf.parent.span_id, child.context.span_id)
        self.assertEqual({s.context.trace_id for s in spans}, {root.context.trace_id})
        self.assertEqual(response.headers["X-Trace-ID"], f"{root.context.trace_id:032x}")
        self.assertTrue(all(s.attributes["request.id"] == "test-chain" for s in spans))

    async def test_timeout_marks_client_and_server_error(self):
        async def fail(request):
            raise httpx.ReadTimeout("simulated timeout", request=request)

        orders.state.inventory_client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.MockTransport(fail), base_url="http://inventory:8001"))
        response = await self.client.get("/orders/preview")
        self.assertEqual(response.status_code, 503)
        spans = self.exporter.get_finished_spans()
        self.assertEqual(len(spans), 2)
        self.assertTrue(all(s.status.status_code == StatusCode.ERROR for s in spans))

    async def test_health_excluded_and_remote_parent_preserved(self):
        response = await self.client.get("/health")
        self.assertNotIn("X-Trace-ID", response.headers)
        self.assertEqual(len(self.exporter.get_finished_spans()), 0)
        response = await self.client.get("/orders/preview", headers={
            "traceparent": "00-11111111111111111111111111111111-2222222222222222-01"})
        self.assertEqual(response.headers["X-Trace-ID"], "1" * 32)
        root = next(s for s in self.exporter.get_finished_spans()
                    if s.kind == SpanKind.SERVER and s.resource.attributes["service.name"] == "orders")
        self.assertEqual(root.parent.span_id, int("2" * 16, 16))

    async def test_connection_failure_records_actual_target_and_type(self):
        async def fail(request):
            raise httpx.ConnectError("simulated refused connection", request=request)

        orders.state.inventory_client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.MockTransport(fail), base_url="http://inventory:8002"))
        response = await self.client.get("/orders/preview")
        self.assertEqual(response.status_code, 503)
        spans = self.exporter.get_finished_spans()
        self.assertEqual(len(spans), 2)
        child = next(s for s in spans if s.kind == SpanKind.CLIENT)
        self.assertEqual(child.attributes["server.address"], "inventory")
        self.assertEqual(child.attributes["server.port"], 8002)
        self.assertEqual(child.attributes["error.type"], "ConnectError")
        self.assertEqual(child.status.status_code, StatusCode.ERROR)


if __name__ == "__main__":
    unittest.main()
