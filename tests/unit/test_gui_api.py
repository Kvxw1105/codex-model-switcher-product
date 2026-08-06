from __future__ import annotations

import http.client
import json
import threading

import pytest

from codex_model_switcher.models import ModelCapability, ModelRoute
from codex_model_switcher.web import (
    CSRF_HEADER,
    ControlCenterState,
    create_control_center_server,
    run_control_center,
)


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, provider_id: str, secret: str) -> None:
        self.values[provider_id] = secret

    def get(self, provider_id: str) -> str:
        return self.values[provider_id]

    def delete(self, provider_id: str) -> None:
        self.values.pop(provider_id, None)

    def exists(self, provider_id: str) -> bool:
        return provider_id in self.values


def _route(model_id: str, provider_id: str, lane: str) -> ModelRoute:
    return ModelRoute(
        model_id=model_id,
        display_name=f"{provider_id} {'Official' if lane == 'official' else 'API'}",
        lane=lane,  # type: ignore[arg-type]
        provider_id=provider_id,
        upstream_model="fixture-model",
        capability=ModelCapability(
            context_window=32_000,
            supports_responses=True,
            supports_streaming=True,
            supports_tools=True,
            supports_images=False,
            supports_files=False,
            supports_compaction_context=True,
        ),
    )


@pytest.fixture
def state() -> ControlCenterState:
    return ControlCenterState(
        providers=[
            {
                "id": "deepseek",
                "name": "DeepSeek API",
                "base_url": "https://api.example.invalid/v1",
                "protocol": "responses",
                "model": "fixture-model",
                "lane": "third_party",
                "capabilities": {"context_window": 32_000, "supports_responses": True},
            },
            {
                "id": "official",
                "name": "Codex Official",
                "base_url": "https://official.example.invalid/v1",
                "protocol": "responses",
                "model": "fixture-official",
                "lane": "official",
            },
        ],
        models=[
            _route("fixture-deepseek", "deepseek", "third_party"),
            _route("fixture-official", "official", "official"),
        ],
        credential_store=MemoryCredentialStore(),
        official_identity_available=True,
    )


@pytest.fixture
def server(state: ControlCenterState):
    instance = create_control_center_server(state=state)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=2)


def _request(
    server,
    method: str,
    path: str,
    payload: object | None = None,
    *,
    csrf: str | None = None,
) -> tuple[int, object, str]:
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if csrf is not None:
        headers[CSRF_HEADER] = csrf
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    content_type = response.getheader("Content-Type", "")
    connection.close()
    if "json" in content_type:
        return response.status, json.loads(raw), raw
    return response.status, raw, raw


def test_control_center_state_has_random_csrf_token() -> None:
    first = ControlCenterState()
    second = ControlCenterState()

    assert first.csrf_token
    assert first.csrf_token != second.csrf_token


def test_provider_get_never_returns_secret(server, state: ControlCenterState) -> None:
    secret = "fixture-secret-never-return"
    state.credential_store.set("deepseek", secret)

    status, body, raw = _request(server, "GET", "/api/providers")

    assert status == 200
    assert isinstance(body, list)
    assert body[0]["credential_configured"] is True
    assert secret not in raw
    assert set(body[0]) == {
        "id",
        "name",
        "base_url",
        "protocol",
        "model",
        "capabilities",
        "credential_configured",
        "recent_probe",
    }


def test_post_requires_csrf_and_returns_structured_json_error(server) -> None:
    status, body, raw = _request(
        server,
        "POST",
        "/api/router/start",
        {},
    )

    assert status == 403
    assert body == {"error": {"type": "csrf_required", "message": "csrf required"}}
    assert "fixture" not in raw


def test_credential_write_is_write_only(server, state: ControlCenterState) -> None:
    secret = "fixture-write-only-secret"

    status, body, raw = _request(
        server,
        "POST",
        "/api/providers/deepseek/credential",
        {"secret": secret},
        csrf=state.csrf_token,
    )

    assert status == 200
    assert body == {"configured": True}
    assert secret not in raw
    assert state.credential_store.values["deepseek"] == secret

    _, providers, provider_raw = _request(server, "GET", "/api/providers")
    assert providers[0]["credential_configured"] is True
    assert secret not in provider_raw


