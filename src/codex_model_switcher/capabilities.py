"""Parsing and validation for explicit model capability declarations."""

from collections.abc import Mapping

from .models import ModelCapability


class CapabilityValidationError(ValueError):
    """Raised when a route does not declare every capability explicitly."""


CAPABILITY_FIELDS = (
    "context_window",
    "supports_responses",
    "supports_streaming",
    "supports_tools",
    "supports_images",
    "supports_files",
    "supports_compaction_context",
)


def parse_capability(value: Mapping[str, object]) -> ModelCapability:
    if not isinstance(value, Mapping):
        raise CapabilityValidationError("capability must be an object")
    missing = [field for field in CAPABILITY_FIELDS if field not in value]
    if missing:
        missing_fields = ", ".join(missing)
        raise CapabilityValidationError(f"capability is missing: {missing_fields}")
    try:
        return ModelCapability(**{field: value[field] for field in CAPABILITY_FIELDS})
    except (TypeError, ValueError) as error:
        raise CapabilityValidationError(str(error)) from error
