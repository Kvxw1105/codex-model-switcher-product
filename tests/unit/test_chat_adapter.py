from __future__ import annotations

import pytest

from codex_model_switcher.adapters.chat import (
    UnsupportedItemError,
    adapt_chat_to_responses,
)


def test_chat_to_responses_preserves_supported_message_and_tool_semantics() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": "be concise"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,ZmFrZS1pbWFnZQ=="
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-fixture",
                "content": "tool result",
            },
        ],
        "max_tokens": 12,
    }

    adapted = adapt_chat_to_responses(payload, model="responses-model")

    assert adapted["model"] == "responses-model"
    assert adapted["input"][0]["role"] == "system"
    assert adapted["input"][1]["content"][1]["type"] == "input_image"
    assert adapted["input"][2] == {
        "type": "function_call_output",
        "call_id": "call-fixture",
        "output": "tool result",
    }
    assert adapted["max_output_tokens"] == 12


def test_chat_to_responses_rejects_unverified_image_format() -> None:
    with pytest.raises(UnsupportedItemError) as caught:
        adapt_chat_to_responses(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:text/plain;base64,eA=="},
                            }
                        ],
                    }
                ]
            },
            model="responses-model",
        )

    assert caught.value.to_dict()["error"]["unsupported_item_types"] == ["image_url"]
