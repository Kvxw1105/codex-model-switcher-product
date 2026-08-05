from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from codex_model_switcher.capabilities import CapabilityValidationError, parse_capability
from codex_model_switcher.models import ModelCapability, ModelRoute


def _capability_record() -> dict[str, object]:
    return {
        "context_window": 32_000,
        "supports_responses": True,
        "supports_streaming": True,
        "supports_tools": True,
        "supports_images": False,
        "supports_files": True,
        "supports_compaction_context": False,
    }


def test_capability_and_route_are_frozen_dataclasses() -> None:
    capability = ModelCapability(**_capability_record())
    route = ModelRoute(
        model_id="cms-example-chat",
        display_name="Example API",
        lane="third_party",
        provider_id="example-provider",
        upstream_model="example-chat",
        capability=capability,
    )

    assert is_dataclass(capability)
    assert is_dataclass(route)
    with pytest.raises(FrozenInstanceError):
        capability.context_window = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        route.model_id = "changed"  # type: ignore[misc]


def test_parse_capability_rejects_missing_fields_instead_of_defaulting() -> None:
    record = _capability_record()
    del record["context_window"]

    with pytest.raises(CapabilityValidationError, match="context_window"):
        parse_capability(record)


def test_route_display_name_identifies_its_lane() -> None:
    capability = ModelCapability(**_capability_record())

    with pytest.raises(ValueError, match="Official"):
        ModelRoute(
            model_id="cms-example-official",
            display_name="Example model",
            lane="official",
            provider_id="official-provider",
            upstream_model="example-model",
            capability=capability,
        )
