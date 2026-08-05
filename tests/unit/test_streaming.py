from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from codex_model_switcher.catalog import CatalogDocument
from codex_model_switcher.models import ModelCapability, ModelRoute
from codex_model_switcher.router import Router, RouterRequest
from codex_model_switcher.routing import RouteTarget, default_deepseek_target
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


class ScriptedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class Transport(httpx.AsyncBaseTransport):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self.stream = stream
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=self.stream,
        )


def build_router(
    transport: Transport,
    *,
    wire_api: str = "chat",
) -> tuple[Router, UpstreamClient]:
    route = ModelRoute(
        "deepseek-model",
        "DeepSeek API",
        "third_party",
        "deepseek",
        "deepseek-v4-flash",
        ModelCapability(4096, True, True, True, False, False, False),
    )
    if wire_api == "responses":
        target = default_deepseek_target(route, allowed_hosts={"api.deepseek.com"})
    else:
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


def test_responses_facing_deepseek_text_stream_emits_real_responses_events() -> None:
    async def run() -> None:
        chunks = [
            (
                b'id: sse-1\ndata: '
                b'{"id":"chat-123","created":1700000000,"model":"deepseek-v4-flash",'
                b'"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"},'
                b'"finish_reason":null}]}\n\n'
            ),
            (
                b'id: sse-2\ndata: '
                b'{"id":"chat-123","created":1700000000,"model":"deepseek-v4-flash",'
                b'"choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
            ),
            (
                b'id: sse-3\ndata: '
                b'{"id":"chat-123","created":1700000000,"model":"deepseek-v4-flash",'
                b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            ),
            b'id: sse-4\ndata: [DONE]\n\n',
        ]
        stream = ScriptedStream(chunks)
        transport = Transport(stream)
        router, client = build_router(transport)
        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": "hello"},
                "task-fixture",
                "turn-text-stream",
                api="responses",
                stream=True,
            )
        )
        events = [event async for event in response.aiter_events()]
        payloads = [json.loads(event.data) for event in events]

        assert response.status_code == 200
        assert [payload["type"] for payload in payloads] == [
            "response.created",
            "response.output_text.delta",
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        ]
        assert [event.id for event in events] == [
            "sse-1",
            "sse-1",
            "sse-2",
            "sse-3",
            "sse-3",
            "sse-3",
            "sse-3",
        ]
        assert [payload["sequence_number"] for payload in payloads] == list(range(7))
        assert payloads[0]["response"]["id"] == "chat-123"
        assert payloads[0]["response"]["model"] == "deepseek-v4-flash"
        assert payloads[0]["response"]["created_at"] == 1700000000
        assert payloads[1]["delta"] == "Hel"
        assert payloads[2]["delta"] == "lo"
        assert payloads[3]["text"] == "Hello"
        assert payloads[4]["part"]["annotations"] == []
        assert payloads[-1]["response"]["output"][0]["content"][0]["annotations"] == []
        assert payloads[-1]["response"]["id"] == "chat-123"
        assert payloads[-1]["response"]["model"] == "deepseek-v4-flash"
        assert all("usage" not in payload for payload in payloads)
        assert json.loads(transport.requests[0].content.decode("utf-8"))["thinking"] == {
            "type": "disabled"
        }
        assert stream.closed is True
        await router.aclose()
        await client.aclose()

    asyncio.run(run())


def test_default_deepseek_responses_sse_is_passed_through_without_translation() -> None:
    async def run() -> None:
        chunks = [
            (
                b'event: response.created\ndata: '
                b'{"type":"response.created","response":{"id":"resp-1",'
                b'"status":"in_progress","future_field":{"keep":true}}}\n\n'
            ),
            (
                b'event: response.completed\ndata: '
                b'{"type":"response.completed","response":{"id":"resp-1",'
                b'"status":"completed","output":[]}}\n\n'
            ),
        ]
        stream = ScriptedStream(chunks)
        transport = Transport(stream)
        router, client = build_router(transport, wire_api="responses")
        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": [{"type": "future_item", "keep": True}]},
                "task-fixture",
                "turn-responses-pass-through",
                stream=True,
            )
        )

        events = [event async for event in response.aiter_events()]

        assert [event.event for event in events] == [
            "response.created",
            "response.completed",
        ]
        assert [event.data for event in events] == [
            '{"type":"response.created","response":{"id":"resp-1","status":"in_progress","future_field":{"keep":true}}}',
            '{"type":"response.completed","response":{"id":"resp-1","status":"completed","output":[]}}',
        ]
        assert events[0].raw == chunks[0].decode("utf-8")
        assert transport.requests[0].url.path == "/responses"
        assert stream.closed is True
        await router.aclose()
        await client.aclose()

    asyncio.run(run())


