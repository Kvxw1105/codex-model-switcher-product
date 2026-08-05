"""Explicit Chat-to-Responses request conversion."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from .responses import (
    AdapterError,
    UnsupportedItemError,
    _is_verified_image_url,
    adapt_chat_response_to_responses,
)


def adapt_chat_to_responses(
    payload: Mapping[str, Any],
    *,
    model: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AdapterError("Chat payload must be an object")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise AdapterError("Chat payload messages must be an array")
    input_items: list[dict[str, Any]] = []
    unsupported: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            unsupported.append("message")
            continue
        role = message.get("role")
        if role == "tool":
            output = message.get("content", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id"),
                    "output": output,
                }
            )
            continue
        if role not in {"system", "developer", "user", "assistant"}:
            unsupported.append(str(role or "message"))
            continue
        content = message.get("content", "")
        converted_content = _chat_content_to_responses(content, unsupported)
        if converted_content is not None:
            input_items.append(
                {"type": "message", "role": role, "content": converted_content}
            )
        for tool_call in message.get("tool_calls", []):
            if not isinstance(tool_call, Mapping):
                unsupported.append("tool_call")
                continue
            function = tool_call.get("function")
            if not isinstance(function, Mapping):
                unsupported.append("tool_call")
                continue
            input_items.append(
                {
                    "type": "function_call",
                    "call_id": tool_call.get("id"),
                    "name": function.get("name"),
                    "arguments": function.get("arguments", ""),
                }
            )
    if unsupported:
        raise UnsupportedItemError(unsupported)
    result: dict[str, Any] = {"model": model, "input": input_items}
    for key in ("temperature", "top_p", "stream", "parallel_tool_calls", "tools"):
        if key in payload:
            result[key] = copy.deepcopy(payload[key])
    if "max_tokens" in payload:
        result["max_output_tokens"] = copy.deepcopy(payload["max_tokens"])
    return result


def _chat_content_to_responses(
    content: object,
    unsupported: list[str],
) -> list[dict[str, Any]] | None:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        unsupported.append("message_content")
        return None
    converted: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, Mapping):
            unsupported.append("message_content")
            continue
        part_type = part.get("type")
        if part_type == "text" and isinstance(part.get("text"), str):
            converted.append({"type": "input_text", "text": part["text"]})
        elif part_type == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, Mapping) else image
            if not _is_verified_image_url(url):
                unsupported.append("image_url")
            else:
                converted.append({"type": "input_image", "image_url": url})
        else:
            unsupported.append(str(part_type or "message_content"))
    return converted


chat_to_responses = adapt_chat_to_responses

__all__ = [
    "AdapterError",
    "UnsupportedItemError",
    "adapt_chat_response_to_responses",
    "adapt_chat_to_responses",
    "chat_to_responses",
]
