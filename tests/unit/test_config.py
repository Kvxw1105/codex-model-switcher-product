import hashlib
import json

import pytest

from codex_model_switcher.catalog import (
    PickerSchemaEvidence,
    PickerVerificationReceipt,
    TrustedPickerVerifier,
    catalog_fingerprint,
    load_catalog,
)
from codex_model_switcher.config import (
    ConfigChangedError,
    ConfigError,
    apply_managed_config,
    restore_managed_config,
)


def _verification(catalog_path):
    catalog = load_catalog(catalog_path)
    class FixtureTrustedVerifier(TrustedPickerVerifier):
        def _read_current_client_evidence(self, candidate):
            return PickerSchemaEvidence(
                schema_version=candidate.schema_version,
                client_version=candidate.client_version,
                source="fixture-verifier",
            )

    verifier = FixtureTrustedVerifier()
    return verifier.issue_receipt(catalog)


def _safe_catalog() -> dict[str, object]:
    return {
        "schema_version": "picker-v1",
        "client_version": "9.9.9",
        "provider_id": "example-provider",
        "models": [
            {
                "id": "cms-example-chat",
                "display_name": "Example API",
                "lane": "third_party",
                "provider_id": "example-provider",
                "upstream_model": "example-chat",
                "capability": {
                    "context_window": 32_000,
                    "supports_responses": True,
                    "supports_streaming": True,
                    "supports_tools": True,
                    "supports_images": False,
                    "supports_files": True,
                    "supports_compaction_context": False,
                },
            }
        ],
    }


def test_apply_then_restore_is_byte_exact(tmp_path, monkeypatch) -> None:
    isolated_home = tmp_path / "isolated-codex-home"
    isolated_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    config_path = isolated_home / "config.toml"
    original = b"# user comment\r\nmodel = \"gpt-5.6\"\r\n"
    config_path.write_bytes(original)
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")

    receipt = apply_managed_config(
        config_path,
        catalog_path,
        verification=_verification(catalog_path),
    )

    assert config_path.read_bytes() != original
    assert receipt.original_hash == hashlib.sha256(original).hexdigest()
    assert receipt.written_hash == hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert receipt.backup_path.read_bytes() == original
    assert b"codex-model-switcher managed start" in config_path.read_bytes()

    restore_managed_config(config_path, receipt)

    assert config_path.read_bytes() == original


def test_restore_refuses_to_overwrite_external_edits(tmp_path, monkeypatch) -> None:
    isolated_home = tmp_path / "isolated-codex-home"
    isolated_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    config_path = isolated_home / "config.toml"
    config_path.write_bytes(b"model = \"original\"\n")
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")

    receipt = apply_managed_config(
        config_path,
        catalog_path,
        verification=_verification(catalog_path),
    )
    config_path.write_bytes(config_path.read_bytes() + b"# external edit\n")

    with pytest.raises(ConfigChangedError, match="changed"):
        restore_managed_config(config_path, receipt)


def test_reapplying_replaces_only_the_managed_block(tmp_path, monkeypatch) -> None:
    isolated_home = tmp_path / "isolated-codex-home"
    isolated_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    config_path = isolated_home / "config.toml"
    config_path.write_bytes(b"# keep this\nmodel = \"original\"\n")
    first_catalog_path = tmp_path / "first-picker.json"
    first_catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")
    second_catalog = _safe_catalog()
    second_catalog["client_version"] = "10.0.0"
    second_catalog_path = tmp_path / "second-picker.json"
    second_catalog_path.write_text(json.dumps(second_catalog), encoding="utf-8")

    apply_managed_config(
        config_path,
        first_catalog_path,
        verification=_verification(first_catalog_path),
    )
    apply_managed_config(
        config_path,
        second_catalog_path,
        verification=_verification(second_catalog_path),
    )

    rendered = config_path.read_text(encoding="utf-8")
    assert rendered.count("codex-model-switcher managed start") == 1
    assert "# keep this\nmodel = \"original\"\n" in rendered
    assert "10.0.0" in rendered


def test_apply_rejects_unverified_candidate_without_external_attestation(
    tmp_path,
    monkeypatch,
) -> None:
    isolated_home = tmp_path / "isolated-codex-home"
    isolated_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    config_path = isolated_home / "config.toml"
    config_path.write_bytes(b"model = \"original\"\n")
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")

    with pytest.raises(ConfigError, match="UNVERIFIED"):
        apply_managed_config(config_path, catalog_path)


def test_public_receipt_construction_cannot_authorize_apply(tmp_path) -> None:
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")
    catalog = load_catalog(catalog_path)

    with pytest.raises(TypeError, match="verifier-issued"):
        PickerVerificationReceipt(
            schema_version=catalog.schema_version,
            client_version=catalog.client_version,
            catalog_sha256=catalog_fingerprint(catalog),
            source="current-client-artifact",
        )


def test_caller_forged_receipt_is_rejected(tmp_path, monkeypatch) -> None:
    isolated_home = tmp_path / "isolated-codex-home"
    isolated_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    config_path = isolated_home / "config.toml"
    config_path.write_bytes(b"model = \"original\"\n")
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")
    catalog = load_catalog(catalog_path)

    class CallerForgedReceipt:
        schema_version = catalog.schema_version
        client_version = catalog.client_version
        catalog_sha256 = catalog_fingerprint(catalog)
        source = "current-client-artifact"

    with pytest.raises(ConfigError, match="verifier-issued|authentic"):
        apply_managed_config(config_path, catalog_path, verification=CallerForgedReceipt())


@pytest.mark.parametrize(
    "line",
    [
        "# user note: # >>> codex-model-switcher managed start",
        'note = "# <<< codex-model-switcher managed end"',
    ],
)
def test_marker_substrings_inside_user_content_are_rejected(tmp_path, monkeypatch, line) -> None:
    isolated_home = tmp_path / "isolated-codex-home"
    isolated_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    config_path = isolated_home / "config.toml"
    config_path.write_text(line + "\n", encoding="utf-8")
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")

    with pytest.raises(ConfigError, match="marker"):
        apply_managed_config(config_path, catalog_path, verification=_verification(catalog_path))


def test_duplicate_managed_blocks_are_rejected(tmp_path, monkeypatch) -> None:
    isolated_home = tmp_path / "isolated-codex-home"
    isolated_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    config_path = isolated_home / "config.toml"
    config_path.write_bytes(b"model = \"original\"\n")
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")
    verification = _verification(catalog_path)

    apply_managed_config(config_path, catalog_path, verification=verification)
    existing = config_path.read_text(encoding="utf-8")
    config_path.write_text(existing + existing, encoding="utf-8")

    with pytest.raises(ConfigError, match="exactly one|duplicate|marker"):
        apply_managed_config(config_path, catalog_path, verification=verification)


def test_marker_lines_inside_toml_multiline_string_are_rejected(tmp_path, monkeypatch) -> None:
    isolated_home = tmp_path / "isolated-codex-home"
    isolated_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    config_path = isolated_home / "config.toml"
    config_path.write_text(
        'notes = """\n'
        "# >>> codex-model-switcher managed start\n"
        "# <<< codex-model-switcher managed end\n"
        '"""\n',
        encoding="utf-8",
    )
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")

    with pytest.raises(ConfigError, match="multiline"):
        apply_managed_config(config_path, catalog_path, verification=_verification(catalog_path))
