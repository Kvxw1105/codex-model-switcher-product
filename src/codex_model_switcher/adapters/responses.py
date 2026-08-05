"""Responses protocol preservation and explicit Responses-to-Chat conversion."""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..upstream import SSEEvent


class AdapterError(ValueError):
    """Base error for protocol conversion failures."""


class UnsupportedItemError(AdapterError):
    """Raised when no safe equivalent exists in the destination protocol."""

    status_code = 422

    def __init__(self, unsupported_item_types: Sequence[str]) -> None:
        unique = list(dict.fromkeys(str(item) for item in unsupported_item_types))
        self.unsupported_item_types = tuple(unique)
        super().__init__(
            "the requested Responses items have no equivalent Chat representation: "
            + ", ".join(unique)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "error": {
                "type": "unsupported_item",
                "message": str(self),
                "unsupported_item_types": list(self.unsupported_item_types),
            }
        }


@dataclass(frozen=True, slots=True)
class AdapterResult:
    payload: dict[str, Any]
    compatibility_warnings: tuple[str, ...] = ()


class UnsupportedStreamChunkError(AdapterError):
    """A Chat stream chunk has no safe text-only Responses equivalent."""

    def __init__(self, unsupported_type: str) -> None:
        self.unsupported_type = unsupported_type
        super().__init__(f"unsupported upstream streaming chunk: {unsupported_type}")


class ChatToResponsesTextStream:
    """Translate one single-choice Chat text stream into Responses events.

    The translator deliberately has no tool/reasoning fallback.  It reuses
    upstream response metadata and only emits usage when Chat supplied it.
    """

    def __init__(self) -> None:
        self._response_id: str | None = None
        self._model: str | None = None
        self._created_at: int | None = None
        self._text = ""
        self._usage: Any = None
        self._started = False
        self._finished = False
        self._saw_finish_reason = False
        self._sequence_number = 0

    def translate(self, event: SSEEvent) -> tuple[SSEEvent, ...]:
        if self._finished:
            if event.data.strip() == "[DONE]":
                return ()
            raise UnsupportedStreamChunkError("post_completion_chunk")
        if event.event not in (None, "message"):
            raise UnsupportedStreamChunkError("unknown_chunk")
        if event.data.strip() == "[DONE]":
            return self._finish(event.id)
        try:
            payload = json.loads(event.data)
        except json.JSONDecodeError as error:
            raise UnsupportedStreamChunkError("unknown_chunk") from error
        if not isinstance(payload, Mapping):
            raise UnsupportedStreamChunkError("unknown_chunk")

        self._read_metadata(payload)
        output: list[SSEEvent] = []

        usage = payload.get("usage")
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise UnsupportedStreamChunkError("usage")
            self._usage = copy.deepcopy(dict(usage))

        choices = payload.get("choices")
        if choices == [] and usage is not None:
            if not self._started:
                output.append(self._created(event.id))
                self._started = True
            return tuple(output)
        if not isinstance(choices, list) or len(choices) != 1:
            raise UnsupportedStreamChunkError("unknown_chunk")
        choice = choices[0]
        if not isinstance(choice, Mapping) or choice.get("index", 0) != 0:
            raise UnsupportedStreamChunkError("unknown_chunk")
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            raise UnsupportedStreamChunkError("unknown_chunk")
        self._reject_unsupported_delta(delta)
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise UnsupportedStreamChunkError("non_text_content")
        if not self._started:
            output.append(self._created(event.id))
            self._started = True
        if isinstance(content, str):
            self._text += content
            output.append(
                self._event(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": self._response_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": content,
                    },
                    event.id,
                )
            )
        if choice.get("finish_reason") is not None:
            self._saw_finish_reason = True
            output.extend(self._finish(event.id))
        return tuple(output)

    def error_event(self, event: SSEEvent, error: UnsupportedStreamChunkError) -> SSEEvent:
        return self._event(
            "error",
            {
                "type": "error",
                "code": "unsupported_upstream_chunk",
                "message": str(error),
                "param": error.unsupported_type,
            },
            event.id,
        )

    def _read_metadata(self, payload: Mapping[str, Any]) -> None:
        response_id = payload.get("id")
        model = payload.get("model")
        created = payload.get("created")
        if not self._started:
            if not isinstance(response_id, str) or not response_id:
                raise UnsupportedStreamChunkError("missing_response_id")
            if not isinstance(model, str) or not model:
                raise UnsupportedStreamChunkError("missing_model")
            if not isinstance(created, int) or isinstance(created, bool):
                raise UnsupportedStreamChunkError("missing_created")
            self._response_id = response_id
            self._model = model
            self._created_at = created
            return
        if response_id is not None and response_id != self._response_id:
            raise UnsupportedStreamChunkError("response_id_changed")
        if model is not None and model != self._model:
            raise UnsupportedStreamChunkError("model_changed")
        if created is not None and created != self._created_at:
            raise UnsupportedStreamChunkError("created_changed")

    @staticmethod
    def _reject_unsupported_delta(delta: Mapping[str, Any]) -> None:
        if "reasoning_content" in delta:
            raise UnsupportedStreamChunkError("reasoning_content")
        if "tool_calls" in delta:
            raise UnsupportedStreamChunkError("tool_calls")
        allowed = {"role", "content"}
        if any(key not in allowed for key in delta):
            raise UnsupportedStreamChunkError("unknown_chunk")

    def _created(self, event_id: str | None) -> SSEEvent:
        return self._event(
            "response.created",
            {
                "type": "response.created",
                "response": {
                    "id": self._response_id,
                    "object": "response",
                    "created_at": self._created_at,
                    "status": "in_progress",
                    "model": self._model,
                    "output": [],
                },
            },
            event_id,
        )

    def _finish(self, event_id: str | None) -> tuple[SSEEvent, ...]:
        if self._finished:
            return ()
        self._finished = True
        output_text = {
            "type": "output_text",
            "text": self._text,
            "annotations": [],
        }
        output_item = {
            "id": self._response_id,
            "status": "completed",
            "type": "message",
            "role": "assistant",
            "content": [output_text],
        }
        completed_response: dict[str, Any] = {
            "id": self._response_id,
            "object": "response",
            "created_at": self._created_at,
            "status": "completed",
            "model": self._model,
            "output": [output_item],
        }
        if self._usage is not None:
            completed_response["usage"] = self._usage
        warnings: list[str] = []
        if not self._saw_finish_reason:
            warnings.append("missing_finish_reason")
        if self._usage is None:
            warnings.append("missing_usage")
        if warnings:
            completed_response["compatibility_warnings"] = warnings
        return (
            self._event(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": self._response_id,
                    "output_index": 0,
                    "content_index": 0,
                    "text": self._text,
                },
                event_id,
            ),
            self._event(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "item_id": self._response_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": output_text,
                },
                event_id,
            ),
            self._event(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": output_item,
                },
                event_id,
            ),
            self._event(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": completed_response,
                },
                event_id,
            ),
        )

    def _event(self, event_name: str, payload: Mapping[str, Any], event_id: str | None) -> SSEEvent:
        event_payload = dict(payload)
        event_payload["sequence_number"] = self._sequence_number
        self._sequence_number += 1
        return SSEEvent(
            event_name,
            json.dumps(event_payload, ensure_ascii=False, separators=(",", ":")),
            event_id,
        )


