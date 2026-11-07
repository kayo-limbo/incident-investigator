"""Process-local request counters; diagnostic observations, never injection settings."""

from time import monotonic_ns, time_ns
from uuid import uuid4


def initialize(app, max_connections, pool_timeout_ms):
    app.state.runtime_observation = dict(
        process_id=uuid4().hex, started_us=time_ns() // 1000,
        max_connections=max_connections, pool_timeout_ms=pool_timeout_ms,
        requests_started=0, requests_finished=0, in_flight=0, peak_in_flight=0,
    )


def install(app):
    @app.middleware("http")
    async def observe(request, call_next):
        counters = getattr(request.app.state, "runtime_observation", None)
        if request.url.path != "/orders/preview" or counters is None:
            return await call_next(request)
        counters["requests_started"] += 1
        counters["in_flight"] += 1
        counters["peak_in_flight"] = max(counters["peak_in_flight"], counters["in_flight"])
        try:
            return await call_next(request)
        finally:
            counters["in_flight"] -= 1
            counters["requests_finished"] += 1

    @app.get("/observations/client")
    async def observation():
        return {**app.state.runtime_observation, "observed_us": time_ns() // 1000}

    @app.get("/observations/client/timed")
    async def timed_observation():
        # Separate endpoint preserves the strict legacy observation contract.
        return {**app.state.runtime_observation, "observed_us": time_ns() // 1000,
                "monotonic_us": monotonic_ns() // 1000}
