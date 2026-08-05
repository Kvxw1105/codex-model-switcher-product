"""Credential isolation and secret-safe observability primitives.

This module deliberately has no dependency on the Router or GUI.  Callers can
inject a ``CredentialStore`` and use the pure serialization/redaction helpers
until those layers are integrated.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

SERVICE_NAME = "CodexModelSwitcher"
REDACTED = "[REDACTED]"
REDACTED_URL = "[REDACTED_URL]"

_PROVIDER_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,63})$")
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_LABELED_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"authorization|proxy-authorization|token|secret|password)\s*[:=]\s*)"
    r"(?:bearer\s+)?[^\s,;]+"
)
_KEY_SECRET_RE = re.compile(r"(?i)^sk-[a-z0-9_-]{8,}$")

_SENSITIVE_KEYS = {
    "access_token",
    "account_email",
    "api_key",
    "apikey",
    "api_token",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "email",
    "id_token",
    "password",
    "proxy_authorization",
    "refresh_token",
    "secret",
    "set_cookie",
    "token",
}
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_password",
    "_secret",
    "_token",
)
_URL_KEYS = {
    "base_url",
    "endpoint",
    "host",
    "location",
    "origin",
    "redirect_uri",
    "referer",
    "url",
    "uri",
}
_PRIVATE_CONTENT_KEYS = {
    "attachment",
    "attachments",
    "body",
    "file",
    "file_content",
    "file_contents",
    "files",
    "prompt",
}


class CredentialError(Exception):
    """Base class for safe, non-secret credential errors."""


class CredentialStoreError(CredentialError):
    """Raised when the operating-system credential backend fails."""


class CredentialNotConfiguredError(CredentialError):
    """Raised when a provider has no configured credential."""


class CredentialValueError(CredentialError):
    """Raised when a credential value is not acceptable."""


class CredentialMigrationError(CredentialError):
    """Raised when an explicit legacy-catalog migration cannot complete."""


class ProviderIdError(CredentialError):
    """Raised when a provider ID cannot be used as a credential username."""


@runtime_checkable
class CredentialStore(Protocol):
    """Replaceable provider credential storage contract."""

    def set(self, provider_id: str, secret: str) -> None:
        ...

    def get(self, provider_id: str) -> str:
        ...

    def delete(self, provider_id: str) -> None:
        ...

    def exists(self, provider_id: str) -> bool:
        ...


CredentialStoreProtocol = CredentialStore


def validate_provider_id(provider_id: str) -> str:
    """Validate the safe identifier used as a Credential Manager username."""

    if not isinstance(provider_id, str) or _PROVIDER_ID_RE.fullmatch(provider_id) is None:
        raise ProviderIdError("provider ID is not valid")
    return provider_id


def credential_ref(provider_id: str) -> str:
    """Return the non-secret catalog reference for a provider credential."""

    return validate_provider_id(provider_id)


class KeyringCredentialStore:
    """CredentialStore backed by keyring's Windows Credential Manager backend."""

    SERVICE_NAME = SERVICE_NAME

    def __init__(self, keyring_module: Any | None = None) -> None:
        if keyring_module is None:
            try:
                import keyring as keyring_module
            except ImportError:
                raise CredentialStoreError("keyring package is required") from None
        self._keyring = keyring_module

    def set(self, provider_id: str, secret: str) -> None:
        username = validate_provider_id(provider_id)
        _validate_secret(secret)
        try:
            self._keyring.set_password(self.SERVICE_NAME, username, secret)
        except Exception:
            raise CredentialStoreError("credential backend write failed") from None

    def get(self, provider_id: str) -> str:
        username = validate_provider_id(provider_id)
        try:
            value = self._keyring.get_password(self.SERVICE_NAME, username)
        except Exception:
            raise CredentialStoreError("credential backend read failed") from None
        if value is None:
            raise CredentialNotConfiguredError("provider credential is not configured")
        if not isinstance(value, str):
            raise CredentialStoreError("credential backend returned an invalid value")
        return value

    def delete(self, provider_id: str) -> None:
        username = validate_provider_id(provider_id)
        try:
            self._keyring.delete_password(self.SERVICE_NAME, username)
        except Exception as error:
            if type(error).__name__ == "PasswordDeleteError":
                return
            raise CredentialStoreError("credential backend delete failed") from None

    def exists(self, provider_id: str) -> bool:
        username = validate_provider_id(provider_id)
        try:
            return self._keyring.get_password(self.SERVICE_NAME, username) is not None
        except Exception:
            raise CredentialStoreError("credential backend existence check failed") from None


WindowsCredentialStore = KeyringCredentialStore


def configure_credential(
    credential_store: CredentialStore,
    provider_id: str,
    secret: str,
) -> dict[str, bool]:
    """Write a credential and expose only a boolean GUI/call-layer result."""

    try:
        validate_provider_id(provider_id)
        _validate_secret(secret)
        credential_store.set(provider_id, secret)
        configured = bool(credential_store.exists(provider_id))
    except Exception:
        configured = False
    return {"configured": configured}