def adapt_responses_to_responses(
    payload: Mapping[str, Any],
    *,
    model: str | None = None,
) -> dict[str, Any]:
    """Copy a Responses request without dropping unknown fields or items."""

    if not isinstance(payload, Mapping):
        raise AdapterError("Responses payload must be an object")
    result = copy.deepcopy(dict(payload))
    if model is not None:
        result["model"] = model
    return result


def adapt_responses_to_chat(
    payload: Mapping[str, Any],
    *,
    model: str,
) -> dict[str, Any]:
    """Convert only the explicitly supported Responses item allowlist."""

    if not isinstance(payload, Mapping):
        raise AdapterError("Responses payload must be an object")
    messages: list[dict[str, Any]] = []
    unsupported: list[str] = []

    instructions = payload.get("instructions")
    if instructions is not None:
        if isinstance(instructions, str):
            messages.append({"role": "system", "content": instructions})
        else:
            unsupported.append("instructions")

    raw_input = payload.get("input", [])
    input_items: list[Any]
    if isinstance(raw_input, str):
        input_items = [{"type": "message", "role": "user", "content": raw_input}]
    elif isinstance(raw_input, Mapping):
        input_items = [raw_input]
    elif isinstance(raw_input, list):
        input_items = raw_input
    else:
        unsupported.append("input")
        input_items = []

    for item in input_items:
        if not isinstance(item, Mapping):
            unsupported.append("unknown")
            continue
        item_type = item.get("type")
        if item_type == "message":
            converted = _message_item_to_chat(item, unsupported)
            if converted is not None:
                messages.append(converted)
        elif item_type in {"input_text", "text", "output_text"}:
            text = item.get("text")
            if isinstance(text, str):
                messages.append({"role": "user", "content": text})
            else:
                unsupported.append(str(item_type))
        elif item_type == "function_call":
            converted = _function_call_to_chat(item)
            if converted is None:
                unsupported.append("function_call")
            else:
                messages.append(converted)
        elif item_type == "function_call_output":
            converted = _function_call_output_to_chat(item)
            if converted is None:
                unsupported.append("function_call_output")
            else:
                messages.append(converted)
        else:
            unsupported.append(str(item_type or "unknown"))

    if unsupported:
        raise UnsupportedItemError(unsupported)

    result: dict[str, Any] = {"model": model, "messages": messages}
    _copy_chat_compatible_request_fields(payload, result)
    return result


