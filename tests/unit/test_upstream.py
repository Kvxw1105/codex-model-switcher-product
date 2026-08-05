from __future__ import annotations

import asyncio

import httpx
import pytest

from codex_model_switcher.credentials import prepare_third_party_headers
from codex_model_switcher.routing import HostNotAllowedError, RedirectNotAllowedError
from codex_model_switcher.upstream import (
    RequestLimitError,
    SSEEvent,
    UpstreamClient,
)


class MemoryCredentials:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def set(self, provider_id: str, secret: str) -> None:
        self.values[provider_id] = secret

    def get(self, provider_id: str) -> str:
        return self.values[provider_id]

    def delete(self, provider_id: str) -> None:
        self.values.pop(provider_id, None)

    def exists(self, provider_id: str) -> bool:
        return provider_id in self.values


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], release: asyncio.Event | None = None) -> None:
        self.chunks = chunks
        self.release = release
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.release is not None:
            await self.release.wait()

    async def aclose(self) -> None:
        self.closed = True


class MemoryTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.handler(request)


def test_third_party_headers_remove_chatgpt_identity_and_inject_selected_credential() -> None:
    headers = prepare_third_party_headers(
        {
            "Authorization": "Bearer in",
            "Cookie": "session=chatgpt-fixture",
            "X-OpenAI-Account-Id": "account-fixture",
            "Content-Type": "application/json",
        },
        provider_id="deepseek",
        credential_store=MemoryCredentials({"deepseek": "deep"}),
    )

    assert headers == {
        "Content-Type": "application/json",
        "Authorization": "Bearer deep",
    }


def test_upstream_rejects_host_before_transport_is_called() -> None:
    async def run() -> None:
        transport = MemoryTransport(
            lambda _request: asyncio.sleep(0, result=httpx.Response(200, json={"ok": True}))
        )
        client = UpstreamClient(transport=transport)

        with pytest.raises(HostNotAllowedError):
            await client.request(
                "https://not-allowed.example.invalid/v1/responses",
                allowed_hosts={"allowed.example.invalid"},
                json_body={"model": "fixture"},
            )

        assert transport.requests == []
        await client.aclose()

    asyncio.run(run())


def test_redirect_is_disabled_by_default_and_allowed_redirect_stays_allowlisted() -> None:
    async def run() -> None:
        async def redirect(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                307,
                headers={"location": "https://other.example.invalid/next"},
            )

        transport = MemoryTransport(redirect)
        client = UpstreamClient(transport=transport)

        with pytest.raises(RedirectNotAllowedError):
            await client.request(
                "https://allowed.example.invalid/start",
                allowed_hosts={"allowed.example.invalid"},
                json_body={},
            )
        assert len(transport.requests) == 1
        await client.aclose()

    asyncio.run(run())


def test_body_and_timeout_limits_are_explicit_rejections() -> None:
    async def run() -> None:
        transport = MemoryTransport(
            lambda _request: asyncio.sleep(0, result=httpx.Response(200, json={"ok": True}))
        )
        client = UpstreamClient(transport=transport, max_body_bytes=4, max_timeout_seconds=2)

        with pytest.raises(RequestLimitError):
            await client.request(
                "https://allowed.example.invalid/start",
                allowed_hosts={"allowed.example.invalid"},
                json_body={"body": "too large"},
            )
        with pytest.raises(RequestLimitError):
            await client.request(
                "https://allowed.example.invalid/start",
                allowed_hosts={"allowed.example.invalid"},
                json_body={},
                timeout=3,
            )
        await client.aclose()

    asyncio.run(run())


def test_sse_events_are_yielded_before_the_stream_finishes() -> None:
    async def run() -> None:
        release = asyncio.Event()
        stream = ChunkStream(
            [b"id: first\nevent: message\ndata: {\"delta\":1}\n\n"], release
        )

        async def respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )

        client = UpstreamClient(transport=MemoryTransport(respond))
        events = client.stream_events(
            "https://allowed.example.invalid/stream",
            allowed_hosts={"allowed.example.invalid"},
            json_body={},
        )
        first = await asyncio.wait_for(anext(events), timeout=0.5)

        assert isinstance(first, SSEEvent)
        assert first.id == "first"
        assert first.data == '{"delta":1}'
        await events.aclose()
        assert stream.closed is True
        await client.aclose()

    asyncio.run(run())
