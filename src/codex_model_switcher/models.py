"""Immutable data contracts shared by the catalog and configuration modules."""

from dataclasses import dataclass
from typing import Literal

Lane = Literal["official", "third_party"]


def _require_non_empty_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if any(character.isspace() for character in value):
        raise ValueError(f"{name} must not contain whitespace")


def _require_non_empty_display_name(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("display_name must be a non-empty string")


@dataclass(frozen=True)
class ModelCapability:
    context_window: int
    supports_responses: bool
    supports_streaming: bool
    supports_tools: bool
    supports_images: bool
    supports_files: bool
    supports_compaction_context: bool

    def __post_init__(self) -> None:
        if isinstance(self.context_window, bool) or not isinstance(self.context_window, int):
            raise ValueError("context_window must be an integer")
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        for field_name in (
            "supports_responses",
            "supports_streaming",
            "supports_tools",
            "supports_images",
            "supports_files",
            "supports_compaction_context",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a boolean")


@dataclass(frozen=True)
class ModelRoute:
    model_id: str
    display_name: str
    lane: Lane
    provider_id: str
    upstream_model: str
    capability: ModelCapability

    def __post_init__(self) -> None:
        _require_non_empty_text("model_id", self.model_id)
        _require_non_empty_display_name(self.display_name)
        _require_non_empty_text("provider_id", self.provider_id)
        _require_non_empty_text("upstream_model", self.upstream_model)
        if self.lane not in ("official", "third_party"):
            raise ValueError("lane must be 'official' or 'third_party'")
        if not isinstance(self.capability, ModelCapability):
            raise ValueError("capability is required and must be a ModelCapability")
        marker = "Official" if self.lane == "official" else "API"
        if marker not in self.display_name:
            raise ValueError(f"display_name must include the {marker} lane marker")
