import json
from pathlib import Path
from typing import Any

import pytest

from codex_model_switcher.credentials import (
    CredentialMigrationError,
    CredentialNotConfiguredError,
    CredentialStore,
    CredentialStoreError,
    KeyringCredentialStore,
    ProviderIdAllowlist,
    ProviderIdError,
    build_third_party_headers,
    configure_credential,
    migrate_legacy_catalog,
    resolve_upstream_auth,
    serialize_provider_record,
)


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.get_calls: list[str] = []

    def set(self, provider_id: str, secret: str) -> None:
        self.values[provider_id] = secret

    def get(self, provider_id: str) -> str:
        self.get_calls.append(provider_id)
        return self.values[provider_id]

    def delete(self, provider_id: str) -> None:
        self.values.pop(provider_id, None)

    def exists(self, provider_id: str) -> bool:
        return provider_id in self.values


def assert_secret_absent(value: Any, secret: str) -> None:
    if secret in repr(value):
        raise AssertionError("sensitive fixture leaked")


def assert_secret_equal(actual: Any, expected: Any) -> None:
    if actual != expected:
        raise AssertionError("credential value mismatch")


def test_provider_record_persists_credential_ref_instead_of_bearer_value() -> None:
    secret = "fixture-provider-secret-value"
    record = serialize_provider_record(
        {
            "provider_id": "deepseek",
            "base_url": "https://api.example.invalid/v1",
            "token": secret,
            "headers": {"Authorization": f"Bearer {secret}"},
        }
    )

    assert record["credential_ref"] == "deepseek"
    assert "token" not in record
    assert "headers" not in record
    assert_secret_absent(record, secret)


def test_camel_case_sensitive_fields_are_removed_but_safe_labels_remain() -> None:
    secret = "fixture-camel-case-secret-value"
    record = serialize_provider_record(
        {
            "provider_id": "deepseek",
            "tokenValue": secret,
            "accessTokenValue": secret,
            "apiKeyValue": secret,
            "accessToken": secret,
            "apiKey": secret,
            "tokenLabel": "display-only",
            "credentialRef": secret,
        }
    )

    assert "tokenValue" not in record
    assert "accessTokenValue" not in record
    assert "apiKeyValue" not in record
    assert "accessToken" not in record
    assert "apiKey" not in record
    assert "credentialRef" not in record
    assert_secret_equal(record["credential_ref"], "deepseek")
    assert record["tokenLabel"] == "display-only"
    assert_secret_absent(record, secret)


def test_unregistered_credential_ref_cannot_become_username() -> None:
    with pytest.raises(ProviderIdError):
        serialize_provider_record({"credential_ref": "unregistered-provider"})


def test_memory_fake_satisfies_credential_store_protocol() -> None:
    assert isinstance(MemoryCredentialStore(), CredentialStore)


def test_keyring_store_uses_fixed_service_and_validated_provider_username() -> None:
    class FakeKeyring:
        def __init__(self) -> None:
            self.values: dict[tuple[str, str], str] = {}
            self.calls: list[tuple[str, str]] = []

        def set_password(self, service: str, username: str, password: str) -> None:
            self.calls.append((service, username))
            self.values[(service, username)] = password

        def get_password(self, service: str, username: str) -> str | None:
            self.calls.append((service, username))
            return self.values.get((service, username))

        def delete_password(self, service: str, username: str) -> None:
            self.calls.append((service, username))
            self.values.pop((service, username), None)

    keyring = FakeKeyring()
    store = KeyringCredentialStore(keyring_module=keyring)
    secret = "fixture-keyring-secret-value"

    store.set("deepseek", secret)

    assert store.exists("deepseek") is True
    assert_secret_equal(store.get("deepseek"), secret)
    assert all(service == "CodexModelSwitcher" for service, _ in keyring.calls)
    assert all(username == "deepseek" for _, username in keyring.calls)
    store.delete("deepseek")
    assert store.exists("deepseek") is False
    assert_secret_absent(keyring.calls, secret)


def test_keyring_store_rejects_unvalidated_provider_username() -> None:
    class FakeKeyring:
        def set_password(self, *_args: object) -> None:
            raise AssertionError("backend must not be called")

    store = KeyringCredentialStore(keyring_module=FakeKeyring())

    with pytest.raises(ProviderIdError):
        store.set("user@example.com", "fixture-secret")


