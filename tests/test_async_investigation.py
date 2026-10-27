import asyncio
import io
import json
import signal
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.investigation.agent import ModelFailure
from backend.investigation.async_investigation import InvestigationCancelled, investigate_window_async
from backend.investigation.async_model import AsyncWindowChatModel
from backend.investigation.evidence_models import WindowQuery
from backend.investigation.model import ModelConfig
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.window_cli import main
from backend.investigation.window_queries import WindowEvidenceTools
from test_window_investigation import finish, observed


class AsyncScripted:
    def __init__(self, *actions):
        self.actions = iter(actions)
        self.calls = 0
        self.entered = asyncio.Event()
        self.cleaned = False

    async def decide(self, context):
        self.calls += 1
        action = next(self.actions)
        if action == "wait":
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cleaned = True
        if isinstance(action, Exception):
            raise action
        return action


def freeze(store):
    tools = WindowEvidenceTools(store)
    with patch.object(tools, "_fetch_window", return_value={"data": [observed()]}):
        return tools.query_window(WindowQuery(start_us=100, end_us=200)).data["snapshot_id"]


def envelope(action, reason="stop"):
    return {"choices": [{"finish_reason": reason, "message": {"content": json.dumps(action)}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}


class AsyncInvestigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SnapshotStore(Path(temp.name))
        self.key = freeze(self.store)
        self.tool = {"kind": "tool", "tool": "get_snapshot_trace", "trace_id": "a" * 32}
        self.config = ModelConfig(base_url="https://example.test", model="test", api_key="test-secret")

    async def test_success_and_shared_action_validation(self):
        for action, expected in ((finish(self.key), "completed"),
                                 ({"kind": "unknown"}, "invalid_action"),
                                 (finish(self.key, refs=[]), "invalid_finding")):
            model = AsyncScripted(self.tool, action)
            result = await investigate_window_async(self.store, self.key, model)
            self.assertEqual(result.termination, expected)
            self.assertEqual(result.usage_totals["model_calls"], 2)
            self.assertEqual(result.usage_totals["drilldown_calls"], 1)

    async def test_deadline_interrupts_wait_and_preserves_evidence(self):
        model = AsyncScripted(self.tool, "wait")
        result = await asyncio.wait_for(investigate_window_async(
            self.store, self.key, model, timeout_seconds=0.05), 2)
        self.assertEqual(result.termination, "deadline_exceeded")
        self.assertTrue(model.cleaned)
        self.assertEqual(model.calls, 2)
        self.assertEqual(result.usage_totals["drilldown_calls"], 1)
        self.assertEqual(result.usage.calls[-1].status, "cancelled")
        self.assertIsNone(result.usage_totals["total_tokens"])

    async def test_external_cancel_propagates_with_partial_report(self):
        model = AsyncScripted(self.tool, "wait")
        task = asyncio.create_task(investigate_window_async(self.store, self.key, model))
        await asyncio.wait_for(model.entered.wait(), 2)
        task.cancel()
        with self.assertRaises(InvestigationCancelled) as caught:
            await task
        self.assertTrue(task.cancelled())
        self.assertTrue(model.cleaned)
        self.assertEqual(caught.exception.report.termination, "cancelled")
        self.assertEqual(caught.exception.report.usage_totals["drilldown_calls"], 1)

    async def test_network_timeout_is_not_total_deadline(self):
        model = AsyncScripted(ModelFailure("timeout"))
        result = await investigate_window_async(self.store, self.key, model)
        self.assertEqual(result.termination, "model_error")
        self.assertEqual(result.usage.calls[-1].error_code, "timeout")

    async def test_total_budget_is_shared_across_rounds(self):
        class SlowRounds:
            calls = 0
            async def decide(inner, context):
                inner.calls += 1
                await asyncio.sleep(0.08)
                return self.tool
        model = SlowRounds()
        result = await investigate_window_async(self.store, self.key, model, timeout_seconds=0.13)
        self.assertEqual(result.termination, "deadline_exceeded")
        self.assertEqual(model.calls, 2)
        self.assertEqual(result.usage_totals["drilldown_calls"], 1)

    async def test_cancel_one_investigation_does_not_affect_another(self):
        model = AsyncScripted("wait")
        task = asyncio.create_task(investigate_window_async(self.store, self.key, model))
        await asyncio.wait_for(model.entered.wait(), 2)
        other = AsyncScripted(self.tool, finish(self.key))
        task.cancel()
        with self.assertRaises(InvestigationCancelled):
            await task
        result = await investigate_window_async(self.store, self.key, other)
        self.assertEqual(result.termination, "completed")

    async def test_expired_snapshot_and_missing_snapshot_never_call_model(self):
        model = AsyncScripted()
        result = await investigate_window_async(self.store, self.key, model, timeout_seconds=1e-12)
        self.assertEqual(result.termination, "deadline_exceeded")
        result = await investigate_window_async(self.store, "b" * 64, model)
        self.assertEqual(result.termination, "source_error")
        self.assertEqual(model.calls, 0)

    async def test_tool_budget_still_enforced(self):
        model = AsyncScripted(self.tool)
        result = await investigate_window_async(self.store, self.key, model, max_tool_calls=1)
        self.assertEqual(result.termination, "budget_exhausted")
        self.assertEqual(result.usage_totals["drilldown_calls"], 0)

    async def test_adapter_contract_usage_reset_and_client_close(self):
        responses = iter([httpx.Response(200, json=envelope(finish(self.key, "insufficient_evidence", []))),
                          httpx.Response(200, json=envelope({}, "length")), httpx.Response(429)])
        def handle(request):
            payload = json.loads(request.content)
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertIn("get_snapshot_trace", payload["messages"][0]["content"])
            return next(responses)
        async with AsyncWindowChatModel(self.config, transport=httpx.MockTransport(handle)) as model:
            first = await investigate_window_async(self.store, self.key, model)
            self.assertEqual(first.termination, "completed")
            self.assertEqual(first.usage_totals["total_tokens"], 3)
            second = await investigate_window_async(self.store, self.key, model)
            self.assertEqual(second.usage.calls[-1].error_code, "output_truncated")
            self.assertEqual(second.usage_totals["total_tokens"], 3)
            third = await investigate_window_async(self.store, self.key, model)
            self.assertEqual(third.usage.calls[-1].error_code, "rate_limited")
            self.assertIsNone(third.usage_totals["total_tokens"])
        self.assertTrue(model._client.is_closed)

    async def test_cancel_closes_response_stream(self):
        class WaitingStream(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                yield b'{'
                await asyncio.Event().wait()
            async def aclose(self):
                self.closed = True
        stream = WaitingStream()
        async with AsyncWindowChatModel(self.config, transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=stream))) as model:
            result = await investigate_window_async(self.store, self.key, model, timeout_seconds=0.05)
        self.assertEqual(result.termination, "deadline_exceeded")
        self.assertTrue(stream.closed)
        self.assertTrue(model._client.is_closed)

    async def test_real_http_stall_disconnects_on_deadline(self):
        disconnected = asyncio.Event()
        handlers = set()
        async def handle(reader, writer):
            handlers.add(asyncio.current_task())
            try:
                header = await reader.readuntil(b'\r\n\r\n')
                length = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                              if line.lower().startswith(b'content-length:'))
                await reader.readexactly(length)
                # 已发响应头和部分正文，之后永久停止发送。
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n{')
                await writer.drain()
                await reader.read()
                disconnected.set()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(asyncio.current_task())
        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        try:
            port = server.sockets[0].getsockname()[1]
            config = ModelConfig(base_url=f'http://127.0.0.1:{port}', model='test', api_key='test-secret')
            async with AsyncWindowChatModel(config) as model:
                result = await asyncio.wait_for(investigate_window_async(
                    self.store, self.key, model, timeout_seconds=0.5), 3)
                await asyncio.wait_for(disconnected.wait(), 2)
            self.assertEqual(result.termination, 'deadline_exceeded')
            self.assertTrue(model._client.is_closed)
        finally:
            server.close()
            await server.wait_closed()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*list(handlers), return_exceptions=True)