set_provider_credential = configure_credential
write_credential = configure_credential


def resolve_upstream_auth(
    *,
    lane: str,
    provider_id: str,
    inbound_authorization: str | None,
    credential_store: CredentialStore,
) -> str | None:
    """Resolve an upstream auth header while keeping official and third-party lanes separate."""

    if lane == "third_party":
        validate_provider_id(provider_id)
        try:
            secret = credential_store.get(provider_id)
        except KeyError:
            raise CredentialNotConfiguredError("provider credential is not configured") from None
        if not isinstance(secret, str) or not secret:
            raise CredentialNotConfiguredError("provider credential is not configured")
        return f"Bearer {secret}"
    if lane == "official":
        return inbound_authorization
    raise ValueError("lane must be official or third_party")


def _validate_secret(secret: str) -> None:
    if not isinstance(secret, str) or not secret:
        raise CredentialValueError("credential must be non-empty text")


def _normal_key(key: object) -> str:
    return str(key).strip().lower().replace("-", "_")


def _is_sensitive_key(key: object) -> bool:
    normalized = _normal_key(key)
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)
        or normalized.startswith("authorization_")
    )


def _is_url_key(key: object) -> bool:
    return _normal_key(key) in _URL_KEYS


def _is_private_content_key(key: object) -> bool:
    return _normal_key(key) in _PRIVATE_CONTENT_KEYS


def _redact_text(value: str) -> str:
    value = _BEARER_RE.sub(r"\1" + REDACTED, value)
    value = _LABELED_SECRET_RE.sub(lambda match: match.group(1) + REDACTED, value)
    if _KEY_SECRET_RE.fullmatch(value):
        return REDACTED
    return value


