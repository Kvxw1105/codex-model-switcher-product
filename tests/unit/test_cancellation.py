from __future__ import annotations

import asyncio

import httpx
import pytest

from codex_model_switcher.catalog import CatalogDocument
from codex_model_switcher.models import ModelCapability, ModelRoute
from codex_model_switcher.router import Router, RouterRequest
from codex_model_switcher.routing import RouteTarget
from codex_model_switcher.upstream import UpstreamClient


class Credentials:
    def get(self, provider_id: str) -> str:
        return "fixture-secret"

    def set(self, provider_id: str, secret: str) -> None:
        pass

    def delete(self, provider_id: str) -> None:
        pass

    def exists(self, provider_id: str) -> bool:
        return True


class NeverEndingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        await asyncio.Event().wait()
        yield b""

    async def aclose(self) -> None:
        self.closed = True


class FirstThenWaitStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.first_sent = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.first_sent.set()
        yield (
            b'id: first\ndata: {"id":"chat-cancel","created":1700000003,'
            b'"model":"deepseek-v4-flash","choices":[{"index":0,'
            b'"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
        )
        await self.release.wait()

    async def aclose(self) -> None:
        self.closed = True


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, stream: NeverEndingStream) -> None:
        self.stream = stream
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, stream=self.stream)


def build_router(transport: Transport) -> tuple[Router, UpstreamClient]:
    route = ModelRoute(
        "deepseek-model",
        "DeepSeek API",
        "third_party",
        "deepseek",
        "deepseek-v4-flash",
        ModelCapability(4096, True, True, True, False, False, False),
    )
    target = RouteTarget(
        route,
        "https://deepseek.example.invalid/v1/chat/completions",
        frozenset({"deepseek.example.invalid"}),
        wire_api="chat",
    )
    client = UpstreamClient(transport=transport)
    return (
        Router(
            CatalogDocument("test-v1", "fixture", "fixture", (route,)),
            targets={route.model_id: target},
            credential_store=Credentials(),
            upstream_client=client,
        ),
        client,
    )


def test_cancel_before_stream_iteration_prevents_upstream_and_releases_turn() -> None:
    async def run() -> None:
        stream = NeverEndingStream()
        transport = Transport(stream)
        router, client = build_router(transport)
        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": "hello"},
                "task-fixture",
                "turn-cancel",
                api="chat",
                stream=True,
            )
        )
        assert response.cancel_handle_id is not None
        assert await router.cancel(response.cancel_handle_id) is True
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(anext(response.aiter_events()), timeout=0.5)
        assert transport.requests == []
        await router.aclose()
        await client.aclose()

    asyncio.run(run())


def test_cancel_active_stream_closes_upstream_response_before_returning() -> None:
    async def run() -> None:
        stream = FirstThenWaitStream()

        class ActiveTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, stream=stream)

        client = UpstreamClient(transport=ActiveTransport())
        route = ModelRoute(
            "deepseek-model",
            "DeepSeek API",
            "third_party",
            "deepseek",
            "deepseek-v4-flash",
            ModelCapability(4096, True, True, True, False, False, False),
        )
        router = Router(
            CatalogDocument("test-v1", "fixture", "fixture", (route,)),
            targets={
                route.model_id: RouteTarget(
                    route,
                    "https://deepseek.example.invalid/v1/chat/completions",
                    frozenset({"deepseek.example.invalid"}),
                    wire_api="chat",
                )
            },
            credential_store=Credentials(),
            upstream_client=client,
        )
        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": "hello"},
                "task-fixture",
                "turn-active-cancel",
                api="responses",
                stream=True,
            )
        )
        first_received = asyncio.Event()

        async def consume() -> None:
            async for event in response.aiter_events():
                assert event.event == "response.created"
                first_received.set()
                await asyncio.Event().wait()

        consumer = asyncio.create_task(consume())
        await first_received.wait()
        assert await router.cancel(response.cancel_handle_id or "") is True
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert stream.closed is True
        await router.aclose()
        await client.aclose()

    asyncio.run(run())
