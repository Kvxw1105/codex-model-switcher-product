"""Local, loopback-only control center for the Codex model switcher.

The module deliberately owns only control-plane concerns.  Chat content and
credentials remain outside the HTTP response surface; integration points for
the config and Router layers are injected callbacks until the CLI wires them.
"""

from __future__ import annotations

import inspect
import json
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .credentials import configure_credential
from .models import ModelRoute

CSRF_HEADER = "X-Codex-CSRF"
STARTUP_TOKEN_HEADER = "X-Codex-Startup-Token"
MAX_JSON_BYTES = 1024 * 1024

_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "key",
    "password",
    "prompt",
    "response",
    "secret",
    "token",
}
_CAPABILITY_KEYS = (
    "context_window",
    "supports_responses",
    "supports_streaming",
    "supports_tools",
    "supports_images",
    "supports_files",
    "supports_compaction_context",
)
_SAFE_RESULT_KEYS = {
    "status",
    "configured",
    "running",
    "ok",
    "latency_ms",
    "checked_at",
    "recent_success_at",
    "backup_available",
    "config_applied",
    "message",
    "reason",
    "address",
    "port",
}
_SAFE_STATUSES = {
    "ok",
    "failed",
    "unconfigured",
    "running",
    "stopped",
    "unknown",
    "blocked",
}

Callback = Callable[..., Mapping[str, object] | None]


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        converted = asdict(value)
        if isinstance(converted, Mapping):
            return converted
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, Mapping):
        return attributes
    return {}


