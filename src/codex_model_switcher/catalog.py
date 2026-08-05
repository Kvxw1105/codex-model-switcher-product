"""Safe, explicit model catalog contracts.

The native picker contract is deliberately treated as evidence-driven.  This
module can validate a candidate catalog in an isolated home, but it does not
claim that an unverified candidate is accepted by a particular Codex build.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
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


@dataclass(frozen=True)
class ProviderReference:
    provider_id: str
    model: str
    wire_api: str
    credential_ref: str

    def __post_init__(self) -> None:
        for name in ("provider_id", "model", "credential_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise CatalogValidationError(f"{name} must be a non-empty string")
        if self.wire_api not in {"responses", "chat_completions"}:
            raise CatalogValidationError("wire_api must be responses or chat_completions")

    def to_mapping(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "wire_api": self.wire_api,
            "credential_ref": self.credential_ref,
        }


_RECEIPT_SOURCE = "current-client-artifact"
_REGISTRY_TEST_SOURCE = "registry-test"
_NATIVE_BASE_INSTRUCTIONS = (
    "You are a coding assistant routed through the local Codex Model Switcher Router."
)
_DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
_NATIVE_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credential_ref",
    "env_key",
    "experimental_bearer_token",
    "secret",
    "token",
}


@dataclass(frozen=True)
class CatalogDocument:
    schema_version: str | None
    client_version: str
    provider_id: str
    models: tuple[ModelRoute, ...]
    verification_status: str = "UNVERIFIED"
    providers: tuple[ProviderReference, ...] = ()

    def to_mapping(self) -> dict[str, object]:
        mapping: dict[str, object] = {
            "schema_version": self.schema_version,
            "client_version": self.client_version,
            "provider_id": self.provider_id,
            "models": [_route_to_record(route) for route in self.models],
        }
        if self.providers:
            mapping["providers"] = [provider.to_mapping() for provider in self.providers]
        return mapping

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
            return _is_registered_receipt(self)
        except (AttributeError, TypeError, ValueError):
            return False


@dataclass(frozen=True)
class _ReceiptRegistryEntry:
    receipt_ref: weakref.ReferenceType[PickerVerificationReceipt]
    schema_version: str
    client_version: str
    catalog_sha256: str
    source: str
    guard: object


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


def _make_receipt_registry():
    """Keep id-keyed registration state private to trusted module flows."""

    entries: dict[int, _ReceiptRegistryEntry] = {}
    guard = object()

    def register(receipt: PickerVerificationReceipt, guard_token: object) -> None:
        if guard_token is not guard:
            raise TypeError("receipt registration requires the internal guard")
        if type(receipt) is not PickerVerificationReceipt:
            raise TypeError("only an exact PickerVerificationReceipt can be registered")
        receipt_id = id(receipt)
        existing = entries.get(receipt_id)
        if existing is not None and existing.receipt_ref() is not None:
            raise ValueError("receipt identity is already registered")

        receipt_ref: weakref.ReferenceType[PickerVerificationReceipt]

        def remove_entry(reference: weakref.ReferenceType[PickerVerificationReceipt]) -> None:
            current = entries.get(receipt_id)
            if current is not None and current.receipt_ref is reference:
                entries.pop(receipt_id, None)

        receipt_ref = weakref.ref(receipt, remove_entry)
        entries[receipt_id] = _ReceiptRegistryEntry(
            receipt_ref=receipt_ref,
            schema_version=receipt.schema_version,
            client_version=receipt.client_version,
            catalog_sha256=receipt.catalog_sha256,
            source=receipt.source,
            guard=guard,
        )

    def register_test_receipt(receipt: PickerVerificationReceipt) -> None:
        if receipt.source != _REGISTRY_TEST_SOURCE:
            raise ValueError("registry test receipts cannot impersonate client evidence")
        register(receipt, guard)

    def is_registered(receipt: PickerVerificationReceipt) -> bool:
        entry = entries.get(id(receipt))
        if entry is None or entry.receipt_ref() is not receipt:
            return False
        return (
            entry.guard is guard
            and entry.schema_version == receipt.schema_version
            and entry.client_version == receipt.client_version
            and entry.catalog_sha256 == receipt.catalog_sha256
            and entry.source == receipt.source
        )

    return register_test_receipt, is_registered


_register_test_receipt, _is_registered_receipt = _make_receipt_registry()


def _register_receipt_for_registry_test(
    *,
    schema_version: str,
    client_version: str,
    catalog_sha256: str,
) -> PickerVerificationReceipt:
    """Create only a registry-contract object; never use it as picker evidence."""

    _validate_receipt_fields(schema_version, client_version, catalog_sha256)
    receipt = object.__new__(PickerVerificationReceipt)
    object.__setattr__(receipt, "schema_version", schema_version)
    object.__setattr__(receipt, "client_version", client_version)
    object.__setattr__(receipt, "catalog_sha256", catalog_sha256)
    object.__setattr__(receipt, "source", _REGISTRY_TEST_SOURCE)
    _register_test_receipt(receipt)
    return receipt


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
    provider_references: Sequence[ProviderReference | Mapping[str, object]] | None = None,
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
    providers = _normalize_provider_references(provider_references)
    if provider_references is None:
        providers = _derive_provider_references(route_list)
    mapping: dict[str, object] = {
        "schema_version": schema_version,
        "client_version": client_version,
        "provider_id": provider_id,
        "verification_status": "UNVERIFIED",
        "models": [_route_to_record(route) for route in route_list],
    }
    if providers:
        mapping["providers"] = [provider.to_mapping() for provider in providers]
    return mapping


def build_catalog_from_model_cache(
    cache_path: Path,
    routes: Sequence[ModelRoute],
    *,
    provider_id: str = "codex-model-switcher",
    schema_version: str | None = None,
    provider_references: Sequence[ProviderReference | Mapping[str, object]] | None = None,
) -> dict[str, object]:
    client_version = read_client_version_from_model_cache(cache_path)
    return build_catalog(
        routes,
        client_version=client_version,
        provider_id=provider_id,
        schema_version=schema_version,
        provider_references=provider_references,
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

    raw_providers = payload.get("providers", [])
    if not isinstance(raw_providers, list):
        raise CatalogValidationError("providers must be an array")
    providers = _normalize_provider_references(raw_providers)
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
        providers,
    )


def build_native_catalog(
    catalog: CatalogDocument | Mapping[str, object],
    *,
    bundled_catalog: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Adapt the internal route catalog to the native Codex model schema."""

    document = catalog if isinstance(catalog, CatalogDocument) else catalog_from_mapping(catalog)
    native_models: list[dict[str, object]] = []
    seen: set[str] = set()
    if bundled_catalog is not None:
        raw_models = bundled_catalog.get("models")
        if not isinstance(raw_models, list):
            raise CatalogValidationError("bundled native catalog models must be an array")
        for index, raw_model in enumerate(raw_models):
            if not isinstance(raw_model, Mapping):
                raise CatalogValidationError(f"bundled native models[{index}] must be an object")
            model = json.loads(json.dumps(raw_model, ensure_ascii=False))
            slug = model.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                raise CatalogValidationError(f"bundled native models[{index}] is missing slug")
            if slug in seen:
                raise CatalogValidationError(f"bundled native models repeat slug {slug}")
            _reject_sensitive_native_values(model)
            seen.add(slug)
            native_models.append(model)
    for route in document.models:
        if route.model_id in seen:
            continue
        native_models.append(_route_to_native_record(route))
        seen.add(route.model_id)
    if not native_models:
        raise CatalogValidationError("native catalog must contain at least one model")
    return {"models": native_models}


