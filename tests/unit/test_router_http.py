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


class FailingRouter(FakeRouter):
    async def handle(self, request: RouterRequest) -> RouterResponse:
        self.requests.append(request)
        raise RuntimeError("router event loop is closed")


def test_router_http_unknown_endpoint_returns_not_found(service) -> None:
    instance, router = service

    status, body, _ = _post(
        instance,
        "/v1/unknown",
        {"model": "cms-deepseek-v4-flash"},
        {"X-Codex-Task-Id": "task", "X-Codex-Turn-Id": "turn"},
    )

    assert status == 404
    assert json.loads(body)["error"]["type"] == "not_found"
    assert router.requests == []


def test_router_http_rejects_invalid_json(service) -> None:
    instance, router = service

    connection = http.client.HTTPConnection(*instance.address, timeout=2)
    connection.request(
        "POST",
        "/v1/responses",
        body=b"{not-json",
        headers={
            "Content-Type": "application/json",
            "X-Codex-Task-Id": "task",
            "X-Codex-Turn-Id": "turn",
        },
    )
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    connection.close()

    assert response.status == 400
    assert json.loads(body)["error"]["type"] == "invalid_json"
    assert router.requests == []


def test_router_http_rejects_missing_model_field(service) -> None:
    instance, router = service

    status, body, _ = _post(
        instance,
        "/v1/responses",
        {"input": "hello"},
        {"X-Codex-Task-Id": "task", "X-Codex-Turn-Id": "turn"},
    )

    assert status == 400
    assert json.loads(body)["error"]["type"] == "invalid_request"
    assert router.requests == []


def test_router_http_non_stream_failure_returns_503_instead_of_router_error() -> None:
    from codex_model_switcher.router_http import start_router_http

    router = FailingRouter()
    instance = start_router_http(router, port=0)
    try:
        status, body, _ = _post(
            instance,
            "/v1/responses",
            {"model": "cms-deepseek-v4-flash", "input": "hello"},
            {
                "X-Codex-Task-Id": "task-fail",
                "X-Codex-Turn-Id": "turn-fail",
            },
        )
    finally:
        instance.stop()

    assert status == 503
    assert json.loads(body)["error"]["type"] == "router_not_running"


def test_router_http_stopped_service_submit_raises() -> None:
    from codex_model_switcher.router_http import RouterHttpService

    router = FakeRouter()
    instance = RouterHttpService(router, port=0)
    instance._stopped = True

    async def noop() -> None:
        return None

    operation = noop()
    with pytest.raises(RuntimeError, match="stopped"):
        instance.submit(operation)
    operation.close()  # stopped-service check runs before scheduling; consume the coroutine


def test_router_http_stream_failure_returns_503_instead_of_silent_hang() -> None:
    from codex_model_switcher.router_http import start_router_http

    router = FailingRouter()
    instance = start_router_http(router, port=0)
    try:
        status, body, _ = _post(
            instance,
            "/v1/responses",
            {"model": "cms-deepseek-v4-flash", "input": "hello", "stream": True},
            {
                "X-Codex-Task-Id": "task-fail",
                "X-Codex-Turn-Id": "turn-fail",
            },
        )
    finally:
        instance.stop()

    assert status == 503
    assert json.loads(body)["error"]["type"] == "router_not_running"