def _text(value: object, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


def _safe_capabilities(value: object) -> dict[str, object]:
    source = _mapping(value)
    result: dict[str, object] = {}
    for key in _CAPABILITY_KEYS:
        item = source.get(key)
        if isinstance(item, bool) or (key == "context_window" and isinstance(item, int)):
            result[key] = item
    return result


def _safe_probe(value: object) -> dict[str, object]:
    source = _mapping(value)
    status = _text(source.get("status"), "unknown").lower()
    if status not in _SAFE_STATUSES:
        status = "unknown"
    result: dict[str, object] = {"status": status}
    latency = source.get("latency_ms")
    if isinstance(latency, (int, float)) and not isinstance(latency, bool) and latency >= 0:
        result["latency_ms"] = latency
    checked_at = source.get("checked_at")
    if isinstance(checked_at, str) and len(checked_at) <= 64:
        result["checked_at"] = checked_at
    return result


def _contains_sensitive_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower().replace("-", "_") in _SENSITIVE_KEYS:
                return True
            if _contains_sensitive_key(nested):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def _normalise_provider(value: object) -> dict[str, object]:
    source = _mapping(value)
    provider_id = _text(source.get("id"), _text(source.get("provider_id")))
    if not provider_id or any(character.isspace() for character in provider_id):
        raise ValueError("provider id is invalid")
    name = _text(source.get("name"), provider_id)
    base_url = _text(source.get("base_url"))
    if not base_url:
        raise ValueError("base_url is required")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("base_url is invalid")
    protocol = _text(source.get("protocol"), _text(source.get("wire_api"), "responses"))
    if protocol == "chat_completions":
        protocol = "chat"
    if protocol not in {"responses", "chat"}:
        raise ValueError("protocol is invalid")
    model = _text(source.get("model"), _text(source.get("upstream_model")))
    if not model:
        raise ValueError("model is required")
    lane = _text(source.get("lane"), "third_party")
    if lane not in {"official", "third_party"}:
        raise ValueError("lane is invalid")
    capabilities = _safe_capabilities(source.get("capabilities", source.get("capability", {})))
    return {
        "id": provider_id,
        "name": name,
        "base_url": base_url,
        "protocol": protocol,
        "model": model,
        "capabilities": capabilities,
        "_lane": lane,
        "recent_probe": _safe_probe(source.get("recent_probe", {})),
    }


def _route_record(value: object) -> dict[str, object]:
    if isinstance(value, ModelRoute):
        return {
            "id": value.model_id,
            "name": value.display_name,
            "lane": value.lane,
            "provider_id": value.provider_id,
            "upstream_model": value.upstream_model,
            "capabilities": _safe_capabilities(value.capability),
        }
    source = _mapping(value)
    model_id = _text(source.get("id"), _text(source.get("model_id")))
    display_name = _text(source.get("name"), _text(source.get("display_name"), model_id))
    return {
        "id": model_id,
        "name": display_name,
        "lane": _text(source.get("lane"), "third_party"),
        "provider_id": _text(source.get("provider_id")),
        "upstream_model": _text(source.get("upstream_model"), _text(source.get("model"))),
        "capabilities": _safe_capabilities(
            source.get("capabilities", source.get("capability", {}))
        ),
    }


def _safe_result(value: object) -> dict[str, object]:
    source = _mapping(value)
    result: dict[str, object] = {}
    for key in _SAFE_RESULT_KEYS:
        item = source.get(key)
        if key == "status":
            status = _text(item, "unknown").lower()
            result[key] = status if status in _SAFE_STATUSES else "unknown"
        elif key in {"configured", "running", "ok", "backup_available", "config_applied"}:
            if isinstance(item, bool):
                result[key] = item
        elif key == "latency_ms":
            if isinstance(item, (int, float)) and not isinstance(item, bool) and item >= 0:
                result[key] = item
        elif key == "port":
            if isinstance(item, int) and not isinstance(item, bool) and 0 < item <= 65535:
                result[key] = item
        elif key == "message":
            if isinstance(item, str) and len(item) <= 256:
                result[key] = item
        elif key == "reason":
            if isinstance(item, str) and len(item) <= 128:
                result[key] = item
        elif key == "address":
            if isinstance(item, str) and len(item) <= 128:
                result[key] = item
        elif isinstance(item, str) and len(item) <= 64:
            result[key] = item
    return result


class ControlCenterState:
    """Injectable control-plane state and integration seams for the GUI."""

    def __init__(
        self,
        providers: Sequence[object] | Mapping[str, object] | None = None,
        models: Sequence[object] | None = None,
        *,
        catalog: object | None = None,
        credential_store: object | None = None,
        probe_callback: Callback | None = None,
        config_apply: Callback | None = None,
        config_restore: Callback | None = None,
        router_start: Callback | None = None,
        router_stop: Callback | None = None,
        official_identity_available: bool = False,
        smoke: bool = False,
    ) -> None:
        self.csrf_token = secrets.token_urlsafe(32)
        self.startup_token = self.csrf_token
        self.smoke = bool(smoke)
        self.credential_store = credential_store
        self.probe_callback = probe_callback
        self.config_apply_callback = config_apply
        self.config_restore_callback = config_restore
        self.router_start_callback = router_start
        self.router_stop_callback = router_stop
        self.official_identity_available = bool(official_identity_available)
        self.codex_config_applied = False
        self.router_running = False
        self.router_address: str | None = None
        self._providers: dict[str, dict[str, object]] = {}
        raw_providers = providers.values() if isinstance(providers, Mapping) else (providers or ())
        for provider in raw_providers:
            normalised = _normalise_provider(provider)
            self._providers[str(normalised["id"])] = normalised
        raw_models = getattr(catalog, "models", None) if catalog is not None else models
        self._models = [_route_record(route) for route in (raw_models or ())]
        for route in self._models:
            provider_id = str(route["provider_id"])
            if provider_id not in self._providers:
                self._providers[provider_id] = {
                    "id": provider_id,
                    "name": provider_id,
                    "base_url": "https://example.invalid",
                    "protocol": "responses",
                    "model": route["upstream_model"],
                    "capabilities": route["capabilities"],
                    "_lane": route["lane"],
                    "recent_probe": {"status": "unknown"},
                }
        self._provider_ids = frozenset(self._providers)

    def _credential_configured(self, provider_id: str) -> bool:
        if self.credential_store is None:
            return False
        try:
            return bool(self.credential_store.exists(provider_id))
        except Exception:
            return False

    def public_provider(self, provider_id: str) -> dict[str, object]:
        provider = self._providers[provider_id]
        return {
            "id": provider["id"],
            "name": provider["name"],
            "base_url": provider["base_url"],
            "protocol": provider["protocol"],
            "model": provider["model"],
            "capabilities": dict(provider["capabilities"]),
            "credential_configured": self._credential_configured(provider_id),
            "recent_probe": dict(provider["recent_probe"]),
        }

    def public_providers(self) -> list[dict[str, object]]:
        return [self.public_provider(provider_id) for provider_id in sorted(self._providers)]

    def public_models(self) -> list[dict[str, object]]:
        result = []
        for route in self._models:
            item = dict(route)
            provider = self._providers.get(str(route["provider_id"]))
            item["recent_probe"] = (
                dict(provider["recent_probe"]) if provider else {"status": "unknown"}
            )
            result.append(item)
        return result

    def public_status(self) -> dict[str, object]:
        router_status = "running" if self.router_running else "stopped"
        if not any((self.router_start_callback, self.router_stop_callback)):
            router_status = "unconfigured"
        router = {
            "status": router_status,
            "running": self.router_running,
            "configured": bool(self.router_start_callback or self.router_stop_callback),
        }
        if self.router_address:
            router["address"] = self.router_address
        return {
            "router": router,
            "smoke": {
                "enabled": self.smoke,
                "message": (
                    "smoke 开关已开启：apply/restore 会修改显式指定的配置并保留"
                    "备份与 SHA-256 证据"
                    if self.smoke
                    else "smoke 开关未开启：config apply/restore 保持阻断"
                ),
            },
            "codex_config": {
                "applied": self.codex_config_applied,
                "status": "applied" if self.codex_config_applied else "not_applied",
                "message": "尚未应用真实 Codex 配置"
                if not self.codex_config_applied
                else "Codex 配置已由控制中心应用",
            },
            "official_identity": {"available": self.official_identity_available},
            "providers": self.public_providers(),
            "models": self.public_models(),
        }

    def upsert_provider(self, payload: Mapping[str, object]) -> dict[str, object]:
        if _contains_sensitive_key(payload):
            raise ValueError("provider payload contains a restricted field")
        candidate = _normalise_provider(payload)
        provider_id = str(candidate["id"])
        existing = self._providers.get(provider_id)
        if (existing and existing.get("_lane") == "official") or candidate["_lane"] == "official":
            raise PermissionError("official providers are read-only")
        if existing:
            candidate["recent_probe"] = existing.get("recent_probe", {"status": "unknown"})
        self._providers[provider_id] = candidate
        self._provider_ids = frozenset(self._providers)
        return self.public_provider(provider_id)

    def set_credential(self, provider_id: str, payload: Mapping[str, object]) -> dict[str, bool]:
        if provider_id not in self._providers:
            raise LookupError("provider not found")
        if self._providers[provider_id].get("_lane") == "official":
            raise PermissionError("official providers are read-only")
        value = payload.get("secret", payload.get("credential"))
        if not isinstance(value, str) or not value.strip():
            raise ValueError("credential is required")
        if self.credential_store is None:
            return {"configured": False}
        result = configure_credential(
            self.credential_store,
            provider_id,
            value,
            provider_id_verifier=self._provider_ids,
        )
        return {"configured": bool(result.get("configured", False))}

    def probe(self, provider_id: str) -> tuple[int, dict[str, object]]:
        if provider_id not in self._providers:
            raise LookupError("provider not found")
        if self.probe_callback is None:
            result = {"status": "unconfigured"}
            self._providers[provider_id]["recent_probe"] = result
            return 501, {"status": "unconfigured", "configured": False}
        try:
            raw = _invoke(self.probe_callback, provider_id, self.public_provider(provider_id))
        except Exception:
            result = {"status": "failed"}
            self._providers[provider_id]["recent_probe"] = result
            return 502, {"status": "failed"}
        result = _safe_result(raw)
        result.setdefault("status", "unknown")
        self._providers[provider_id]["recent_probe"] = _safe_probe(result)
        return 200, result

    def operation(self, name: str, payload: Mapping[str, object]) -> tuple[int, dict[str, object]]:
        callbacks = {
            "config_apply": self.config_apply_callback,
            "config_restore": self.config_restore_callback,
            "router_start": self.router_start_callback,
            "router_stop": self.router_stop_callback,
        }
        callback = callbacks[name]
        if callback is None:
            return 501, {"status": "unconfigured", "configured": False}
        try:
            raw = _invoke(callback, payload)
        except Exception:
            return 502, {"status": "failed"}
        result = _safe_result(raw)
        result.setdefault("status", "ok")
        status = result["status"]
        if name == "config_apply" and status == "ok":
            self.codex_config_applied = True
        elif name == "config_restore" and status == "ok":
            self.codex_config_applied = False
        elif name == "router_start" and status == "ok":
            self.router_running = True
            address = result.get("address")
            self.router_address = address if isinstance(address, str) else None
        elif name == "router_stop" and status == "ok":
            self.router_running = False
            self.router_address = None
        status_code = {"blocked": 412, "unconfigured": 501, "failed": 502}.get(status, 200)
        return status_code, result


def _invoke(callback: Callback, *arguments: object) -> Mapping[str, object] | None:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(*arguments)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    if any(
        parameter.kind == parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    ):
        return callback(*arguments)
    return callback(*arguments[: len(positional)])


def _is_loopback(address: object) -> bool:
    try:
        return bool(ip_address(str(address)).is_loopback)
    except ValueError:
        return False


def _error_body(error_type: str) -> dict[str, object]:
    return {"error": {"type": error_type, "message": error_type.replace("_", " ")}}


def make_control_center_handler(state: ControlCenterState) -> type[BaseHTTPRequestHandler]:
    """Return a handler class bound to an injectable state instance."""

    class ControlCenterHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _write_json(self, status: int, payload: Mapping[str, object] | list[object]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Codex-CSRF", state.csrf_token)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _write_error(self, status: int, error_type: str) -> None:
            self._write_json(status, _error_body(error_type))

        def _loopback_or_reject(self) -> bool:
            if _is_loopback(self.client_address[0]):
                return True
            self._write_error(403, "loopback_required")
            return False

        def _csrf_or_reject(self) -> bool:
            token = self.headers.get(CSRF_HEADER) or self.headers.get(STARTUP_TOKEN_HEADER)
            if not token or not secrets.compare_digest(token, state.csrf_token):
                self._write_error(403, "csrf_required")
                return False
            return True

        def _path(self) -> str | None:
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                self._write_error(400, "query_not_allowed")
                return None
            return parsed.path

        def _read_json(self) -> Mapping[str, object] | None:
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "-1")
            except ValueError:
                length = -1
            if length < 0 or length > MAX_JSON_BYTES:
                self._write_error(413, "invalid_json")
                return None
            try:
                parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._write_error(400, "invalid_json")
                return None
            if not isinstance(parsed, Mapping):
                self._write_error(400, "invalid_json")
                return None
            return parsed

        def do_GET(self) -> None:  # noqa: N802
            if not self._loopback_or_reject():
                return
            path = self._path()
            if path is None:
                return
            if path == "/api/status":
                self._write_json(200, state.public_status())
                return
            if path == "/api/providers":
                self._write_json(200, state.public_providers())
                return
            if path == "/api/models":
                self._write_json(200, state.public_models())
                return
            if path in {"/", "/index.html"}:
                self._write_static("templates/index.html", "text/html; charset=utf-8")
                return
            if path == "/static/app.js":
                self._write_static("static/app.js", "text/javascript; charset=utf-8")
                return
            if path == "/static/app.css":
                self._write_static("static/app.css", "text/css; charset=utf-8")
                return
            self._write_error(404, "not_found")

        def _write_static(self, relative_path: str, content_type: str) -> None:
            file_path = Path(__file__).with_name(relative_path.split("/", 1)[0]).joinpath(
                relative_path.split("/", 1)[1]
            )
            try:
                content = file_path.read_bytes()
            except OSError:
                self._write_error(404, "not_found")
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'",
            )
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:  # noqa: N802
            if not self._loopback_or_reject() or not self._csrf_or_reject():
                return
            path = self._path()
            if path is None:
                return
            payload = self._read_json()
            if payload is None:
                return
            try:
                if path == "/api/providers":
                    self._write_json(200, state.upsert_provider(payload))
                    return
                parts = [unquote(part) for part in path.split("/") if part]
                if len(parts) == 4 and parts[:2] == ["api", "providers"]:
                    provider_id = parts[2]
                    operation = parts[3]
                    if operation == "credential":
                        self._write_json(200, state.set_credential(provider_id, payload))
                        return
                    if operation == "probe":
                        status, result = state.probe(provider_id)
                        self._write_json(status, result)
                        return
                operation_map = {
                    "/api/config/apply": "config_apply",
                    "/api/config/restore": "config_restore",
                    "/api/router/start": "router_start",
                    "/api/router/stop": "router_stop",
                }
                if path in operation_map:
                    status, result = state.operation(operation_map[path], payload)
                    self._write_json(status, result)
                    return
                self._write_error(404, "not_found")
            except LookupError:
                self._write_error(404, "not_found")
            except PermissionError:
                self._write_error(403, "read_only")
            except (TypeError, ValueError):
                self._write_error(400, "invalid_request")

    return ControlCenterHandler