def test_default_deepseek_responses_sse_cancellation_closes_upstream() -> None:
    async def run() -> None:
        release = asyncio.Event()
        stream = EventStream(release)
        transport = Transport(stream)
        router, client = build_router(transport, wire_api="responses")
        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": "hello"},
                "task-fixture",
                "turn-responses-cancel",
                stream=True,
            )
        )

        event = await asyncio.wait_for(anext(response.aiter_events()), timeout=0.5)

        assert event.event == "message"
        assert transport.requests[0].url.path == "/responses"
        await response.events.aclose()  # type: ignore[union-attr]
        assert stream.closed is True
        await router.aclose()
        await client.aclose()

    asyncio.run(run())


def test_responses_facing_deepseek_stream_copies_actual_usage_only() -> None:
    async def run() -> None:
        usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
        chunks = [
            (
                b'data: {"id":"chat-usage","created":1700000001,"model":"deepseek-v4-flash",'
                b'"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
            ),
            (
                b'data: {"id":"chat-usage","created":1700000001,"model":"deepseek-v4-flash",'
                b'"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}\n\n'
            ),
            b'data: [DONE]\n\n',
        ]
        stream = ScriptedStream(chunks)
        router, client = build_router(Transport(stream))
        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": "hello"},
                "task-fixture",
                "turn-usage-stream",
                api="responses",
                stream=True,
            )
        )
        events = [event async for event in response.aiter_events()]
        completed = json.loads(events[-1].data)

        assert completed["type"] == "response.completed"
        assert completed["response"]["usage"] == usage
        await router.aclose()
        await client.aclose()

    asyncio.run(run())


def test_responses_stream_warns_when_chat_omits_finish_reason_and_usage() -> None:
    async def run() -> None:
        stream = ScriptedStream(
            [
                (
                    b'data: {"id":"chat-warning","created":1700000004,'
                    b'"model":"deepseek-v4-flash","choices":[{"index":0,'
                    b'"delta":{"content":"ok"}}]}\n\n'
                ),
                b"data: [DONE]\n\n",
            ]
        )
        router, client = build_router(Transport(stream))
        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": "hello"},
                "task-fixture",
                "turn-warning-stream",
                api="responses",
                stream=True,
            )
        )
        events = [event async for event in response.aiter_events()]
        completed = json.loads(events[-1].data)

        assert completed["type"] == "response.completed"
        assert completed["response"]["compatibility_warnings"] == [
            "missing_finish_reason",
            "missing_usage",
        ]
        assert "usage" not in completed["response"]
        await router.aclose()
        await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("delta", "unsupported_type"),
    [
        ({"reasoning_content": "private reasoning"}, "reasoning_content"),
        ({"tool_calls": [{"id": "call-fixture"}]}, "tool_calls"),
        ({"content": [{"type": "image_url"}]}, "non_text_content"),
        ({"unknown_chunk_field": "fixture"}, "unknown_chunk"),
    ],
)
def test_unsupported_chat_stream_chunks_emit_error_and_close(
    delta: dict[str, object],
    unsupported_type: str,
) -> None:
    async def run() -> None:
        chunks = [
            (
                b"data: "
                + json.dumps(
                    {
                        "id": "chat-error",
                        "created": 1700000002,
                        "model": "deepseek-v4-flash",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": None}
                        ],
                    }
                )
                .encode("utf-8")
                + b"\n\n"
            )
        ]
        stream = ScriptedStream(chunks)
        router, client = build_router(Transport(stream))
        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": "hello"},
                "task-fixture",
                f"turn-error-{unsupported_type}",
                api="responses",
                stream=True,
            )
        )
        events = [event async for event in response.aiter_events()]
        payloads = [json.loads(event.data) for event in events]

        assert payloads[-1]["type"] == "error"
        assert payloads[-1]["code"] == "unsupported_upstream_chunk"
        assert payloads[-1]["param"] == unsupported_type
        assert payloads[-1]["sequence_number"] == 0
        assert all(payload["type"] != "response.completed" for payload in payloads)
        assert stream.closed is True
        await router.aclose()
        await client.aclose()

    asyncio.run(run())
