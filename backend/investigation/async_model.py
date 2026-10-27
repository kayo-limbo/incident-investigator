"""两种窗口模型共享异步传输；一个实例由一次调查独占。"""

import httpx

from .agent import ModelFailure
from .usage import token_usage
from .window_model import WindowChatModel
from .model import ChatCompletionsModel
from .hypothesis_model import HypothesisChatModel


class AsyncChatCompletionsModel(ChatCompletionsModel):
    def __init__(self, config, *, transport=None):
        self.config = config
        self.last_usage = token_usage(None)
        self._client = httpx.AsyncClient(timeout=config.timeout_seconds, trust_env=False,
                                        follow_redirects=False, transport=transport)

    def __enter__(self):
        raise TypeError("异步模型须使用 async with")

    def close(self):
        raise TypeError("异步模型须 await aclose()")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    async def decide(self, context):
        self.last_usage = token_usage(None)
        payload = self._payload(context)
        try:
            async with self._client.stream(
                "POST", self.config.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + self.config.api_key}, json=payload,
            ) as response:
                if response.status_code in (401, 403):
                    raise ModelFailure("authentication_failed")
                if response.status_code == 429:
                    raise ModelFailure("rate_limited")
                if response.status_code != 200:
                    raise ModelFailure("http_error")
                size, chunks = 0, []
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.MAX_RESPONSE_BYTES:
                        raise ModelFailure("response_too_large")
                    chunks.append(chunk)
            return self._decode(b"".join(chunks))
        except httpx.TimeoutException:
            raise ModelFailure("timeout") from None
        except httpx.HTTPError:
            raise ModelFailure("transport_error") from None
        # CancelledError 保持传播；async with 负责退出响应流。


class AsyncWindowChatModel(AsyncChatCompletionsModel):
    system_prompt = WindowChatModel.system_prompt
    action_schema = WindowChatModel.action_schema


class AsyncHypothesisChatModel(AsyncChatCompletionsModel):
    system_prompt = HypothesisChatModel.system_prompt
    action_schema = HypothesisChatModel.action_schema
