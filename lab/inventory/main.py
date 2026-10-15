"""最小库存服务：暂用固定库存，尚未接入数据库。"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel
from lab.observability import install_request_logging
from lab.tracing import tracing_lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    delay_ms = int(os.getenv("INVENTORY_DELAY_MS", "0"))
    if not 0 <= delay_ms <= 10000:
        raise ValueError("INVENTORY_DELAY_MS must be between 0 and 10000")
    app.state.delay_ms = delay_ms
    async with tracing_lifespan(app, "inventory"):
        yield


app = FastAPI(title="Inventory Service", lifespan=lifespan)
install_request_logging(app, "inventory")


@app.get("/health")
async def health():
    return {"status": "ok"}


class InventoryResponse(BaseModel):
    sku: str
    available: int


@app.get("/inventory/{sku}", response_model=InventoryResponse)
async def get_inventory(sku: str, request: Request) -> InventoryResponse:
    # 异步等待模拟下游变慢，不阻塞整个事件循环。控制信息不写入业务响应。
    await asyncio.sleep(request.app.state.delay_ms / 1000)
    # 本阶段所有商品都返回 10，重点验证服务间的 HTTP 调用。
    return InventoryResponse(sku=sku, available=10)