def test_keyring_store_requires_registered_provider_id_and_rejects_secret_like_id() -> None:
    class FakeKeyring:
        def set_password(self, *_args: object) -> None:
            raise AssertionError("backend must not be called")

    store = KeyringCredentialStore(
        keyring_module=FakeKeyring(),
        provider_id_verifier=ProviderIdAllowlist({"deepseek"}),
    )

    with pytest.raises(ProviderIdError):
        store.set("unregistered-provider", "fixture-secret")
    with pytest.raises(ProviderIdError):
        store.set("token-value-secret", "fixture-secret")


def test_keyring_backend_failure_does_not_chain_secret() -> None:
    secret = "fixture-backend-error-secret-value"

    class LeakyKeyring:
        def set_password(self, *_args: object) -> None:
            raise RuntimeError(f"backend rejected {secret}")

    with pytest.raises(CredentialStoreError) as error:
        KeyringCredentialStore(keyring_module=LeakyKeyring()).set("deepseek", secret)

    assert error.value.__cause__ is None
    assert_secret_absent(error.value, secret)


def test_configure_credential_returns_only_boolean_status() -> None:
    store = MemoryCredentialStore()
    secret = "fixture-configured-secret-value"

    result = configure_credential(store, "deepseek", secret)

    assert result == {"configured": True}
    assert set(result) == {"configured"}
    assert isinstance(result["configured"], bool)


def test_configure_credential_hides_backend_failure() -> None:
    class FailingStore(MemoryCredentialStore):
        def set(self, provider_id: str, secret: str) -> None:
            raise RuntimeError(f"backend rejected {secret}")

    result = configure_credential(FailingStore(), "deepseek", "fixture-failing-secret")

    assert result == {"configured": False}
    assert_secret_absent(result, "fixture-failing-secret")


def test_third_party_credential_ignores_inbound_authorization() -> None:
    store = MemoryCredentialStore()
    secret = "fixture-third-party-secret-value"
    store.set("deepseek", secret)

    resolved = resolve_upstream_auth(
        lane="third_party",
        provider_id="deepseek",
        inbound_authorization="Bearer test-auth",
        credential_store=store,
    )

    assert_secret_equal(resolved, f"Bearer {secret}")
    assert "test-auth" not in resolved
    assert store.get_calls == ["deepseek"]


def test_third_party_backend_failure_is_fixed_and_cause_free() -> None:
    secret = "fixture-backend-read-secret-value"

    class FailingStore(MemoryCredentialStore):
        def get(self, provider_id: str) -> str:
            raise RuntimeError(f"backend leaked {secret}")

    with pytest.raises(CredentialStoreError) as error:
        resolve_upstream_auth(
            lane="third_party",
            provider_id="deepseek",
            inbound_authorization="Bearer inbound",
            credential_store=FailingStore(),
        )

    assert_secret_equal(str(error.value), "credential backend read failed")
    assert error.value.__cause__ is None
    assert_secret_absent(error.value, secret)


def test_third_party_headers_strip_inbound_auth_and_inject_only_provider_credential() -> None:
    inbound_secret = "fixture-inbound-header-secret-value"
    provider_secret = "fixture-provider-header-secret-value"
    store = MemoryCredentialStore()
    store.set("deepseek", provider_secret)
    inbound = {
        "Authorization": f"Bearer {inbound_secret}",
        "cookie": "session=fixture-cookie",
        "OpenAI-Organization": "org-fixture",
        "X-ChatGPT-Account-Id": "account-fixture",
        "X-Client-Version": "client-1",
    }

    headers = build_third_party_headers(
        inbound,
        provider_id="deepseek",
        credential_store=store,
    )

    assert_secret_equal(headers["Authorization"], f"Bearer {provider_secret}")
    assert "X-Client-Version" not in headers
    assert "cookie" not in {name.lower() for name in headers}
    assert "openai-organization" not in {name.lower() for name in headers}
    assert "x-chatgpt-account-id" not in {name.lower() for name in headers}
    assert_secret_absent(headers, inbound_secret)