def test_official_provider_is_read_only(server, state: ControlCenterState) -> None:
    status, body, _ = _request(
        server,
        "POST",
        "/api/providers",
        {
            "id": "official",
            "name": "Changed Official",
            "base_url": "https://official.example.invalid/v1",
            "protocol": "responses",
            "model": "fixture-official",
            "lane": "official",
        },
        csrf=state.csrf_token,
    )

    assert status == 403
    assert body["error"]["type"] == "read_only"


def test_unconfigured_operations_are_not_success(server, state: ControlCenterState) -> None:
    paths = (
        "/api/config/apply",
        "/api/config/restore",
        "/api/router/start",
        "/api/router/stop",
    )
    for path in paths:
        status, body, _ = _request(server, "POST", path, {}, csrf=state.csrf_token)
        assert status == 501
        assert body == {"status": "unconfigured", "configured": False}

    status, body, _ = _request(server, "GET", "/api/status")
    assert status == 200
    assert body["codex_config"]["applied"] is False
    assert body["codex_config"]["message"] == "尚未应用真实 Codex 配置"
    assert body["smoke"]["enabled"] is False
    assert "保持阻断" in body["smoke"]["message"]


def test_smoke_enabled_state_is_exposed_in_status() -> None:
    state = ControlCenterState(smoke=True)
    status = state.public_status()

    assert status["smoke"]["enabled"] is True
    assert "备份" in status["smoke"]["message"]


def test_invalid_json_has_structured_error(server, state: ControlCenterState) -> None:
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    connection.request(
        "POST",
        "/api/config/apply",
        body=b"not-json",
        headers={"Content-Type": "application/json", CSRF_HEADER: state.csrf_token},
    )
    response = connection.getresponse()
    body = json.loads(response.read().decode("utf-8"))
    connection.close()

    assert response.status == 400
    assert body == {"error": {"type": "invalid_json", "message": "invalid json"}}


def test_homepage_static_assets_are_available(server) -> None:
    for path, marker in (
        ("/", "尚未应用真实 Codex 配置"),
        ("/static/app.js", "fetch"),
        ("/static/app.css", "--ink"),
    ):
        status, body, raw = _request(server, "GET", path)
        assert status == 200
        assert isinstance(body, str)
        assert marker in body
        assert "fixture-secret" not in raw
        assert "localStorage" not in raw


def test_static_controls_expose_immediate_feedback_contract(server) -> None:
    status, script, _ = _request(server, "GET", "/static/app.js")
    assert status == 200
    for marker in (
        "setFeedback",
        "setBusy",
        "AbortController",
        "button.disabled = true",
    ):
        assert marker in script

    status, styles, _ = _request(server, "GET", "/static/app.css")
    assert status == 200
    for marker in (".form-result.is-pending", ".form-result.is-success", ".form-result.is-error"):
        assert marker in styles


def test_run_control_center_returns_bound_server_and_token() -> None:
    instance = run_control_center()
    try:
        assert instance.server_address[0] == "127.0.0.1"
        assert instance.server_address[1] > 0
        assert instance.csrf_token
        assert instance.address == instance.server_address
        assert instance.token == instance.csrf_token
    finally:
        instance.shutdown()
        instance.server_close()


def test_non_loopback_bind_is_rejected() -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        create_control_center_server(host="0.0.0.0")


def test_probe_callback_result_is_sanitized(server, state: ControlCenterState) -> None:
    state.probe_callback = lambda _provider_id, _provider: {
        "status": "ok",
        "latency_ms": 12,
        "secret": "fixture-probe-secret",
        "prompt": "fixture-private-prompt",
    }

    status, body, raw = _request(
        server,
        "POST",
        "/api/providers/deepseek/probe",
        {},
        csrf=state.csrf_token,
    )

    assert status == 200
    assert body == {"status": "ok", "latency_ms": 12}
    assert "fixture-probe-secret" not in raw
    assert "fixture-private-prompt" not in raw


def test_blocked_config_operation_reports_picker_gate_without_success() -> None:
    state = ControlCenterState(
        config_apply=lambda _payload: {
            "status": "blocked",
            "configured": False,
            "reason": "picker_verification_required",
            "message": "real Codex config was not modified",
        }
    )
    server = create_control_center_server(state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body, raw = _request(
            server,
            "POST",
            "/api/config/apply",
            {},
            csrf=state.csrf_token,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 412
    assert body == {
        "status": "blocked",
        "configured": False,
        "reason": "picker_verification_required",
        "message": "real Codex config was not modified",
    }
    assert "Authorization" not in raw
