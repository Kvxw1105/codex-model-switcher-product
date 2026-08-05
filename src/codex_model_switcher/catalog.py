"""Safe, explicit model catalog contracts.

The native picker contract is deliberately treated as evidence-driven.  This
module can validate a candidate catalog in an isolated home, but it does not
claim that an unverified candidate is accepted by a particular Codex build.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .capabilities import CapabilityValidationError, parse_capability
from .models import ModelRoute


class CatalogValidationError(ValueError):
    """Raised when a catalog does not contain a complete route contract."""


@dataclass(frozen=True)
class CatalogDocument:
    schema_version: str
    client_version: str
    provider_id: str
    models: tuple[ModelRoute, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "client_version": self.client_version,
            "provider_id": self.provider_id,
            "models": [_route_to_record(route) for route in self.models],
        }


@dataclass(frozen=True)
class PickerSchemaEvidence:
    """Non-sensitive evidence supplied by a current-client verification."""

    schema_version: str
    client_version: str
    verified_by_current_client: bool

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, str) or not self.schema_version.strip():
            raise ValueError("schema_version must be a non-empty string")
        if not isinstance(self.client_version, str) or not self.client_version.strip():
            raise ValueError("client_version must be a non-empty string")
        if type(self.verified_by_current_client) is not bool:
            raise ValueError("verified_by_current_client must be a boolean")


@dataclass(frozen=True)
class PickerContractResult:
    passed: bool
    reason: str
    evidence: PickerSchemaEvidence
    provider_id: str

    def to_safe_dict(self) -> dict[str, object]:
        """Return only schema/version/provider evidence; never paths or secrets."""

        return {
            "passed": self.passed,
            "schema_version": self.evidence.schema_version,
            "client_version": self.evidence.client_version,
            "provider_id": self.provider_id,
        }


def read_client_version_from_model_cache(cache_path: Path) -> str:
    """Read the explicit client version from a supplied model-cache fixture."""

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CatalogValidationError(f"unable to read model cache: {cache_path.name}") from error
    if not isinstance(payload, Mapping):
        raise CatalogValidationError("model cache must be a JSON object")
    client_version = payload.get("client_version")
    if not isinstance(client_version, str) or not client_version.strip():
        raise CatalogValidationError("model cache is missing client_version")
    return client_version


def build_catalog(
    routes: Sequence[ModelRoute],
    *,
    client_version: str,
    provider_id: str = "codex-model-switcher",
    schema_version: str = "picker-v1",
) -> dict[str, object]:
    if not isinstance(client_version, str) or not client_version.strip():
        raise CatalogValidationError("client_version must be a non-empty string")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise CatalogValidationError("provider_id must be a non-empty string")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise CatalogValidationError("schema_version must be a non-empty string")
    route_list = tuple(routes)
    if any(not isinstance(route, ModelRoute) for route in route_list):
        raise CatalogValidationError("models must contain ModelRoute values")
    model_ids = [route.model_id for route in route_list]
    if len(model_ids) != len(set(model_ids)):
        raise CatalogValidationError("model IDs must be unique")
    return {
        "schema_version": schema_version,
        "client_version": client_version,
        "provider_id": provider_id,
        "models": [_route_to_record(route) for route in route_list],
    }


def build_catalog_from_model_cache(
    cache_path: Path,
    routes: Sequence[ModelRoute],
    *,
    provider_id: str = "codex-model-switcher",
    schema_version: str = "picker-v1",
) -> dict[str, object]:
    client_version = read_client_version_from_model_cache(cache_path)
    return build_catalog(
        routes,
        client_version=client_version,
        provider_id=provider_id,
        schema_version=schema_version,
    )


def load_catalog(catalog_path: Path) -> CatalogDocument:
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        message = f"unable to read catalog fixture: {catalog_path.name}"
        raise CatalogValidationError(message) from error
    return catalog_from_mapping(payload)


def catalog_from_mapping(payload: Mapping[str, object]) -> CatalogDocument:
    if not isinstance(payload, Mapping):
        raise CatalogValidationError("catalog must be a JSON object")
    schema_version = _required_text(payload, "schema_version")
    client_version = _required_text(payload, "client_version")
    provider_id = _required_text(payload, "provider_id")
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise CatalogValidationError("models must be an array")

    routes: list[ModelRoute] = []
    seen_ids: set[str] = set()
    for index, raw_route in enumerate(raw_models):
        try:
            route = _route_from_record(raw_route)
        except (CatalogValidationError, CapabilityValidationError, ValueError) as error:
            raise CatalogValidationError(f"models[{index}] is invalid: {error}") from error
        if route.model_id in seen_ids:
            raise CatalogValidationError(f"models[{index}] repeats model ID {route.model_id}")
        seen_ids.add(route.model_id)
        routes.append(route)
    return CatalogDocument(schema_version, client_version, provider_id, tuple(routes))


def verify_isolated_picker_contract(
    codex_home: Path,
    catalog_path: Path,
    *,
    evidence: PickerSchemaEvidence | None = None,
) -> PickerContractResult:
    """Validate only an isolated candidate config and explicit client evidence.

    A syntactically valid local config is not enough to establish native picker
    compatibility.  The result remains failed until the caller supplies
    non-sensitive evidence from the current client.
    """

    catalog = load_catalog(catalog_path)
    if evidence is None:
        evidence = PickerSchemaEvidence(
            schema_version=catalog.schema_version,
            client_version=catalog.client_version,
            verified_by_current_client=False,
        )
    config_path = codex_home / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return PickerContractResult(
            False,
            "isolated config is not parseable",
            evidence,
            catalog.provider_id,
        )

    provider_id = config.get("model_provider")
    raw_catalog = config.get("model_catalog_json")
    if provider_id != catalog.provider_id or not isinstance(raw_catalog, str):
        return PickerContractResult(
            False,
            "isolated config must contain matching provider and model_catalog_json",
            evidence,
            catalog.provider_id,
        )
    try:
        configured_catalog = json.loads(raw_catalog)
    except json.JSONDecodeError:
        return PickerContractResult(
            False,
            "model_catalog_json is not valid JSON",
            evidence,
            provider_id,
        )
    if configured_catalog != catalog.to_mapping():
        return PickerContractResult(
            False,
            "model_catalog_json does not match the candidate catalog",
            evidence,
            provider_id,
        )
    if not evidence.verified_by_current_client:
        return PickerContractResult(
            False,
            "current client picker schema evidence is unavailable",
            evidence,
            provider_id,
        )
    if (
        evidence.schema_version != catalog.schema_version
        or evidence.client_version != catalog.client_version
    ):
        return PickerContractResult(
            False,
            "client evidence does not match the candidate catalog",
            evidence,
            provider_id,
        )
    return PickerContractResult(
        True,
        "candidate matches supplied current-client picker evidence",
        evidence,
        provider_id,
    )


def _required_text(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise CatalogValidationError(f"{field_name} must be a non-empty string")
    return value


def _route_from_record(raw_route: object) -> ModelRoute:
    if not isinstance(raw_route, Mapping):
        raise CatalogValidationError("route must be an object")
    model_id = raw_route.get("id", raw_route.get("model_id"))
    display_name = raw_route.get("display_name")
    lane = raw_route.get("lane")
    provider_id = raw_route.get("provider_id")
    upstream_model = raw_route.get("upstream_model")
    identity_fields = (model_id, display_name, lane, provider_id, upstream_model)
    if not all(isinstance(value, str) and value.strip() for value in identity_fields):
        raise CatalogValidationError("route identity fields are incomplete")
    raw_capability = raw_route.get("capability")
    if raw_capability is None:
        raise CatalogValidationError("capability is required; incomplete routes cannot be enabled")
    return ModelRoute(
        model_id=model_id,
        display_name=display_name,
        lane=lane,
        provider_id=provider_id,
        upstream_model=upstream_model,
        capability=parse_capability(raw_capability),
    )


def _route_to_record(route: ModelRoute) -> dict[str, object]:
    return {
        "id": route.model_id,
        "display_name": route.display_name,
        "lane": route.lane,
        "provider_id": route.provider_id,
        "upstream_model": route.upstream_model,
        "capability": asdict(route.capability),
    }