class AsyncCliTests(unittest.TestCase):
    def test_cli_cancel_writes_report_and_closes_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory))
            key = freeze(store)
            output = Path(directory) / 'report.private.json'
            class CancelModel:
                closed = False
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    self.closed = True
                async def decide(self, context):
                    asyncio.current_task().cancel()
                    await asyncio.sleep(0)
            model = CancelModel()
            with patch('sys.argv', ['window', '--store', directory, 'investigate', key,
                                   '--strategy', 'agent', '--output', str(output)]), \
                    patch('backend.investigation.window_cli.ModelConfig.from_env'), \
                    patch('backend.investigation.window_cli.AsyncWindowChatModel', return_value=model), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 130)
            self.assertTrue(model.closed)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['termination'], 'cancelled')

    def test_sigint_is_saved_as_cancelled_report(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(Path(directory))
            key = freeze(store)
            output = Path(directory) / 'interrupt.private.json'
            class InterruptModel:
                closed = False
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    self.closed = True
                async def decide(self, context):
                    signal.raise_signal(signal.SIGINT)
                    await asyncio.sleep(0)
            model = InterruptModel()
            with patch('sys.argv', ['window', '--store', directory, 'investigate', key,
                                   '--strategy', 'agent', '--output', str(output)]), \
                    patch('backend.investigation.window_cli.ModelConfig.from_env'), \
                    patch('backend.investigation.window_cli.AsyncWindowChatModel', return_value=model), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 130)
            self.assertTrue(model.closed)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['termination'], 'cancelled')
