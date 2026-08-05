from __future__ import annotations

import asyncio

import httpx

from codex_model_switcher.catalog import CatalogDocument
from codex_model_switcher.models import ModelCapability, ModelRoute
from codex_model_switcher.router import Router, RouterRequest
from codex_model_switcher.routing import RouteTarget
from codex_model_switcher.upstream import UpstreamClient


class Credentials:
    def get(self, provider_id: str) -> str:
        return "deepseek-fixture"

    def set(self, provider_id: str, secret: str) -> None:
        pass

    def delete(self, provider_id: str) -> None:
        pass

    def exists(self, provider_id: str) -> bool:
        return True


class EventStream(httpx.AsyncByteStream):
    def __init__(self, release: asyncio.Event) -> None:
        self.release = release
        self.closed = False

    async def __aiter__(self):
        yield (
            b'id: event-1\nevent: message\ndata: '
            b'{"choices":[{"delta":{"content":"hi"}}]}\n\n'
        )
        await self.release.wait()

    async def aclose(self) -> None:
        self.closed = True


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, stream: EventStream) -> None:
        self.stream = stream
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=self.stream,
        )


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


def test_stream_keeps_upstream_event_order_and_id_before_upstream_finishes() -> None:
    async def run() -> None:
        release = asyncio.Event()
        stream = EventStream(release)
        transport = Transport(stream)
        router, client = build_router(transport)
        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": "hello", "stream": True},
                "task-fixture",
                "turn-stream",
                api="chat",
                stream=True,
            )
        )
        event = await asyncio.wait_for(anext(response.aiter_events()), timeout=0.5)
        assert event.id == "event-1"
        assert event.event == "message"
        assert event.data.startswith('{"choices"')
        await response.events.aclose()  # type: ignore[union-attr]
        assert stream.closed is True
        await router.aclose()
        await client.aclose()

    asyncio.run(run())


def test_responses_facing_deepseek_stream_is_explicitly_rejected_without_guessing_events() -> None:
    async def run() -> None:
        release = asyncio.Event()
        stream = EventStream(release)
        transport = Transport(stream)
        router, client = build_router(transport)
        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": "hello"},
                "task-fixture",
                "turn-unsupported-stream",
                api="responses",
                stream=True,
            )
        )
        assert response.status_code == 422
        assert response.json()["error"]["unsupported_item_types"] == [
            "streaming_responses_to_chat"
        ]
        assert transport.requests == []
        await router.aclose()
        await client.aclose()

    asyncio.run(run())
