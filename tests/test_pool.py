import asyncio
import os
import unittest
from unittest.mock import patch

import httpx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lab.client_observation import ObservedTransport, client_settings
from lab.tracing import get_inventory_with_trace


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.received = 0
        self.tasks = set()

        async def serve(reader, writer):
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                await reader.readuntil(b"\r\n\r\n")
                self.received += 1
                if self.received == 1:
                    self.entered.set()
                    await self.release.wait()
                body = b'{"sku":"demo","available":10}'
                writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                self.tasks.discard(task)

        self.server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("test")
        self.transport = ObservedTransport(1, 50)
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}",
            transport=self.transport, timeout=httpx.Timeout(2, pool=.05), trust_env=False)

    async def asyncTearDown(self):
        self.release.set()
        await self.client.aclose()
        self.server.close()
        await self.server.wait_closed()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.provider.shutdown()

    async def call(self, name):
        return await get_inventory_with_trace(self.client, self.tracer, "demo", name)

    async def test_real_pool_timeout_never_sends_request_and_recovers(self):
        first = asyncio.create_task(self.call("first"))
        await asyncio.wait_for(self.entered.wait(), 2)
        try:
            with self.assertRaises(httpx.PoolTimeout):
                await self.call("waiting")
            self.assertEqual(self.received, 1)
        finally:
            self.release.set()
            await first
        self.assertEqual((await self.call("recovered")).status_code, 200)
        failed = next(s for s in self.exporter.get_finished_spans()
                      if s.attributes["request.id"] == "waiting")
        self.assertEqual(failed.attributes["error.type"], "PoolTimeout")
        self.assertFalse(failed.attributes["client.request_headers_started"])
        self.assertNotIn("client.pre_send_ms", failed.attributes)
        self.assertEqual(failed.attributes["client.in_flight_at_start"], 2)
        self.assertEqual(self.transport.in_flight, 0)

    async def test_successful_queue_wait_is_observed_before_send(self):
        self.client.timeout = httpx.Timeout(2, pool=1)
        first = asyncio.create_task(self.call("first"))
        await asyncio.wait_for(self.entered.wait(), 2)
        second = asyncio.create_task(self.call("waiting"))
        await asyncio.sleep(.06)
        self.release.set()
        await asyncio.gather(first, second)
        waiting = next(s for s in self.exporter.get_finished_spans()
                       if s.attributes["request.id"] == "waiting")
        self.assertGreater(waiting.attributes["client.pre_send_ms"], 30)
        self.assertTrue(waiting.attributes["client.request_headers_started"])
        self.assertEqual(self.received, 2)

    async def test_cancelling_waiter_releases_bookkeeping(self):
        self.client.timeout = httpx.Timeout(2, pool=1)
        first = asyncio.create_task(self.call("first"))
        await asyncio.wait_for(self.entered.wait(), 2)
        waiter = asyncio.create_task(self.call("cancelled"))
        await asyncio.sleep(.02)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(self.transport.in_flight, 1)
        self.release.set()
        await first
        self.assertEqual((await self.call("after_cancel")).status_code, 200)
        self.assertEqual(self.transport.in_flight, 0)
        self.assertEqual(self.received, 2)


class SettingsTests(unittest.TestCase):
    def test_invalid_pool_configuration_fails_startup(self):
        for values in ({"INVENTORY_MAX_CONNECTIONS": "0"},
                       {"INVENTORY_MAX_CONNECTIONS": "bad"},
                       {"INVENTORY_POOL_TIMEOUT_MS": "-1"}):
            with patch.dict(os.environ, values, clear=True), self.assertRaises(ValueError):
                client_settings()
