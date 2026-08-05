"""Credential isolation and secret-safe observability primitives.

This module deliberately has no dependency on the Router or GUI.  Callers can
inject a ``CredentialStore`` and use the pure serialization/redaction helpers
until those layers are integrated.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Collection, Iterable
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

SERVICE_NAME = "CodexModelSwitcher"
REDACTED = "[REDACTED]"
REDACTED_URL = "[REDACTED_URL]"

_PROVIDER_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,63})$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_LABELED_SECRET_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"authorization|proxy-authorization|token|secret|password)\s*[:=]\s*)"
    r"(?:bearer\s+)?[^\s,;]+"
)
_KEY_SECRET_RE = re.compile(r"(?i)^sk-[a-z0-9_-]{8,}$")

_SENSITIVE_KEYS = {
    "access_token",
    "auth_header",
    "account_email",
    "api_key",
    "apikey",
    "api_token",
    "authorization",
    "bearer",
    "bearer_token",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "credential_value",
    "email",
    "id_token",
    "key",
    "password",
    "proxy_authorization",
    "refresh_token",
    "secret",
    "secret_key",
    "secret_value",
    "set_cookie",
    "token",
    "token_value",
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

DEFAULT_REGISTERED_PROVIDER_IDS = frozenset(
    {
        "anthropic",
        "cohere",
        "deepseek",
        "gemini",
        "google",
        "groq",
        "mistral",
        "moonshot",
        "openai",
        "openrouter",
        "qwen",
        "siliconflow",
        "volcengine",
        "xai",
        "zhipu",
    }
)


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


ProviderIdVerifier = Callable[[str], bool]
ProviderIdVerifierSpec = ProviderIdVerifier | Collection[str] | None
_SECRET_LIKE_PROVIDER_ID_RE = re.compile(
    r"(?i)(?:^|[-_])(bearer|token|secret|credential|api[_-]?key)(?:$|[-_])|^sk-[a-z0-9_-]{8,}$"
)


def _validate_provider_id_syntax(provider_id: str) -> str:
    if not isinstance(provider_id, str) or _PROVIDER_ID_RE.fullmatch(provider_id) is None:
        raise ProviderIdError("provider ID is not valid")
    if _SECRET_LIKE_PROVIDER_ID_RE.search(provider_id):
        raise ProviderIdError("provider ID is not valid")
    return provider_id


class ProviderIdAllowlist:
    """Explicit registry of provider IDs permitted for credential usernames."""

    def __init__(self, provider_ids: Iterable[str]) -> None:
        ids = frozenset(_validate_provider_id_syntax(provider_id) for provider_id in provider_ids)
        self._provider_ids = ids

    def __call__(self, provider_id: str) -> bool:
        return provider_id in self._provider_ids

    def __contains__(self, provider_id: object) -> bool:
        return provider_id in self._provider_ids


def _is_registered_provider(provider_id: str, verifier: ProviderIdVerifierSpec) -> bool:
    if verifier is None:
        return provider_id in DEFAULT_REGISTERED_PROVIDER_IDS
    if isinstance(verifier, Collection) and not isinstance(verifier, (str, bytes)):
        return provider_id in verifier
    if callable(verifier):
        try:
            return bool(verifier(provider_id))
        except Exception:
            return False
    return False


def verify_provider_id(
    provider_id: str,
    provider_id_verifier: ProviderIdVerifierSpec = None,
) -> str:
    """Validate syntax, reject secret-like IDs, and require registered membership."""

    provider_id = _validate_provider_id_syntax(provider_id)
    if not _is_registered_provider(provider_id, provider_id_verifier):
        raise ProviderIdError("provider ID is not registered")
    return provider_id


def validate_provider_id(
    provider_id: str,
    provider_id_verifier: ProviderIdVerifierSpec = None,
) -> str:
    """Validate the registered ID used as a Credential Manager username."""

    return verify_provider_id(provider_id, provider_id_verifier)


def credential_ref(
    provider_id: str,
    provider_id_verifier: ProviderIdVerifierSpec = None,
) -> str:
    """Return the non-secret catalog reference for a provider credential."""

    return verify_provider_id(provider_id, provider_id_verifier)


class KeyringCredentialStore:
    """CredentialStore backed by keyring's Windows Credential Manager backend."""

    SERVICE_NAME = SERVICE_NAME

    def __init__(
        self,
        keyring_module: Any | None = None,
        *,
        provider_id_verifier: ProviderIdVerifierSpec = None,
    ) -> None:
        if keyring_module is None:
            try:
                import keyring as keyring_module
            except ImportError:
                raise CredentialStoreError("keyring package is required") from None
        self._keyring = keyring_module
        self._provider_id_verifier = provider_id_verifier

    def set(self, provider_id: str, secret: str) -> None:
        username = validate_provider_id(provider_id, self._provider_id_verifier)
        _validate_secret(secret)
        try:
            self._keyring.set_password(self.SERVICE_NAME, username, secret)
        except Exception:
            raise CredentialStoreError("credential backend write failed") from None

    def get(self, provider_id: str) -> str:
        username = validate_provider_id(provider_id, self._provider_id_verifier)
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
        username = validate_provider_id(provider_id, self._provider_id_verifier)
        try:
            self._keyring.delete_password(self.SERVICE_NAME, username)
        except Exception as error:
            if type(error).__name__ == "PasswordDeleteError":
                return
            raise CredentialStoreError("credential backend delete failed") from None

    def exists(self, provider_id: str) -> bool:
        username = validate_provider_id(provider_id, self._provider_id_verifier)
        try:
            return self._keyring.get_password(self.SERVICE_NAME, username) is not None
        except Exception:
            raise CredentialStoreError("credential backend existence check failed") from None