def redact_sensitive(value: Any, *, key: object | None = None) -> Any:
    """Recursively redact credentials, URLs, and private content from data."""

    if _is_sensitive_key(key) or _is_private_content_key(key):
        return REDACTED
    if _is_url_key(key):
        return REDACTED_URL
    if isinstance(value, Mapping):
        return {
            item_key: redact_sensitive(item_value, key=item_key)
            for item_key, item_value in value.items()
            if not _is_sensitive_key(item_key)
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        return _redact_text(value)
    return value


def redact_headers(headers: Mapping[str, Any]) -> dict[Any, Any]:
    return redact_sensitive(headers)


def redact_query(query: Mapping[str, Any]) -> dict[Any, Any]:
    return redact_sensitive(query)


def redact_json(payload: Any) -> Any:
    return redact_sensitive(payload)


def redact_exception(error: BaseException) -> dict[str, str]:
    """Return a minimal exception summary without message, URL, or attributes."""

    error_type = type(error).__name__
    safe_type = re.sub(r"[^A-Za-z0-9_.-]", "_", error_type)[:80] or "Exception"
    return {"type": safe_type, "message": REDACTED}


def redact_sse_fragment(fragment: str) -> str:
    """Redact SSE headers and JSON data while retaining event framing."""

    if not isinstance(fragment, str):
        return REDACTED
    output: list[str] = []
    for line in fragment.splitlines(keepends=True):
        line_body = line.rstrip("\r\n")
        line_ending = line[len(line_body) :]
        if line_body.lower().startswith("data:"):
            prefix, data = line_body.split(":", 1)
            data = data.lstrip()
            try:
                safe_data = json.dumps(redact_sensitive(json.loads(data)), separators=(",", ":"))
            except (json.JSONDecodeError, TypeError):
                safe_data = REDACTED
            output.append(f"{prefix}: {safe_data}{line_ending}")
        else:
            output.append(_redact_text(line_body) + line_ending)
    return "".join(output)


def summarize_subprocess_env(environment: Mapping[str, Any]) -> dict[str, list[str]]:
    """Return environment names only; never expose child-process values."""

    keys = sorted(str(key) for key in environment)
    sensitive_keys = sorted(key for key in keys if _is_sensitive_key(key))
    return {"keys": keys, "sensitive_keys": sensitive_keys}


_SAFE_LOG_FIELDS = ("route_id", "status_code", "elapsed_ms", "byte_count", "trace_id")


def build_safe_log_record(
    fields: Mapping[str, Any] | None = None,
    **extra_fields: Any,
) -> dict[str, Any]:
    """Keep only the small, non-content telemetry contract used by default logs."""

    combined: dict[str, Any] = {}
    if fields is not None:
        combined.update(fields)
    combined.update(extra_fields)
    return {field: combined[field] for field in _SAFE_LOG_FIELDS if field in combined}


safe_log_fields = build_safe_log_record


def _sanitize_catalog_value(value: Any, *, key: object | None = None) -> Any:
    if _is_sensitive_key(key):
        return None
    if isinstance(value, Mapping):
        return {
            item_key: sanitized
            for item_key, item_value in value.items()
            if not _is_sensitive_key(item_key)
            for sanitized in [_sanitize_catalog_value(item_value, key=item_key)]
        }
    if isinstance(value, list):
        return [_sanitize_catalog_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_catalog_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def serialize_provider_record(
    provider: Mapping[str, Any],
    *,
    provider_id: str | None = None,
) -> dict[str, Any]:
    """Serialize provider metadata with a credential reference, never a secret."""

    if not isinstance(provider, Mapping):
        raise TypeError("provider must be a mapping")
    candidate = provider_id or provider.get("provider_id") or provider.get("id")
    if candidate is None:
        candidate = provider.get("credential_ref")
    reference = credential_ref(candidate)  # type: ignore[arg-type]
    result = _sanitize_catalog_value(provider)
    if not isinstance(result, dict):
        raise TypeError("provider must serialize to an object")
    for item_key in list(result):
        if isinstance(result[item_key], dict) and not result[item_key]:
            del result[item_key]
    result["credential_ref"] = reference
    return result


sanitize_provider_record = serialize_provider_record
serialize_provider = serialize_provider_record


def serialize_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize a catalog while replacing provider secrets with credential refs."""

    if not isinstance(catalog, Mapping):
        raise TypeError("catalog must be a mapping")
    result = _sanitize_catalog_value(catalog)
    if not isinstance(result, dict):
        raise TypeError("catalog must serialize to an object")
    providers = catalog.get("providers")
    if isinstance(providers, list):
        result["providers"] = [serialize_provider_record(provider) for provider in providers]
    return result


sanitize_catalog = serialize_catalog


def _load_catalog(source: Mapping[str, Any] | str | Path) -> tuple[dict[str, Any], Path | None]:
    if isinstance(source, Mapping):
        return dict(source), None
    source_path = Path(source)
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise CredentialMigrationError("could not read legacy catalog") from None
    if not isinstance(payload, dict):
        raise CredentialMigrationError("legacy catalog must be a JSON object")
    return payload, source_path


def _provider_id_from_record(provider: Mapping[str, Any]) -> str:
    candidate = provider.get("provider_id") or provider.get("id") or provider.get("credential_ref")
    try:
        return validate_provider_id(candidate)
    except ProviderIdError:
        raise CredentialMigrationError("legacy provider has no valid provider ID") from None


def _secret_from_legacy_value(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    bearer = re.match(r"(?i)^bearer\s+(.+)$", value.strip())
    return bearer.group(1) if bearer else value


def _find_legacy_secret(value: Any, *, key: object | None = None) -> str | None:
    if _is_sensitive_key(key):
        return _secret_from_legacy_value(value)
    if isinstance(value, Mapping):
        for item_key, item_value in value.items():
            secret = _find_legacy_secret(item_value, key=item_key)
            if secret is not None:
                return secret
    if isinstance(value, list):
        for item in value:
            secret = _find_legacy_secret(item)
            if secret is not None:
                return secret
    return None


def _verify_written_credential(
    credential_store: CredentialStore,
    provider_id: str,
    secret: str,
) -> None:
    try:
        credential_store.set(provider_id, secret)
        readback = credential_store.get(provider_id)
    except Exception:
        raise CredentialMigrationError("credential write or verification failed") from None
    if readback != secret:
        raise CredentialMigrationError("credential write or verification failed")


def migrate_legacy_catalog(
    source: Mapping[str, Any] | str | Path,
    credential_store: CredentialStore,
    *,
    destination: str | Path | None = None,
) -> dict[str, Any]:
    """Explicitly migrate legacy inline credentials and preserve the source file."""

    catalog, source_path = _load_catalog(source)
    if destination is not None and source_path is not None:
        destination_path = Path(destination)
        try:
            same_path = destination_path.resolve() == source_path.resolve()
        except OSError:
            raise CredentialMigrationError("could not validate catalog destination") from None
        if same_path:
            raise CredentialMigrationError("migration destination must differ from source")
    providers = catalog.get("providers")
    if not isinstance(providers, list):
        raise CredentialMigrationError("legacy catalog has no providers list")

    for provider in providers:
        if not isinstance(provider, Mapping):
            raise CredentialMigrationError("legacy provider must be an object")
        provider_id = _provider_id_from_record(provider)
        secret = _find_legacy_secret(provider)
        if secret is not None:
            _verify_written_credential(credential_store, provider_id, secret)
        elif not provider.get("credential_ref"):
            raise CredentialMigrationError("legacy provider has no credential")

    migrated = serialize_catalog(catalog)
    if destination is not None:
        destination_path = Path(destination)
        try:
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.write_text(
                json.dumps(migrated, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            raise CredentialMigrationError("could not write migrated catalog") from None
    return migrated
