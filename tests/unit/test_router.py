from __future__ import annotations

import asyncio
import json

import httpx

from codex_model_switcher.catalog import CatalogDocument
from codex_model_switcher.credentials import CredentialNotConfiguredError
from codex_model_switcher.models import ModelCapability, ModelRoute
from codex_model_switcher.router import Router, RouterRequest
from codex_model_switcher.routing import RouteTarget, default_deepseek_target
from codex_model_switcher.upstream import UpstreamClient


class MemoryCredentials:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def set(self, provider_id: str, secret: str) -> None:
        self.values[provider_id] = secret

    def get(self, provider_id: str) -> str:
        try:
            return self.values[provider_id]
        except KeyError as error:
            raise CredentialNotConfiguredError("fixture credential missing") from error

    def delete(self, provider_id: str) -> None:
        self.values.pop(provider_id, None)

    def exists(self, provider_id: str) -> bool:
        return provider_id in self.values


def route(model_id: str, *, lane: str, provider_id: str, upstream_model: str) -> ModelRoute:
    return ModelRoute(
        model_id=model_id,
        display_name=f"{model_id} {'Official' if lane == 'official' else 'API'}",
        lane=lane,
        provider_id=provider_id,
        upstream_model=upstream_model,
        capability=ModelCapability(4096, True, True, True, False, False, True),
    )


def make_router(handler):
    official = route(
        "official-model",
        lane="official",
        provider_id="openai",
        upstream_model="official",
    )
    third_party = route(
        "deepseek-model",
        lane="third_party",
        provider_id="deepseek",
        upstream_model="deepseek-v4-flash",
    )
    catalog = CatalogDocument("test-v1", "fixture", "fixture", (official, third_party))
    targets = {
        official.model_id: RouteTarget(
            official,
            "https://official.example.invalid/v1/responses",
            frozenset({"official.example.invalid"}),
            wire_api="responses",
        ),
        third_party.model_id: RouteTarget(
            third_party,
            "https://deepseek.example.invalid/v1/chat/completions",
            frozenset({"deepseek.example.invalid"}),
            wire_api="chat",
        ),
    }
    transport = RecordingTransport(handler)
    client = UpstreamClient(transport=transport)
    router = Router(
        catalog,
        targets=targets,
        credential_store=MemoryCredentials({"deepseek": "deepseek-fixture"}),
        upstream_client=client,
    )
    return router, transport, client


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.handler(request)


def test_unknown_model_returns_structured_404_response() -> None:
    async def run() -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "unused"})

        router, _transport, client = make_router(handler)
        response = await router.handle(
            RouterRequest(
                "not-in-catalog",
                {"input": "hello"},
                "task-fixture",
                "turn-fixture",
            )
        )
        assert response.status_code == 404
        assert response.json() == {
            "error": {
                "type": "unknown_model",
                "message": "model_id is not present in the loaded catalog",
                "model_id": "not-in-catalog",
            }
        }
        await client.aclose()

    asyncio.run(run())


def test_third_party_request_never_forwards_inbound_chatgpt_identity() -> None:
    async def run() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert (
                "authorization" not in request.headers
                or request.headers["authorization"] != "Bearer inbound-chatgpt"
            )
            assert "cookie" not in request.headers
            body = json.loads((await request.aread()).decode("utf-8"))
            assert body["thinking"] == {"type": "disabled"}
            return httpx.Response(
                200,
                json={"id": "third-party-r1", "choices": [{"message": {"content": "ok"}}]},
            )

        router, transport, client = make_router(handler)
        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": "hello"},
                "task-fixture",
                "turn-1",
                headers={
                    "Authorization": "Bearer inbound-chatgpt",
                    "Cookie": "session=chatgpt",
                    "Content-Type": "application/json",
                },
            )
        )
        assert response.status_code == 200
        assert transport.requests[0].headers["authorization"] == "Bearer deepseek-fixture"
        assert "cookie" not in transport.requests[0].headers
        await client.aclose()

    asyncio.run(run())