ControlCenterHandlerFactory = make_control_center_handler


class ControlCenterServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], state: ControlCenterState) -> None:
        host, _port = server_address
        if host != "127.0.0.1":
            raise ValueError("control center must bind to 127.0.0.1")
        self.state = state
        super().__init__(server_address, make_control_center_handler(state))

    @property
    def csrf_token(self) -> str:
        return self.state.csrf_token

    @property
    def address(self) -> tuple[str, int]:
        return self.server_address

    @property
    def token(self) -> str:
        return self.csrf_token


def create_control_center_server(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    state: ControlCenterState | None = None,
    **state_kwargs: object,
) -> ControlCenterServer:
    """Create a server without starting it; ideal for CLI and tests."""

    return ControlCenterServer(
        (host, port),
        state or ControlCenterState(**state_kwargs),
    )


def run_control_center(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    state: ControlCenterState | None = None,
    **state_kwargs: object,
) -> ControlCenterServer:
    """Start a daemon serving thread and return its address/token-bearing server.

    A CLI can keep its process alive and call ``server.shutdown()`` on exit;
    tests can use the returned ``server.server_address`` and ``server.csrf_token``.
    """

    server = create_control_center_server(host, port, state=state, **state_kwargs)
    thread = threading.Thread(target=server.serve_forever, name="codex-control-center", daemon=True)
    thread.start()
    server.thread = thread  # type: ignore[attr-defined]
    return server


__all__ = [
    "CSRF_HEADER",
    "STARTUP_TOKEN_HEADER",
    "ControlCenterHandlerFactory",
    "ControlCenterServer",
    "ControlCenterState",
    "create_control_center_server",
    "make_control_center_handler",
    "run_control_center",
]
