"""订单预览：通过 HTTP 查询库存，不创建真实订单。"""

import os
from contextlib import asynccontextmanager
from time import perf_counter

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError
from lab.observability import emit, install_request_logging
from lab.tracing import get_inventory_with_trace, tracing_lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 一个进程复用一个客户端和连接池；服务关闭时自动释放连接。
    async with tracing_lifespan(app, "orders"), httpx.AsyncClient(
        base_url=os.getenv("INVENTORY_BASE_URL", "http://127.0.0.1:8001"),
        timeout=2.0,
        trust_env=False,
    ) as client:
        app.state.inventory_client = client
        yield


app = FastAPI(title="Orders Service", lifespan=lifespan)
install_request_logging(app, "orders")


@app.get("/health")
async def health():
    return {"status": "ok"}


class InventoryResponse(BaseModel):
    sku: str
    available: int = Field(ge=0, strict=True)


class OrderPreview(BaseModel):
    sku: str
    can_order: bool


@app.get("/orders/preview", response_model=OrderPreview)
async def preview_order(
    request: Request,
    sku: str = Query(default="demo", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
) -> OrderPreview:
    client: httpx.AsyncClient = request.app.state.inventory_client
    started = perf_counter()
    outcome = "error"
    try:
        response = await get_inventory_with_trace(
            client, request.app.state.tracer, sku, request.state.request_id,
        )
        outcome = "success"
    except httpx.TimeoutException as exc:
        outcome = "timeout"
        raise HTTPException(status_code=503, detail="Inventory service timed out") from exc
    except httpx.HTTPError as exc:
        # 查询失败不能当成库存为零，否则会给用户错误的业务结论。
        raise HTTPException(status_code=503, detail="Inventory service unavailable") from exc
    finally:
        emit("orders", "inventory_call", request_id=request.state.request_id,
             outcome=outcome, duration_ms=round((perf_counter() - started) * 1000, 3))

    try:
        inventory = InventoryResponse.model_validate(response.json())
        if inventory.sku != sku:
            raise ValueError("Inventory SKU mismatch")
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=502, detail="Invalid inventory response") from exc

    return OrderPreview(sku=sku, can_order=inventory.available > 0)