def write_native_catalog(
    catalog_path: Path,
    native_catalog_path: Path,
    *,
    bundled_catalog_path: Path | None = None,
) -> Path:
    document = load_catalog(Path(catalog_path))
    bundled = None
    if bundled_catalog_path is not None:
        try:
            raw = json.loads(Path(bundled_catalog_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CatalogValidationError("unable to read bundled native catalog") from error
        if not isinstance(raw, Mapping):
            raise CatalogValidationError("bundled native catalog must be a JSON object")
        bundled = raw
    native = build_native_catalog(document, bundled_catalog=bundled)
    destination = Path(native_catalog_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(native, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination


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
    if raw_catalog.lstrip().startswith(("{", "[")):
        return PickerContractResult(
            False,
            "model_catalog_json must be a JSON file path",
            evidence,
            provider_id,
            False,
        )
    native_path = Path(raw_catalog)
    if not native_path.is_absolute():
        native_path = config_path.parent / native_path
    try:
        configured_catalog = json.loads(native_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return PickerContractResult(
            False,
            "model_catalog_json path is not readable",
            evidence,
            provider_id,
            False,
        )
    if not isinstance(configured_catalog, Mapping) or not _native_matches_routes(
        configured_catalog, catalog.models
    ):
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


def _normalize_provider_references(
    values: Sequence[ProviderReference | Mapping[str, object]] | None,
) -> tuple[ProviderReference, ...]:
    if values is None:
        return ()
    providers: list[ProviderReference] = []
    seen: set[str] = set()
    for index, raw in enumerate(values):
        if isinstance(raw, ProviderReference):
            provider = raw
        elif isinstance(raw, Mapping):
            provider = ProviderReference(
                provider_id=_required_text(raw, "provider_id"),
                model=_required_text(raw, "model"),
                wire_api=_required_text(raw, "wire_api"),
                credential_ref=_required_text(raw, "credential_ref"),
            )
        else:
            raise CatalogValidationError(f"providers[{index}] must be an object")
        if provider.provider_id in seen:
            raise CatalogValidationError(f"providers repeat provider ID {provider.provider_id}")
        seen.add(provider.provider_id)
        providers.append(provider)
    return tuple(providers)


def _derive_provider_references(routes: Sequence[ModelRoute]) -> tuple[ProviderReference, ...]:
    deepseek_models = {
        route.upstream_model for route in routes if route.provider_id == "deepseek"
    }
    if len(deepseek_models) != 1:
        return ()
    return (
        ProviderReference(
            provider_id="deepseek",
            model=_DEFAULT_DEEPSEEK_MODEL,
            wire_api="responses",
            credential_ref="deepseek",
        ),
    )


def _route_to_native_record(route: ModelRoute) -> dict[str, object]:
    capability = route.capability
    return {
        "slug": route.model_id,
        "display_name": route.display_name,
        "description": f"{route.display_name} via the local Router",
        "default_reasoning_level": None,
        "supported_reasoning_levels": [],
        "shell_type": "disabled",
        "visibility": "list",
        "supported_in_api": capability.supports_responses,
        "priority": 0,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "base_instructions": _NATIVE_BASE_INSTRUCTIONS,
        "model_messages": None,
        "supports_reasoning_summaries": False,
        "default_reasoning_summary": "none",
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": None,
        "web_search_tool_type": "text",
        "truncation_policy": {"mode": "tokens", "limit": 10_000},
        "supports_parallel_tool_calls": False,
        "supports_image_detail_original": False,
        "context_window": capability.context_window,
        "max_context_window": capability.context_window,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": ["text"] + (["image"] if capability.supports_images else []),
        "supports_search_tool": False,
    }


def _reject_sensitive_native_values(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower() in _NATIVE_SENSITIVE_KEYS:
                raise CatalogValidationError("native catalog contains a sensitive field")
            _reject_sensitive_native_values(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_native_values(nested)


def _native_matches_routes(payload: Mapping[str, object], routes: Sequence[ModelRoute]) -> bool:
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return False
    by_slug = {
        model.get("slug"): model
        for model in raw_models
        if isinstance(model, Mapping) and isinstance(model.get("slug"), str)
    }
    return all(
        route.model_id in by_slug
        and by_slug[route.model_id].get("display_name") == route.display_name
        for route in routes
    )


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
    if (
        type(verification) is not PickerVerificationReceipt
        or not verification._is_authentic()
        or verification.source != _RECEIPT_SOURCE
    ):
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
