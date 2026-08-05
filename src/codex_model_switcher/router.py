"""Turn-bounded local router for official and third-party model lanes."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from .adapters.chat import adapt_chat_to_responses
from .adapters.responses import (
    AdapterError,
    ChatToResponsesTextStream,
    UnsupportedItemError,
    UnsupportedStreamChunkError,
    adapt_chat_response_to_responses,
    adapt_responses_to_chat,
    adapt_responses_to_responses,
)
from .catalog import CatalogDocument
from .credentials import (
    CredentialError,
    CredentialStore,
    build_third_party_headers,
    resolve_upstream_auth,
)
from .routing import RouteTarget, RoutingError, RoutingTable
from .state import StateStore
from .upstream import RequestLimitError, SSEEvent, UpstreamClient, UpstreamError, iter_sse_events


class RouterRequestError(Exception):
    """Base request error returned as a structured response."""

    status_code = 400
    error_type = "router_request_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, object]:
        error: dict[str, object] = {"type": self.error_type, "message": self.message}
        error.update(self.details)
        return {"error": error}


class MissingCorrelationError(RouterRequestError):
    error_type = "missing_correlation"


class TurnInProgressError(RouterRequestError):
    status_code = 409
    error_type = "turn_in_progress"


class CrossRouteResponseError(RouterRequestError):
    status_code = 409
    error_type = "cross_route_response"


@dataclass(frozen=True, slots=True, init=False)
class RouterRequest:
    """A single correlated Codex request.

    ``model`` is accepted as a compatibility alias for ``model_id``; the
    router still resolves only the catalog's stable ID.
    """

    model_id: str
    payload: Mapping[str, Any]
    codex_task_id: str
    turn_id: str
    headers: Mapping[str, str]
    api: str
    stream: bool
    timeout: float | None

    def __init__(
        self,
        model_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        codex_task_id: str | None = None,
        turn_id: str | None = None,
        *,
        model: str | None = None,
        headers: Mapping[str, str] | None = None,
        api: str = "responses",
        stream: bool = False,
        timeout: float | None = None,
    ) -> None:
        chosen_model = model_id if model_id is not None else model
        if chosen_model is None:
            raise ValueError("model_id is required")
        if payload is None:
            raise ValueError("payload is required")
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be an object")
        if api not in ("responses", "chat"):
            raise ValueError("api must be responses or chat")
        object.__setattr__(self, "model_id", chosen_model)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "codex_task_id", codex_task_id or "")
        object.__setattr__(self, "turn_id", turn_id or "")
        object.__setattr__(self, "headers", headers or {})
        object.__setattr__(self, "api", api)
        object.__setattr__(self, "stream", stream)
        object.__setattr__(self, "timeout", timeout)


@dataclass(slots=True)
class RouterResponse:
    status_code: int
    body: Any = None
    headers: dict[str, str] | None = None
    events: AsyncIterator[SSEEvent] | None = None
    cancel_handle_id: str | None = None

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {}

    @property
    def is_streaming(self) -> bool:
        return self.events is not None

    def json(self) -> Any:
        if isinstance(self.body, (dict, list)):
            return self.body
        if isinstance(self.body, bytes):
            return json.loads(self.body.decode("utf-8"))
        return self.body

    @property
    def text(self) -> str:
        if isinstance(self.body, str):
            return self.body
        return json.dumps(self.body, ensure_ascii=False, separators=(",", ":"))

    async def aiter_events(self) -> AsyncIterator[SSEEvent]:
        if self.events is None:
            raise RuntimeError("response is not streaming")
        async for event in self.events:
            yield event

    async def aclose(self) -> None:
        """Close a client-disconnected stream and release its cancel handle."""

        if self.events is not None:
            close = getattr(self.events, "aclose", None)
            if callable(close):
                await close()


class _TrackedStream:
    """Async-generator wrapper that handles disconnect before first iteration."""

    def __init__(
        self,
        source: AsyncIterator[SSEEvent],
        on_close: Callable[[], Awaitable[bool]],
    ) -> None:
        self._source = source
        self._on_close = on_close

    def __aiter__(self) -> _TrackedStream:
        return self

    async def __anext__(self) -> SSEEvent:
        return await self._source.__anext__()

    async def aclose(self) -> None:
        close = getattr(self._source, "aclose", None)
        if callable(close):
            await close()
        await self._on_close()


@dataclass(slots=True)
class _ActiveRequest:
    handle_id: str
    request: RouterRequest
    target: RouteTarget
    task: asyncio.Task[Any] | None = None
    cancelled: asyncio.Event = None  # type: ignore[assignment]
    close_upstream: Callable[[], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        self.cancelled = asyncio.Event()


class Router:
    """Route one request at a time and release all active resources on cancel."""

    def __init__(
        self,
        catalog: CatalogDocument,
        *,
        targets: Mapping[str, RouteTarget],
        credential_store: CredentialStore,
        upstream_client: UpstreamClient | None = None,
        state_store: StateStore | None = None,
        provider_id_verifier: object | None = None,
    ) -> None:
        self._table = RoutingTable(catalog, targets)
        self._credential_store = credential_store
        self._upstream = upstream_client or UpstreamClient()
        self._state = state_store
        self._provider_id_verifier = provider_id_verifier
        self._active: dict[str, _ActiveRequest] = {}
        self._active_turns: dict[tuple[str, str], str] = {}
        self._response_routes: dict[str, str] = {}

    async def handle(self, request: RouterRequest) -> RouterResponse:
        try:
            active, body, headers = self._prepare(request)
            if request.stream:
                events = _TrackedStream(
                    self._stream_active(active, body, headers),
                    lambda: self.cancel(active.handle_id),
                )
                return RouterResponse(
                    200,
                    headers={"content-type": "text/event-stream"},
                    events=events,
                    cancel_handle_id=active.handle_id,
                )
            active.task = asyncio.current_task()
            return await self._request_active(active, body, headers)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return _error_response(error)

    async def stream(self, request: RouterRequest) -> RouterResponse:
        """Convenience entry point that always returns a streaming response."""

        if request.stream:
            return await self.handle(request)
        return await self.handle(
            RouterRequest(
                request.model_id,
                request.payload,
                request.codex_task_id,
                request.turn_id,
                headers=request.headers,
                api=request.api,
                stream=True,
                timeout=request.timeout,
            )
        )

    def _prepare(
        self,
        request: RouterRequest,
    ) -> tuple[_ActiveRequest, dict[str, Any], dict[str, str]]:
        if (
            not isinstance(request.codex_task_id, str)
            or not request.codex_task_id.strip()
            or not isinstance(request.turn_id, str)
            or not request.turn_id.strip()
        ):
            raise MissingCorrelationError(
                "codex_task_id and turn_id are required",
                required_fields=["codex_task_id", "turn_id"],
            )
        target = self._table.resolve_target(request.model_id)
        turn_key = (request.codex_task_id, request.turn_id)
        active_model = self._active_turns.get(turn_key)
        if active_model is not None:
            raise TurnInProgressError(
                "the correlated turn is still active",
                codex_task_id=request.codex_task_id,
                turn_id=request.turn_id,
                active_model_id=active_model,
                requested_model_id=request.model_id,
            )
        body = self._adapt_request(request, target)
        self._drop_cross_route_response_id(body, request)
        handle_id = f"cancel-{uuid.uuid4().hex}"
        active = _ActiveRequest(handle_id, request, target)
        self._active[handle_id] = active
        self._active_turns[turn_key] = request.model_id
        try:
            if self._state is not None:
                self._state.save_route_selection(
                    request.codex_task_id,
                    request.turn_id,
                    request.model_id,
                )
                self._state.save_cancel_handle(
                    handle_id,
                    codex_task_id=request.codex_task_id,
                    route_id=request.model_id,
                )
            headers = self._headers_for_route(request, target)
        except BaseException:
            self._finish(active)
            raise
        return active, body, headers

    def _adapt_request(self, request: RouterRequest, target: RouteTarget) -> dict[str, Any]:
        if target.wire_api == request.api == "responses":
            return adapt_responses_to_responses(request.payload, model=target.route.upstream_model)
        if target.wire_api == request.api == "chat":
            body = dict(request.payload)
            body["model"] = target.route.upstream_model
            return self._prepare_deepseek_chat_body(request.payload, target, body)
        if target.wire_api == "chat":
            body = adapt_responses_to_chat(request.payload, model=target.route.upstream_model)
            return self._prepare_deepseek_chat_body(request.payload, target, body)
        return adapt_chat_to_responses(request.payload, model=target.route.upstream_model)

    def _prepare_deepseek_chat_body(
        self,
        source: Mapping[str, Any],
        target: RouteTarget,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        if target.route.provider_id != "deepseek":
            return body
        unsupported: list[str] = []
        if "reasoning" in source:
            unsupported.append("reasoning")
        if any(key in source for key in ("tools", "tool_choice", "parallel_tool_calls")):
            unsupported.append("tools")
        if "thinking" not in source:
            body["thinking"] = {"type": "disabled"}
        else:
            thinking = source["thinking"]
            if not isinstance(thinking, Mapping) or thinking.get("type") != "disabled":
                unsupported.append("thinking")
            else:
                body["thinking"] = dict(thinking)
        if unsupported:
            raise UnsupportedItemError(unsupported)
        return body

    def _drop_cross_route_response_id(
        self,
        body: dict[str, Any],
        request: RouterRequest,
    ) -> None:
        previous_id = body.get("previous_response_id")
        if not isinstance(previous_id, str):
            return
        previous_route = self._response_routes.get(previous_id)
        if previous_route is not None and previous_route != request.model_id:
            # Codex input remains authoritative; only the upstream-specific
            # continuation handle is removed across a route boundary.
            body.pop("previous_response_id", None)

    def _headers_for_route(
        self,
        request: RouterRequest,
        target: RouteTarget,
    ) -> dict[str, str]:
        inbound = {str(key): str(value) for key, value in request.headers.items()}
        if target.route.lane == "third_party":
            headers = build_third_party_headers(
                inbound,
                provider_id=target.route.provider_id,
                credential_store=self._credential_store,
                provider_id_verifier=self._provider_id_verifier,
            )
            headers.setdefault(
                "Accept",
                "text/event-stream" if request.stream else "application/json",
            )
            return headers
        inbound_authorization = next(
            (value for key, value in inbound.items() if key.lower() == "authorization"),
            None,
        )
        resolved = resolve_upstream_auth(
            lane="official",
            provider_id=target.route.provider_id,
            inbound_authorization=inbound_authorization,
            credential_store=self._credential_store,
            provider_id_verifier=self._provider_id_verifier,
        )
        headers = {
            key: value
            for key, value in inbound.items()
            if key.lower() not in {"host", "content-length"}
        }
        for key in list(headers):
            if key.lower() == "authorization":
                del headers[key]
        if resolved is not None:
            headers["Authorization"] = resolved
        headers.setdefault("Accept", "text/event-stream" if request.stream else "application/json")
        return headers

    async def _request_active(
        self,
        active: _ActiveRequest,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> RouterResponse:
        try:
            response = await self._upstream.request(
                active.target.endpoint,
                allowed_hosts=active.target.allowed_hosts,
                headers=headers,
                json_body=body,
                timeout=active.request.timeout,
                follow_redirects=active.target.follow_redirects,
            )
            payload = await _response_json(response)
            if response.status_code >= 400:
                return RouterResponse(response.status_code, payload)
            converted = self._adapt_response(payload, active.request.api, active.target.wire_api)
            self._record_response(active, converted)
            return RouterResponse(response.status_code, converted)
        finally:
            self._finish(active)

    async def _stream_active(
        self,
        active: _ActiveRequest,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> AsyncIterator[SSEEvent]:
        active.task = asyncio.current_task()
        translator = (
            ChatToResponsesTextStream()
            if active.request.api == "responses" and active.target.wire_api == "chat"
            else None
        )
        try:
            if active.cancelled.is_set():
                raise asyncio.CancelledError
            async with self._upstream.stream_response(
                active.target.endpoint,
                allowed_hosts=active.target.allowed_hosts,
                headers=headers,
                json_body=body,
                timeout=active.request.timeout,
                follow_redirects=active.target.follow_redirects,
            ) as response:
                active.close_upstream = response.aclose
                if response.status_code >= 400:
                    payload = await _response_json(response)
                    raise RouterRequestError(
                        "upstream returned an error",
                        upstream_status=response.status_code,
                        upstream_error=payload,
                    )
                async for event in iter_sse_events(response.aiter_bytes()):
                    if active.cancelled.is_set():
                        raise asyncio.CancelledError
                    if translator is None:
                        yield event
                        continue
                    try:
                        translated = translator.translate(event)
                    except UnsupportedStreamChunkError as error:
                        yield translator.error_event(event, error)
                        return
                    for translated_event in translated:
                        yield translated_event
        finally:
            active.close_upstream = None
            self._finish(active)

    def _adapt_response(self, payload: Any, input_api: str, wire_api: str) -> Any:
        if not isinstance(payload, Mapping):
            raise AdapterError("upstream response must be a JSON object")
        if input_api == wire_api:
            return dict(payload)
        if input_api == "responses" and wire_api == "chat":
            return adapt_chat_response_to_responses(payload)
        # A Chat client receiving Responses is intentionally only supported
        # when the response already has a Chat-equivalent shape.
        if "choices" not in payload:
            raise UnsupportedItemError(["responses_output"])
        return dict(payload)

    def _record_response(self, active: _ActiveRequest, payload: Mapping[str, Any]) -> None:
        response_id = payload.get("id")
        if not isinstance(response_id, str) or not response_id:
            return
        self._response_routes[response_id] = active.request.model_id
        if self._state is not None:
            self._state.link_response(
                response_id,
                response_id,
                route_id=active.request.model_id,
                codex_task_id=active.request.codex_task_id,
            )

    async def cancel(self, handle_id: str) -> bool:
        active = self._active.get(handle_id)
        if active is None:
            return False
        active.cancelled.set()
        if active.close_upstream is not None:
            await active.close_upstream()
        task = active.task
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        elif task is None:
            # A response can be cancelled after handle() returns but before a
            # client starts iterating it.  No upstream exists yet, so release
            # the turn immediately and make later iteration observe cancel.
            self._finish(active)
        return True

    async def aclose(self) -> None:
        for handle_id in list(self._active):
            await self.cancel(handle_id)
        await self._upstream.aclose()

    def _finish(self, active: _ActiveRequest) -> None:
        self._active.pop(active.handle_id, None)
        self._active_turns.pop(
            (active.request.codex_task_id, active.request.turn_id),
            None,
        )


async def _response_json(response: httpx.Response) -> Any:
    try:
        if not response.is_closed:
            await response.aread()
        return response.json()
    except (ValueError, json.JSONDecodeError) as error:
        raise UpstreamError("upstream response was not valid JSON") from error


def _error_response(error: Exception) -> RouterResponse:
    status = getattr(error, "status_code", 500)
    if isinstance(error, (RoutingError, RouterRequestError, UnsupportedItemError)):
        body = error.to_dict()
    elif isinstance(error, RequestLimitError):
        status = 413
        body = {"error": {"type": "request_limit", "message": str(error)}}
    elif isinstance(error, UpstreamError):
        status = 502
        body = {"error": {"type": "upstream_error", "message": str(error)}}
    elif isinstance(error, CredentialError):
        status = 424
        body = {"error": {"type": "credential_error", "message": str(error)}}
    elif isinstance(error, (AdapterError, ValueError)):
        status = 400
        body = {"error": {"type": "adapter_error", "message": str(error)}}
    else:
        body = {"error": {"type": "router_error", "message": "request failed"}}
    return RouterResponse(status, body)


__all__ = [
    "CrossRouteResponseError",
    "MissingCorrelationError",
    "Router",
    "RouterRequest",
    "RouterRequestError",
    "RouterResponse",
    "TurnInProgressError",
]