def _message_item_to_chat(
    item: Mapping[str, Any],
    unsupported: list[str],
) -> dict[str, Any] | None:
    role = item.get("role")
    if role not in {"system", "developer", "user", "assistant", "tool"}:
        unsupported.append("message")
        return None
    raw_content = item.get("content", "")
    if isinstance(raw_content, str):
        return {"role": role, "content": raw_content}
    if not isinstance(raw_content, list):
        unsupported.append("message")
        return None
    content: list[dict[str, Any]] = []
    for part in raw_content:
        if not isinstance(part, Mapping):
            unsupported.append("message_content")
            continue
        part_type = part.get("type")
        if part_type in {"input_text", "text", "output_text"} and isinstance(
            part.get("text"), str
        ):
            content.append({"type": "text", "text": part["text"]})
        elif part_type in {"input_image", "image_url"}:
            image_url = part.get("image_url")
            if isinstance(image_url, Mapping):
                image_url = image_url.get("url")
            if not _is_verified_image_url(image_url):
                unsupported.append("input_image")
            else:
                content.append({"type": "image_url", "image_url": {"url": image_url}})
        else:
            unsupported.append(str(part_type or "message_content"))
    return {"role": role, "content": content}


def _function_call_to_chat(item: Mapping[str, Any]) -> dict[str, Any] | None:
    name = item.get("name")
    arguments = item.get("arguments")
    call_id = item.get("call_id", item.get("id"))
    if not all(isinstance(value, str) and value for value in (name, arguments, call_id)):
        return None
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


def _function_call_output_to_chat(item: Mapping[str, Any]) -> dict[str, Any] | None:
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    output = item.get("output", "")
    if not isinstance(output, str):
        try:
            output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
    return {"role": "tool", "tool_call_id": call_id, "content": output}


def _copy_chat_compatible_request_fields(
    source: Mapping[str, Any],
    destination: dict[str, Any],
) -> None:
    for key in ("temperature", "top_p", "stream", "parallel_tool_calls"):
        if key in source:
            destination[key] = copy.deepcopy(source[key])
    if "max_tokens" in source:
        destination["max_tokens"] = copy.deepcopy(source["max_tokens"])
    elif "max_output_tokens" in source:
        destination["max_tokens"] = copy.deepcopy(source["max_output_tokens"])
    raw_tools = source.get("tools")
    if raw_tools is not None:
        if not isinstance(raw_tools, list):
            raise UnsupportedItemError(["tools"])
        converted_tools: list[dict[str, Any]] = []
        unsupported: list[str] = []
        for tool in raw_tools:
            if not isinstance(tool, Mapping) or tool.get("type") != "function":
                tool_type = tool.get("type", "tool") if isinstance(tool, Mapping) else "tool"
                unsupported.append(str(tool_type))
                continue
            converted_tools.append(copy.deepcopy(dict(tool)))
        if unsupported:
            raise UnsupportedItemError(unsupported)
        destination["tools"] = converted_tools


def _is_verified_image_url(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if re.fullmatch(
        r"data:image/(?:png|jpe?g|webp);base64,[A-Za-z0-9+/]+={0,2}",
        value,
    ):
        try:
            base64.b64decode(value.split(",", 1)[1], validate=True)
        except (ValueError, binascii.Error):
            return False
        return True
    parsed = urlsplit(value)
    return parsed.scheme == "https" and parsed.path.lower().endswith(
        (".png", ".jpg", ".jpeg", ".webp")
    )


def adapt_chat_response_to_responses(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Map a Chat response while reporting fields Chat did not supply."""

    if not isinstance(payload, Mapping):
        raise AdapterError("Chat response must be an object")
    result: dict[str, Any] = {}
    warnings: list[str] = []
    for key in ("id", "model", "created"):
        if key in payload:
            result[key] = copy.deepcopy(payload[key])
    if "id" not in payload:
        warnings.append("missing_response_id")
    if "usage" not in payload:
        warnings.append("missing_usage")
    choices = payload.get("choices")
    if not isinstance(choices, list):
        raise AdapterError("Chat response choices must be an array")
    output: list[dict[str, Any]] = []
    for choice in choices:
        if not isinstance(choice, Mapping):
            raise AdapterError("Chat response choice must be an object")
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise AdapterError("Chat response message is missing")
        item: dict[str, Any] = {"type": "message", "role": "assistant"}
        content = message.get("content")
        if isinstance(content, str):
            item["content"] = [{"type": "output_text", "text": content}]
        else:
            item["content"] = []
        finish_reason = choice.get("finish_reason")
        if finish_reason is None:
            warnings.append("missing_finish_reason")
        else:
            item["finish_reason"] = finish_reason
        output.append(item)
        for tool_call in message.get("tool_calls", []):
            if not isinstance(tool_call, Mapping):
                raise AdapterError("Chat tool call must be an object")
            function = tool_call.get("function")
            if not isinstance(function, Mapping):
                raise AdapterError("Chat tool call function is missing")
            output.append(
                {
                    "type": "function_call",
                    "call_id": tool_call.get("id"),
                    "name": function.get("name"),
                    "arguments": function.get("arguments", ""),
                }
            )
    if "usage" in payload:
        result["usage"] = copy.deepcopy(payload["usage"])
    result["output"] = output
    if warnings:
        result["compatibility_warnings"] = list(dict.fromkeys(warnings))
    return result


responses_to_responses = adapt_responses_to_responses
responses_to_chat = adapt_responses_to_chat
chat_response_to_responses = adapt_chat_response_to_responses
