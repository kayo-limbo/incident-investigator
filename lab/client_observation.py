"""HTTPX 公开 transport/trace 扩展观测；不修改 httpcore 的连接池实现。"""

import os
from time import perf_counter

import httpx
from opentelemetry import trace


def client_settings() -> tuple[int, int]:
    connections = int(os.getenv("INVENTORY_MAX_CONNECTIONS", "100"))
    pool_timeout_ms = int(os.getenv("INVENTORY_POOL_TIMEOUT_MS", "2000"))
    if not 1 <= connections <= 1000:
        raise ValueError("INVENTORY_MAX_CONNECTIONS must be between 1 and 1000")
    if not 1 <= pool_timeout_ms <= 10000:
        raise ValueError("INVENTORY_POOL_TIMEOUT_MS must be between 1 and 10000")
    return connections, pool_timeout_ms


class ObservedTransport(httpx.AsyncHTTPTransport):
    """仅用于单事件循环、HTTP/1.1 的订单客户端。

    pre_send 包括排队、建连及调度，不等于 pool wait。
    in_flight 包括等待与执行中的 transport 请求，不是已占用连接数。
    """

    def __init__(self, max_connections: int, pool_timeout_ms: int):
        super().__init__(limits=httpx.Limits(max_connections=max_connections,
                                            max_keepalive_connections=max_connections),
                         http2=False, retries=0)
        self.max_connections = max_connections
        self.pool_timeout_ms = pool_timeout_ms
        self.in_flight = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        span = trace.get_current_span()
        started = perf_counter()
        connect_started = None
        connect_ms = 0.0
        headers_started = False
        previous_trace = request.extensions.get("trace")
        self.in_flight += 1
        span.set_attribute("client.max_connections", self.max_connections)
        span.set_attribute("client.pool_timeout_ms", self.pool_timeout_ms)
        span.set_attribute("client.in_flight_at_start", self.in_flight)
        span.set_attribute("client.request_headers_started", False)

        async def observe(event, info):
            nonlocal connect_started, connect_ms, headers_started
            now = perf_counter()
            if event == "connection.connect_tcp.started":
                connect_started = now
            elif event in ("connection.connect_tcp.complete", "connection.connect_tcp.failed"):
                if connect_started is not None:
                    connect_ms += (now - connect_started) * 1000
                    span.set_attribute("client.connect_ms", connect_ms)
                    connect_started = None
            elif event == "http11.send_request_headers.started" and not headers_started:
                headers_started = True
                span.set_attribute("client.request_headers_started", True)
                span.set_attribute("client.pre_send_ms", (now - started) * 1000)
            if previous_trace is not None:
                await previous_trace(event, info)

        request.extensions["trace"] = observe
        try:
            return await super().handle_async_request(request)
        finally:
            # transport 返回响应头或抛出异常时结束；不含响应体消费。
            span.set_attribute("client.transport_ms", (perf_counter() - started) * 1000)
            self.in_flight -= 1
