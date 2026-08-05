"""Bounded async HTTP transport with explicit host and streaming contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

from .routing import (
    HostNotAllowedError,
    RedirectNotAllowedError,
    validate_allowlisted_url,
)


class UpstreamError(Exception):
    """Base error for safe transport failures."""


class RequestLimitError(UpstreamError):
    """Raised when a request exceeds an explicit transport limit."""


class SSEParseError(UpstreamError):
    """Raised when an upstream event cannot be decoded as SSE."""


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """One already-framed SSE event, including its original id and ordering."""

    event: str | None
    data: str
    id: str | None = None
    raw: str = ""

    def encode(self) -> bytes:
        return (self.raw or _format_sse_event(self)).encode("utf-8")


def _format_sse_event(event: SSEEvent) -> str:
    lines: list[str] = []
    if event.id is not None:
        lines.append(f"id: {event.id}")
    if event.event is not None:
        lines.append(f"event: {event.event}")
    for data_line in event.data.split("\n"):
        lines.append(f"data: {data_line}")
    return "\n".join(lines) + "\n\n"


def _parse_sse_block(block: str) -> SSEEvent | None:
    event_name: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []
    for line in block.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line or line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)
    if not data_lines and event_name is None and event_id is None:
        return None
    return SSEEvent(event_name, "\n".join(data_lines), event_id, block + "\n\n")


async def iter_sse_events(chunks: AsyncIterator[bytes]) -> AsyncIterator[SSEEvent]:
    """Parse a byte stream incrementally without buffering the full response."""

    buffer = ""
    try:
        async for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise SSEParseError("upstream stream yielded a non-byte chunk")
            try:
                buffer += chunk.decode("utf-8")
            except UnicodeDecodeError as error:
                raise SSEParseError("upstream SSE is not valid UTF-8") from error
            while True:
                boundary = _find_event_boundary(buffer)
                if boundary is None:
                    break
                block, buffer = (
                    buffer[: boundary[0]],
                    buffer[boundary[0] + boundary[1] :],
                )
                event = _parse_sse_block(block)
                if event is not None:
                    yield event
    finally:
        # The caller owns the response context.  This finally exists so an
        # early consumer close stops parsing immediately.
        pass
    if buffer.strip():
        event = _parse_sse_block(buffer)
        if event is not None:
            yield event


def _find_event_boundary(buffer: str) -> tuple[int, int] | None:
    candidates: list[tuple[int, int]] = []
    for marker, width in (("\r\n\r\n", 4), ("\n\n", 2)):
        index = buffer.find(marker)
        if index >= 0:
            candidates.append((index, width))
    return min(candidates, default=None)


@dataclass(frozen=True, slots=True)
class UpstreamLimits:
    max_body_bytes: int = 8 * 1024 * 1024
    max_header_bytes: int = 64 * 1024
    max_timeout_seconds: float = 120.0


class UpstreamClient:
    """Async HTTPX client with injectable transport and cancellation-safe streams."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_body_bytes: int = 8 * 1024 * 1024,
        max_header_bytes: int = 64 * 1024,
        max_timeout_seconds: float = 120.0,
        max_concurrency: int = 16,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("inject either client or transport, not both")
        if max_body_bytes < 0 or max_header_bytes < 0 or max_timeout_seconds <= 0:
            raise ValueError("transport limits must be positive or zero for byte limits")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self._client = client or httpx.AsyncClient(transport=transport, follow_redirects=False)
        self._owns_client = client is None
        self._limits = UpstreamLimits(
            max_body_bytes=max_body_bytes,
            max_header_bytes=max_header_bytes,
            max_timeout_seconds=max_timeout_seconds,
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def request(
        self,
        url: str,
        *,
        allowed_hosts: set[str] | frozenset[str],
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        body = _encode_json_body(json_body, self._limits.max_body_bytes)
        request_headers = _validate_headers(headers or {}, self._limits.max_header_bytes)
        request_headers.setdefault("Content-Type", "application/json")
        timeout_value = self._validate_timeout(timeout)
        endpoint = validate_allowlisted_url(url, allowed_hosts)
        async with self._semaphore:
            return await self._send_with_redirects(
                endpoint,
                request_headers,
                body,
                timeout_value,
                allowed_hosts,
                follow_redirects,
                stream=False,
            )

    @asynccontextmanager
    async def stream_response(
        self,
        url: str,
        *,
        allowed_hosts: set[str] | frozenset[str],
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
        follow_redirects: bool = False,
    ):
        body = _encode_json_body(json_body, self._limits.max_body_bytes)
        request_headers = _validate_headers(headers or {}, self._limits.max_header_bytes)
        request_headers.setdefault("Content-Type", "application/json")
        timeout_value = self._validate_timeout(timeout)
        endpoint = validate_allowlisted_url(url, allowed_hosts)
        async with self._semaphore:
            response = await self._send_with_redirects(
                endpoint,
                request_headers,
                body,
                timeout_value,
                allowed_hosts,
                follow_redirects,
                stream=True,
            )
            try:
                yield response
            finally:
                await response.aclose()

    def stream_events(
        self,
        url: str,
        *,
        allowed_hosts: set[str] | frozenset[str],
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
        follow_redirects: bool = False,
    ) -> AsyncIterator[SSEEvent]:
        return self._stream_events(
            url,
            allowed_hosts=allowed_hosts,
            headers=headers,
            json_body=json_body,
            timeout=timeout,
            follow_redirects=follow_redirects,
        )

    async def _stream_events(self, url: str, **kwargs: Any) -> AsyncIterator[SSEEvent]:
        async with self.stream_response(url, **kwargs) as response:
            async for event in iter_sse_events(response.aiter_bytes()):
                yield event

    async def _send_with_redirects(
        self,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
        allowed_hosts: set[str] | frozenset[str],
        follow_redirects: bool,
        *,
        stream: bool,
    ) -> httpx.Response:
        current_url = url
        for hop in range(4):
            request = self._client.build_request(
                "POST",
                current_url,
                headers=headers,
                content=body,
                timeout=timeout,
            )
            response = await self._client.send(request, stream=stream, follow_redirects=False)
            if response.status_code < 300 or response.status_code >= 400:
                return response
            location = response.headers.get("location")
            await response.aclose()
            if not follow_redirects or not location:
                raise RedirectNotAllowedError(
                    "upstream redirect is disabled",
                    status_code=response.status_code,
                )
            redirected = urljoin(current_url, location)
            try:
                current_url = validate_allowlisted_url(redirected, allowed_hosts)
            except HostNotAllowedError as error:
                raise RedirectNotAllowedError(
                    "upstream redirect target is not in the route allowlist"
                ) from error
        raise RedirectNotAllowedError("upstream redirect limit exceeded")

    def _validate_timeout(self, timeout: float | None) -> float:
        value = self._limits.max_timeout_seconds if timeout is None else timeout
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise RequestLimitError("timeout must be positive")
        if value > self._limits.max_timeout_seconds:
            raise RequestLimitError("timeout exceeds the configured maximum")
        return float(value)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _encode_json_body(value: Any, maximum: int) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RequestLimitError("request body is not JSON serializable") from error
    if len(encoded) > maximum:
        raise RequestLimitError("request body exceeds the configured maximum")
    return encoded


def _validate_headers(headers: Mapping[str, str], maximum: int) -> dict[str, str]:
    total = 0
    result: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise RequestLimitError("request headers must be text")
        total += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if total > maximum:
            raise RequestLimitError("request headers exceed the configured maximum")
        result[name] = value
    return result
