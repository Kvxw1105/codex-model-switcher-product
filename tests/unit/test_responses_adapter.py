from __future__ import annotations

import copy

import pytest

from codex_model_switcher.adapters.responses import (
    UnsupportedItemError,
    adapt_chat_response_to_responses,
    adapt_responses_to_chat,
    adapt_responses_to_responses,
)


def test_responses_to_responses_preserves_unknown_items_and_fields() -> None:
    payload = {
        "model": "catalog-model",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "future_item"}]},
            {"type": "future_top_level", "opaque": {"value": 3}},
        ],
        "future_field": {"keep": [1, 2, 3]},
    }
    original = copy.deepcopy(payload)

    adapted = adapt_responses_to_responses(payload, model="upstream-model")

    assert adapted["model"] == "upstream-model"
    assert adapted["input"] == original["input"]
    assert adapted["future_field"] == original["future_field"]
    assert payload == original


def test_responses_to_chat_rejects_unsupported_items_with_structured_types() -> None:
    payload = {
        "model": "catalog-model",
        "input": [
            {"type": "input_file", "file_id": "file-fixture"},
            {"type": "web_search_call", "id": "search-fixture"},
            {"type": "reasoning", "summary": []},
        ],
    }

    with pytest.raises(UnsupportedItemError) as caught:
        adapt_responses_to_chat(payload, model="chat-model")

    error = caught.value
    assert error.status_code == 422
    assert error.to_dict()["error"]["unsupported_item_types"] == [
        "input_file",
        "web_search_call",
        "reasoning",
    ]


def test_responses_to_chat_maps_text_verified_image_and_function_items() -> None:
    payload = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "describe this"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,ZmFrZS1pbWFnZQ==",
                    },
                ],
            },
            {
                "type": "function_call",
                "call_id": "call-fixture",
                "name": "lookup",
                "arguments": "{\"q\":\"fixture\"}",
            },
            {
                "type": "function_call_output",
                "call_id": "call-fixture",
                "output": "result",
            },
        ]
    }

    adapted = adapt_responses_to_chat(payload, model="chat-model")

    assert adapted["model"] == "chat-model"
    assert adapted["messages"][0] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,ZmFrZS1pbWFnZQ=="},
            },
        ],
    }
    assert adapted["messages"][1]["tool_calls"][0]["function"]["name"] == "lookup"
    assert adapted["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call-fixture",
        "content": "result",
    }


def test_chat_response_missing_fields_emits_warnings_without_fabrication() -> None:
    adapted = adapt_chat_response_to_responses(
        {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
    )

    assert "id" not in adapted
    assert "usage" not in adapted
    assert "finish_reason" not in adapted["output"][0]
    assert adapted["compatibility_warnings"] == [
        "missing_response_id",
        "missing_usage",
        "missing_finish_reason",
    ]