@pytest.mark.parametrize(
    "header_name",
    [
        "Authorization",
        "authorization",
        "COOKIE",
        "Cookie",
        "Api-Key",
        "api_key",
        "Access-Token",
        "access_token",
        "Refresh-Token",
        "refresh_token",
        "Account-Id",
        "account_id",
        "Account-Email",
        "account_email",
        "X-Account-Name",
        "x_account_name",
        "OpenAI-Organization",
        "openai_organization",
        "ChatGPT-Account-Id",
        "chatgpt_account_id",
    ],
)
def test_third_party_headers_allowlist_drops_identity_and_credential_variants(
    header_name: str,
) -> None:
    inbound_secret = "fixture-parameterized-inbound-secret-value"
    provider_secret = "fixture-parameterized-provider-secret-value"
    store = MemoryCredentialStore()
    store.set("deepseek", provider_secret)
    inbound = {"Accept": "application/json", header_name: inbound_secret}

    headers = build_third_party_headers(
        inbound,
        provider_id="deepseek",
        credential_store=store,
    )

    assert set(headers) == {"Accept", "Authorization"}
    assert_secret_equal(headers["Accept"], "application/json")
    assert_secret_equal(headers["Authorization"], f"Bearer {provider_secret}")
    assert_secret_absent(headers, inbound_secret)


def test_third_party_missing_credential_does_not_return_inbound_authorization() -> None:
    store = MemoryCredentialStore()

    with pytest.raises(CredentialNotConfiguredError) as error:
        resolve_upstream_auth(
            lane="third_party",
            provider_id="deepseek",
            inbound_authorization="Bearer test-auth",
            credential_store=store,
        )

    assert "test-auth" not in str(error.value)


def test_migrate_legacy_catalog_writes_and_verifies_secret_without_deleting_source(
    tmp_path: Path,
) -> None:
    secret = "fixture-legacy-catalog-secret-value"
    source = tmp_path / "legacy-catalog.json"
    destination = tmp_path / "migrated-catalog.json"
    original = {
        "providers": [
            {
                "provider_id": "deepseek",
                "base_url": "https://api.example.invalid/v1",
                "api_key": secret,
            }
        ]
    }
    source.write_text(json.dumps(original), encoding="utf-8")
    original_bytes = source.read_bytes()
    store = MemoryCredentialStore()

    migrated = migrate_legacy_catalog(source, store, destination=destination)

    assert source.read_bytes() == original_bytes
    assert migrated["providers"][0]["credential_ref"] == "deepseek"
    assert_secret_absent(migrated, secret)
    assert_secret_absent(json.loads(destination.read_text(encoding="utf-8")), secret)
    assert_secret_equal(store.get("deepseek"), secret)


def test_migrate_nested_credentials_api_key_writes_and_verifies_secret(tmp_path: Path) -> None:
    secret = "fixture-nested-catalog-secret-value"
    source = tmp_path / "legacy-catalog.json"
    source.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider_id": "deepseek",
                        "credentials": {"api_key": secret},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = MemoryCredentialStore()

    migrated = migrate_legacy_catalog(source, store)

    assert migrated["providers"][0]["credential_ref"] == "deepseek"
    assert_secret_equal(store.get("deepseek"), secret)
    assert_secret_absent(migrated, secret)


def test_migrate_rejects_multiple_nested_credentials_without_output(tmp_path: Path) -> None:
    source = tmp_path / "legacy-catalog.json"
    source.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider_id": "deepseek",
                        "credentials": {
                            "api_key": "fixture-first-secret-value",
                            "access_token": "fixture-second-secret-value",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CredentialMigrationError) as error:
        migrate_legacy_catalog(source, MemoryCredentialStore())

    assert str(error.value) == "legacy provider contains multiple credentials"


def test_migrate_legacy_catalog_stops_before_output_when_readback_differs(tmp_path: Path) -> None:
    secret = "fixture-readback-secret-value"
    source = tmp_path / "legacy-catalog.json"
    destination = tmp_path / "migrated-catalog.json"
    source.write_text(
        json.dumps({"providers": [{"provider_id": "deepseek", "token": secret}]}),
        encoding="utf-8",
    )

    class CorruptingStore(MemoryCredentialStore):
        def get(self, provider_id: str) -> str:
            super().get(provider_id)
            return "different-value"

    with pytest.raises(CredentialMigrationError) as error:
        migrate_legacy_catalog(source, CorruptingStore(), destination=destination)

    assert not destination.exists()
    assert_secret_absent(str(error.value), secret)
