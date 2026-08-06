"""Loopback HTTP adapter for the asynchronous model Router."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .router import RouterRequest, RouterResponse

MAX_REQUEST_BYTES = 8 * 1024 * 1024
TASK_HEADER = "X-Codex-Task-Id"
TURN_HEADER = "X-Codex-Turn-Id"


def _json_error(error_type: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"type": error_type, "message": message}}


def _encode_json(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _request_api(path: str) -> str | None:
    clean_path = PurePosixPath(urlsplit(path).path)
    if str(clean_path) in {"/v1/responses", "/responses"}:
        return "responses"
    if str(clean_path) in {"/v1/chat/completions", "/chat/completions"}:
        return "chat"
    return None


def _read_json(handler: BaseHTTPRequestHandler) -> Mapping[str, Any] | None:
    raw_length = handler.headers.get("Content-Length")
    try:
        length = int(raw_length or "-1")
    except ValueError:
        length = -1
    if length < 0 or length > MAX_REQUEST_BYTES:
        return None
    try:
        value = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


class RouterHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class RouterHttpService:
    """Own the HTTP server and one event loop for a Router instance."""

    def __init__(
        self,
        router: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        model_aliases: Mapping[str, str] | None = None,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("router HTTP service must bind to 127.0.0.1")
        self.router = router
        self.model_aliases = dict(model_aliases or {})
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            name="codex-router-async",
            daemon=True,
        )
        self.server = RouterHTTPServer((host, port), self._handler_factory())
        self._server_thread = threading.Thread(
            target=self.server.serve_forever,
            name="codex-router-http",
            daemon=True,
        )
        self._stopped = False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def start(self) -> "RouterHttpService":
        self._loop_thread.start()
        self._loop_ready.wait(timeout=5)
        if not self._loop_ready.is_set():
            raise RuntimeError("router event loop did not start")
        self._server_thread.start()
        return self

    @property
    def address(self) -> tuple[str, int]:
        return self.server.server_address

    @property
    def running(self) -> bool:
        return not self._stopped and self._server_thread.is_alive()

    def submit(self, operation: Awaitable[Any]) -> Any:
        if self._stopped:
            raise RuntimeError("router HTTP service is stopped")
        future = asyncio.run_coroutine_threadsafe(operation, self._loop)
        return future.result(timeout=130)

    def stop(self) -> None:
        if self._stopped:
            return
        self.server.shutdown()
        self.server.server_close()
        self._server_thread.join(timeout=5)
        close = getattr(self.router, "aclose", None)
        if callable(close):
            try:
                self.submit(close())
            except (RuntimeError, TimeoutError):
                pass
        self._stopped = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)

    def _handler_factory(self) -> type[BaseHTTPRequestHandler]:
        service = self

        class RouterHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _write(self, status: int, payload: object, content_type: str) -> None:
                encoded = _encode_json(payload)
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _write_error(self, status: int, error_type: str, message: str) -> None:
                self._write(status, _json_error(error_type, message), "application/json")

            def do_POST(self) -> None:  # noqa: N802
                api = _request_api(self.path)
                if api is None:
                    self._write_error(404, "not_found", "router endpoint not found")
                    return
                payload = _read_json(self)
                if payload is None:
                    self._write_error(400, "invalid_json", "request body must be a JSON object")
                    return
                model = payload.get("model")
                if not isinstance(model, str) or not model.strip():
                    self._write_error(400, "invalid_request", "model is required")
                    return
                stream = payload.get("stream", False)
                if not isinstance(stream, bool):
                    self._write_error(400, "invalid_request", "stream must be boolean")
                    return
                task_id = self.headers.get(TASK_HEADER, "")
                turn_id = self.headers.get(TURN_HEADER, "")
                if not task_id.strip() or not turn_id.strip():
                    self._write(
                        400,
                        _json_error(
                            "missing_correlation",
                            "X-Codex-Task-Id and X-Codex-Turn-Id are required",
                        ),
                        "application/json; charset=utf-8",
                    )
                    return
                model_id = service.model_aliases.get(model, model)
                request = RouterRequest(
                    model_id=model_id,
                    payload=payload,
                    codex_task_id=task_id,
                    turn_id=turn_id,
                    headers={key: value for key, value in self.headers.items()},
                    api=api,
                    stream=stream,
                )
                if stream:
                    service.submit(self._stream_response(request))
                    return
                try:
                    response = service.submit(service.router.handle(request))
                except (RuntimeError, TimeoutError):
                    self._write_error(504, "router_timeout", "router request timed out")
                    return
                self._write_router_response(response)

            async def _stream_response(self, request: RouterRequest) -> None:
                response = await service.router.handle(request)
                if not response.is_streaming:
                    self._write_router_response(response)
                    return
                self.send_response(response.status_code)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    async for event in response.aiter_events():
                        self.wfile.write(event.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    await response.aclose()
                finally:
                    await response.aclose()

            def _write_router_response(self, response: RouterResponse) -> None:
                if response.is_streaming:
                    self._write_error(500, "router_error", "stream response requires stream=true")
                    return
                self._write(
                    response.status_code,
                    response.body,
                    "application/json; charset=utf-8",
                )

        return RouterHandler


def start_router_http(
    router: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    model_aliases: Mapping[str, str] | None = None,
) -> RouterHttpService:
    """Start a loopback-only HTTP adapter for one Router instance."""

    return RouterHttpService(
        router,
        host=host,
        port=port,
        model_aliases=model_aliases,
    ).start()


__all__ = ["RouterHttpService", "start_router_http"]