def test_default_deepseek_responses_request_preserves_unknown_fields_and_items() -> None:
    async def run() -> None:
        bodies: list[dict[str, object]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads((await request.aread()).decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "id": "deepseek-r1",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                },
            )

        deepseek = route(
            "deepseek-model",
            lane="third_party",
            provider_id="deepseek",
            upstream_model="deepseek-v4-flash",
        )
        target = default_deepseek_target(
            deepseek,
            allowed_hosts={"api.deepseek.com"},
        )
        transport = RecordingTransport(handler)
        client = UpstreamClient(transport=transport)
        router = Router(
            CatalogDocument("test-v1", "fixture", "fixture", (deepseek,)),
            targets={deepseek.model_id: target},
            credential_store=MemoryCredentials({"deepseek": "deepseek-fixture"}),
            upstream_client=client,
        )
        payload = {
            "input": [
                {"type": "input_text", "text": "hello"},
                {"type": "future_item", "opaque": {"keep": True}},
            ],
            "future_field": {"keep": [1, 2, 3]},
            "store": False,
        }

        response = await router.handle(
            RouterRequest(
                "deepseek-model",
                payload,
                "task-fixture",
                "turn-responses-preserve",
            )
        )

        assert response.status_code == 200
        assert bodies == [{**payload, "model": "deepseek-v4-flash"}]
        await client.aclose()

    asyncio.run(run())


def test_deepseek_explicit_reasoning_or_tools_are_rejected_before_upstream() -> None:
    async def run() -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("unsupported DeepSeek capability must not reach upstream")

        router, transport, client = make_router(handler)
        cases = (
            ("reasoning", {"input": "hello", "reasoning": {"effort": "medium"}}),
            ("thinking", {"input": "hello", "thinking": {"type": "enabled"}}),
            ("tools", {"input": "hello", "tools": [{"type": "function"}]}),
        )
        for suffix, payload in cases:
            response = await router.handle(
                RouterRequest(
                    "deepseek-model",
                    payload,
                    "task-fixture",
                    f"turn-explicit-{suffix}",
                )
            )
            assert response.status_code == 422
            assert response.json()["error"]["unsupported_item_types"] == [suffix]
        assert transport.requests == []
        await client.aclose()

    asyncio.run(run())


def test_missing_correlation_is_rejected_without_upstream_call() -> None:
    async def run() -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("upstream must not receive an uncorrelated request")

        router, transport, client = make_router(handler)
        response = await router.handle(
            RouterRequest("official-model", {"input": "hello"}, "", "turn-1")
        )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "missing_correlation"
        assert transport.requests == []
        await client.aclose()

    asyncio.run(run())


def test_active_turn_rejects_route_switch_until_previous_request_finishes() -> None:
    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return httpx.Response(200, json={"id": "official-r1", "output": []})

        router, _transport, client = make_router(handler)
        first_task = asyncio.create_task(
            router.handle(
                RouterRequest("official-model", {"input": "hello"}, "task-fixture", "turn-1")
            )
        )
        await started.wait()
        second = await router.handle(
            RouterRequest("deepseek-model", {"input": "hello"}, "task-fixture", "turn-1")
        )
        assert second.status_code == 409
        assert second.json()["error"]["type"] == "turn_in_progress"
        release.set()
        assert (await first_task).status_code == 200
        await client.aclose()

    asyncio.run(run())


def test_cross_route_does_not_reuse_previous_response_id() -> None:
    async def run() -> None:
        bodies: list[dict[str, object]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads((await request.aread()).decode("utf-8")))
            if len(bodies) == 1:
                return httpx.Response(200, json={"id": "official-r1", "output": []})
            return httpx.Response(
                200,
                json={"id": "third-r1", "choices": [{"message": {"content": "ok"}}]},
            )

        router, _transport, client = make_router(handler)
        first = await router.handle(
            RouterRequest("official-model", {"input": "one"}, "task-fixture", "turn-1")
        )
        assert first.status_code == 200
        second = await router.handle(
            RouterRequest(
                "deepseek-model",
                {"input": "two", "previous_response_id": "official-r1"},
                "task-fixture",
                "turn-2",
            )
        )
        assert second.status_code == 200
        assert "previous_response_id" not in bodies[1]
        await client.aclose()

    asyncio.run(run())
