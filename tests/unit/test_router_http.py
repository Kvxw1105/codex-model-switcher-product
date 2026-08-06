from __future__ import annotations

import http.client
import json

import pytest

from codex_model_switcher.router import RouterRequest, RouterResponse
from codex_model_switcher.upstream import SSEEvent


class FakeRouter:
    def __init__(self) -> None:
        self.requests: list[RouterRequest] = []
        self.closed = False

    async def handle(self, request: RouterRequest) -> RouterResponse:
        self.requests.append(request)
        return RouterResponse(
            200,
            {
                "id": "fixture-response",
                "object": "response",
                "status": "completed",
                "output": [],
            },
        )

    async def aclose(self) -> None:
        self.closed = True


class StreamingRouter(FakeRouter):
    async def handle(self, request: RouterRequest) -> RouterResponse:
        self.requests.append(request)

        async def events():
            yield SSEEvent("response.created", '{"id":"fixture-stream"}')
            yield SSEEvent("response.completed", '{"id":"fixture-stream"}')

        return RouterResponse(200, events=events())


@pytest.fixture
def service():
    from codex_model_switcher.router_http import start_router_http

    router = FakeRouter()
    instance = start_router_http(router, port=0)
    yield instance, router
    instance.stop()


def _post(service, path: str, payload: object, headers: dict[str, str] | None = None):
    connection = http.client.HTTPConnection(*service.address, timeout=2)
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    connection.request(
        "POST",
        path,
        body=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
    )
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    content_type = response.getheader("Content-Type", "")
    connection.close()
    return response.status, body, content_type


def test_router_http_forwards_correlated_responses_request(service) -> None:
    instance, router = service

    status, body, content_type = _post(
        instance,
        "/v1/responses",
        {"model": "cms-deepseek-v4-flash", "input": "hello", "stream": False},
        {
            "X-Codex-Task-Id": "task-fixture",
            "X-Codex-Turn-Id": "turn-fixture",
        },
    )

    assert status == 200
    assert "application/json" in content_type
    assert json.loads(body)["id"] == "fixture-response"
    assert router.requests[0].api == "responses"
    assert router.requests[0].codex_task_id == "task-fixture"
    assert router.requests[0].turn_id == "turn-fixture"


def test_router_http_rejects_missing_correlation_before_router_call(service) -> None:
    instance, router = service

    status, body, _ = _post(
        instance,
        "/v1/responses",
        {"model": "cms-deepseek-v4-flash", "input": "hello"},
    )

    assert status == 400
    assert json.loads(body)["error"]["type"] == "missing_correlation"
    assert router.requests == []


def test_router_http_rejects_non_loopback_bind() -> None:
    from codex_model_switcher.router_http import start_router_http

    with pytest.raises(ValueError, match="127.0.0.1"):
        start_router_http(FakeRouter(), host="0.0.0.0", port=0)


def test_router_http_forwards_sse_events_before_stream_finishes() -> None:
    from codex_model_switcher.router_http import start_router_http

    router = StreamingRouter()
    instance = start_router_http(router, port=0)
    try:
        status, body, content_type = _post(
            instance,
            "/v1/responses",
            {"model": "cms-deepseek-v4-flash", "input": "hello", "stream": True},
            {
                "X-Codex-Task-Id": "task-stream",
                "X-Codex-Turn-Id": "turn-stream",
            },
        )
    finally:
        instance.stop()

    assert status == 200
    assert content_type.startswith("text/event-stream")
    assert "event: response.created" in body
    assert "event: response.completed" in body


def test_router_http_stop_closes_router() -> None:
    from codex_model_switcher.router_http import start_router_http

    router = FakeRouter()
    instance = start_router_http(router, port=0)
    instance.stop()

    assert router.closed is True
    assert instance.running is False
