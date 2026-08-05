"""Safe, explicit model catalog contracts.

The native picker contract is deliberately treated as evidence-driven.  This
module can validate a candidate catalog in an isolated home, but it does not
claim that an unverified candidate is accepted by a particular Codex build.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
import weakref
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .capabilities import CapabilityValidationError, parse_capability
from .models import ModelRoute


class CatalogValidationError(ValueError):
    """Raised when a catalog does not contain a complete route contract."""


_RECEIPT_SOURCE = "current-client-artifact"


@dataclass(frozen=True)
class CatalogDocument:
    schema_version: str | None
    client_version: str
    provider_id: str
    models: tuple[ModelRoute, ...]
    verification_status: str = "UNVERIFIED"

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "client_version": self.client_version,
            "provider_id": self.provider_id,
            "models": [_route_to_record(route) for route in self.models],
        }

    def to_candidate_mapping(self) -> dict[str, object]:
        mapping = self.to_mapping()
        mapping["verification_status"] = self.verification_status
        return mapping


@dataclass(frozen=True)
class PickerSchemaEvidence:
    """Non-sensitive evidence about a candidate, never native-client proof."""

    schema_version: str | None
    client_version: str
    source: str

    def __post_init__(self) -> None:
        if self.schema_version is not None and (
            not isinstance(self.schema_version, str) or not self.schema_version.strip()
        ):
            raise ValueError("schema_version must be a non-empty string or None")
        if not isinstance(self.client_version, str) or not self.client_version.strip():
            raise ValueError("client_version must be a non-empty string")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string")


@dataclass(frozen=True, init=False)
class PickerVerificationReceipt:
    """An opaque writer capability registered by an internal trusted flow."""

    schema_version: str
    client_version: str
    catalog_sha256: str
    source: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("PickerVerificationReceipt is verifier-issued")

    def _is_authentic(self) -> bool:
        if type(self) is not PickerVerificationReceipt:
            return False
        try:
            _validate_receipt_fields(
                self.schema_version,
                self.client_version,
                self.catalog_sha256,
            )
            return (
                self.source == _RECEIPT_SOURCE
                and _is_registered_receipt(self)
            )
        except (AttributeError, TypeError, ValueError):
            return False


class TrustedPickerVerifier(ABC):
    """Future external-verifier boundary for reading client evidence.

    The package intentionally ships no concrete implementation and this
    interface deliberately has no receipt factory.  A real adapter must obtain
    non-sensitive evidence from the current client across its trusted boundary;
    callers cannot inject an evidence callback or obtain a writer capability
    from a subclass.
    """

    @abstractmethod
    def _read_current_client_evidence(
        self,
        catalog: CatalogDocument,
    ) -> PickerSchemaEvidence:
        """Read evidence from a trusted external client adapter."""
        raise NotImplementedError


def _make_receipt_registry_checker():
    """Keep the registration set private; this build has no issuing flow."""

    registered: weakref.WeakSet[PickerVerificationReceipt] = weakref.WeakSet()

    def is_registered(receipt: PickerVerificationReceipt) -> bool:
        return receipt in registered

    return is_registered


_is_registered_receipt = _make_receipt_registry_checker()


def _validate_receipt_fields(
    schema_version: str,
    client_version: str,
    catalog_sha256: str,
) -> None:
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise ValueError("schema_version must be a non-empty string")
    if not isinstance(client_version, str) or not client_version.strip():
        raise ValueError("client_version must be a non-empty string")
    if not isinstance(catalog_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", catalog_sha256
    ):
        raise ValueError("catalog_sha256 must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True)
class PickerContractResult:
    passed: bool
    reason: str
    evidence: PickerSchemaEvidence
    provider_id: str
    candidate_matches: bool
    status: str = "UNVERIFIED"

    def __post_init__(self) -> None:
        if self.passed:
            raise ValueError("native picker result must remain UNVERIFIED")
        if self.status != "UNVERIFIED":
            raise ValueError("native picker result status must remain UNVERIFIED")
        if type(self.candidate_matches) is not bool:
            raise ValueError("candidate_matches must be a boolean")

    def to_safe_dict(self) -> dict[str, object]:
        """Return only schema/version/provider evidence; never paths or secrets."""

        return {
            "passed": False,
            "status": self.status,
            "candidate_matches": self.candidate_matches,
            "schema_version": self.evidence.schema_version,
            "client_version": self.evidence.client_version,
            "provider_id": self.provider_id,
        }


def read_client_version_from_model_cache(cache_path: Path) -> str:
    """Read the explicit client version from a supplied model-cache fixture."""

    try:
        cache_path = Path(cache_path)
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
    schema_version: str | None = None,
) -> dict[str, object]:
    if not isinstance(client_version, str) or not client_version.strip():
        raise CatalogValidationError("client_version must be a non-empty string")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise CatalogValidationError("provider_id must be a non-empty string")
    if schema_version is not None and (
        not isinstance(schema_version, str) or not schema_version.strip()
    ):
        raise CatalogValidationError("schema_version must be a non-empty string or None")
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
        "verification_status": "UNVERIFIED",
        "models": [_route_to_record(route) for route in route_list],
    }


def build_catalog_from_model_cache(
    cache_path: Path,
    routes: Sequence[ModelRoute],
    *,
    provider_id: str = "codex-model-switcher",
    schema_version: str | None = None,
) -> dict[str, object]:
    client_version = read_client_version_from_model_cache(cache_path)
    return build_catalog(
        routes,
        client_version=client_version,
        provider_id=provider_id,
        schema_version=schema_version,
    )


def load_catalog(catalog_path: Path) -> CatalogDocument:
    catalog_path = Path(catalog_path)
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        message = f"unable to read catalog fixture: {catalog_path.name}"
        raise CatalogValidationError(message) from error
    return catalog_from_mapping(payload)


def catalog_from_mapping(payload: Mapping[str, object]) -> CatalogDocument:
    if not isinstance(payload, Mapping):
        raise CatalogValidationError("catalog must be a JSON object")
    schema_version = _optional_text(payload, "schema_version")
    client_version = _required_text(payload, "client_version")
    provider_id = _required_text(payload, "provider_id")
    verification_status = payload.get("verification_status", "UNVERIFIED")
    if verification_status != "UNVERIFIED":
        raise CatalogValidationError("catalog verification_status must remain UNVERIFIED")
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
    return CatalogDocument(
        schema_version,
        client_version,
        provider_id,
        tuple(routes),
        verification_status,
    )


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
            source="unverified-candidate",
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
            False,
        )

    provider_id = config.get("model_provider")
    raw_catalog = config.get("model_catalog_json")
    if provider_id != catalog.provider_id or not isinstance(raw_catalog, str):
        return PickerContractResult(
            False,
            "isolated config must contain matching provider and model_catalog_json",
            evidence,
            catalog.provider_id,
            False,
        )
    try:
        configured_catalog = json.loads(raw_catalog)
    except json.JSONDecodeError:
        return PickerContractResult(
            False,
            "model_catalog_json is not valid JSON",
            evidence,
            provider_id,
            False,
        )
    if configured_catalog != catalog.to_mapping():
        return PickerContractResult(
            False,
            "model_catalog_json does not match the candidate catalog",
            evidence,
            provider_id,
            False,
        )
    return PickerContractResult(
        False,
        (
            "candidate matches in isolation; current-client evidence unavailable; "
            "native picker remains UNVERIFIED"
        ),
        evidence,
        provider_id,
        True,
    )


def _required_text(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise CatalogValidationError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(payload: Mapping[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CatalogValidationError(f"{field_name} must be a non-empty string or None")
    return value


def catalog_fingerprint(catalog: CatalogDocument) -> str:
    canonical = json.dumps(
        catalog.to_mapping(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_picker_verification(
    catalog: CatalogDocument,
    verification: PickerVerificationReceipt | None,
) -> None:
    if verification is None:
        raise CatalogValidationError(
            "cannot apply an UNVERIFIED catalog without current-client-artifact evidence"
        )
    if type(verification) is not PickerVerificationReceipt or not verification._is_authentic():
        raise CatalogValidationError("verification must be an authentic verifier-issued receipt")
    if catalog.schema_version is None:
        raise CatalogValidationError(
            "cannot apply an UNVERIFIED catalog without an evidenced schema_version"
        )
    if verification.schema_version != catalog.schema_version:
        raise CatalogValidationError("verification schema_version does not match the catalog")
    if verification.client_version != catalog.client_version:
        raise CatalogValidationError("verification client_version does not match the catalog")
    if verification.catalog_sha256 != catalog_fingerprint(catalog):
        raise CatalogValidationError("verification catalog hash does not match the catalog")


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