WindowsCredentialStore = KeyringCredentialStore


def configure_credential(
    credential_store: CredentialStore,
    provider_id: str,
    secret: str,
    *,
    provider_id_verifier: ProviderIdVerifierSpec = None,
) -> dict[str, bool]:
    """Write a credential and expose only a boolean GUI/call-layer result."""

    try:
        validate_provider_id(provider_id, provider_id_verifier)
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
    provider_id_verifier: ProviderIdVerifierSpec = None,
) -> str | None:
    """Resolve an upstream auth header while keeping official and third-party lanes separate."""

    if lane == "third_party":
        validate_provider_id(provider_id, provider_id_verifier)
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


def _canonical_key(key: object) -> str:
    text = str(key).strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_").lower()


def _normal_key(key: object) -> str:
    return _canonical_key(key)


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


def _is_secret_like_text(value: str) -> bool:
    lowered = value.lower()
    return (
        "@" in value
        or "://" in value
        or any(
            marker in lowered
            for marker in (
                "api_key",
                "apikey",
                "authorization",
                "bearer",
                "credential",
                "password",
                "secret",
                "token",
            )
        )
        or _KEY_SECRET_RE.fullmatch(value) is not None
    )


def _safe_log_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if _SAFE_IDENTIFIER_RE.fullmatch(value) is None:
        return None
    if _is_secret_like_text(value) or _redact_text(value) != value:
        return None
    return value


def build_safe_log_record(
    fields: Mapping[str, Any] | None = None,
    **extra_fields: Any,
) -> dict[str, Any]:
    """Keep only the small, non-content telemetry contract used by default logs."""

    combined: dict[str, Any] = {}
    if fields is not None:
        combined.update(fields)
    combined.update(extra_fields)
    record: dict[str, Any] = {}
    for field in _SAFE_LOG_FIELDS:
        if field not in combined:
            continue
        value = combined[field]
        if field in {"route_id", "trace_id"}:
            safe_value = _safe_log_identifier(value)
            if safe_value is not None:
                record[field] = safe_value
        elif field == "status_code":
            if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                record[field] = value
        elif field in {"elapsed_ms", "byte_count"}:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if math.isfinite(value) and value >= 0:
                    record[field] = value
    return record


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
    provider_id_verifier: ProviderIdVerifierSpec = None,
) -> dict[str, Any]:
    """Serialize provider metadata with a credential reference, never a secret."""

    if not isinstance(provider, Mapping):
        raise TypeError("provider must be a mapping")
    candidate = provider_id or provider.get("provider_id") or provider.get("id")
    if candidate is None:
        candidate = provider.get("credential_ref")
    reference = credential_ref(candidate, provider_id_verifier)  # type: ignore[arg-type]
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


def serialize_catalog(
    catalog: Mapping[str, Any],
    *,
    provider_id_verifier: ProviderIdVerifierSpec = None,
) -> dict[str, Any]:
    """Serialize a catalog while replacing provider secrets with credential refs."""

    if not isinstance(catalog, Mapping):
        raise TypeError("catalog must be a mapping")
    result = _sanitize_catalog_value(catalog)
    if not isinstance(result, dict):
        raise TypeError("catalog must serialize to an object")
    providers = catalog.get("providers")
    if isinstance(providers, list):
        result["providers"] = [
            serialize_provider_record(provider, provider_id_verifier=provider_id_verifier)
            for provider in providers
        ]
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


def _provider_id_from_record(
    provider: Mapping[str, Any],
    provider_id_verifier: ProviderIdVerifierSpec = None,
) -> str:
    candidate = provider.get("provider_id") or provider.get("id") or provider.get("credential_ref")
    try:
        return validate_provider_id(candidate, provider_id_verifier)
    except ProviderIdError:
        raise CredentialMigrationError("legacy provider has no valid provider ID") from None


def _secret_from_legacy_value(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    bearer = re.match(r"(?i)^bearer\s+(.+)$", value.strip())
    return bearer.group(1) if bearer else value


def _collect_legacy_secrets(value: Any, *, key: object | None = None) -> list[str]:
    secrets: list[str] = []
    if _is_sensitive_key(key) and isinstance(value, str):
        secret = _secret_from_legacy_value(value)
        if secret is not None:
            secrets.append(secret)
    if isinstance(value, Mapping):
        for item_key, item_value in value.items():
            secrets.extend(_collect_legacy_secrets(item_value, key=item_key))
    if isinstance(value, list):
        for item in value:
            secrets.extend(_collect_legacy_secrets(item, key=key))
    return secrets


def _find_legacy_secret(value: Any, *, key: object | None = None) -> str | None:
    secrets = _collect_legacy_secrets(value, key=key)
    if len(secrets) > 1:
        raise CredentialMigrationError("legacy provider contains multiple credentials")
    return secrets[0] if secrets else None


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
    provider_id_verifier: ProviderIdVerifierSpec = None,
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
        provider_id = _provider_id_from_record(provider, provider_id_verifier)
        secret = _find_legacy_secret(provider)
        if secret is not None:
            _verify_written_credential(credential_store, provider_id, secret)
        elif not provider.get("credential_ref"):
            raise CredentialMigrationError("legacy provider has no credential")

    migrated = serialize_catalog(catalog, provider_id_verifier=provider_id_verifier)
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
