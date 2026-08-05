import hashlib
import json

import pytest

from codex_model_switcher.catalog import (
    PickerVerificationReceipt,
    catalog_fingerprint,
    load_catalog,
)
from codex_model_switcher.config import (
    MANAGED_END,
    MANAGED_START,
    ConfigChangedError,
    ConfigError,
    ConfigReceipt,
    _replace_or_append_managed_block,
    apply_managed_config,
    render_managed_config,
    restore_managed_config,
)


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


def test_managed_block_replacement_round_trips_user_bytes() -> None:
    original_block = "\r\n".join(
        (
            MANAGED_START,
            'model_provider = "old-provider"',
            'model_catalog_json = "old-catalog"',
            MANAGED_END,
        )
    )
    original = "# user comment\r\n" + original_block + "\r\n"
    new_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "example-catalog"',
            MANAGED_END,
        )
    )

    written = _replace_or_append_managed_block(original, new_block)
    restored = _replace_or_append_managed_block(written, original_block)

    assert written.encode("utf-8") != original.encode("utf-8")
    assert restored.encode("utf-8") == original.encode("utf-8")


def test_restore_refuses_to_overwrite_external_edits(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    original = b'model = "original"\n'
    written = b'model = "managed"\n'
    config_path.write_bytes(written)
    backup_path = tmp_path / "config.toml.bak"
    backup_path.write_bytes(original)
    receipt = ConfigReceipt(
        config_path=config_path,
        backup_path=backup_path,
        original_hash=hashlib.sha256(original).hexdigest(),
        written_hash=hashlib.sha256(written).hexdigest(),
        timestamp="20260805T000000000000Z",
    )
    config_path.write_bytes(written + b"# external edit\n")

    with pytest.raises(ConfigChangedError, match="changed"):
        restore_managed_config(config_path, receipt)


def test_replacement_replaces_only_the_managed_block() -> None:
    first_block = "\n".join((MANAGED_START, 'version = "9.9.9"', MANAGED_END))
    second_block = "\n".join((MANAGED_START, 'version = "10.0.0"', MANAGED_END))
    original = '# keep this\nmodel = "original"\n'

    first = _replace_or_append_managed_block(original, first_block)
    second = _replace_or_append_managed_block(first, second_block)

    assert second.count(MANAGED_START) == 1
    assert '# keep this\nmodel = "original"\n' in second
    assert 'version = "10.0.0"' in second


def test_render_and_apply_reject_without_real_receipt(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_bytes(b'model = "original"\n')
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")

    with pytest.raises(ConfigError, match="UNVERIFIED"):
        render_managed_config(catalog_path)
    with pytest.raises(ConfigError, match="UNVERIFIED"):
        apply_managed_config(config_path, catalog_path)


def test_apply_rejects_concurrent_edit_before_replace(tmp_path, monkeypatch) -> None:
    import codex_model_switcher.config as config_module

    config_path = tmp_path / "config.toml"
    config_path.write_bytes(b'model = "original"\n')
    catalog_path = tmp_path / "candidate.json"
    managed_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "{}"',
            MANAGED_END,
        )
    )
    external = b'model = "edited concurrently"\n'
    real_atomic_write = config_module._atomic_write

    monkeypatch.setattr(
        config_module,
        "render_managed_config",
        lambda _path, *, verification=None: managed_block,
    )

    def racing_atomic_write(path, data, *, expected=None):
        if expected is not None and path.resolve() == config_path.resolve():
            config_path.write_bytes(external)
        if expected is None:
            return real_atomic_write(path, data)
        return real_atomic_write(path, data, expected=expected)

    monkeypatch.setattr(config_module, "_atomic_write", racing_atomic_write)

    with pytest.raises(ConfigChangedError, match="changed"):
        apply_managed_config(config_path, catalog_path)

    assert config_path.read_bytes() == external


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


def test_caller_forged_receipt_is_rejected(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_bytes(b'model = "original"\n')
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


def test_object_new_receipt_copy_is_rejected_by_apply(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_bytes(b'model = "original"\n')
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")
    catalog = load_catalog(catalog_path)

    import codex_model_switcher.catalog as catalog_module

    forged = object.__new__(PickerVerificationReceipt)
    for field_name, value in {
        "schema_version": catalog.schema_version,
        "client_version": catalog.client_version,
        "catalog_sha256": catalog_fingerprint(catalog),
        "source": "current-client-artifact",
        "_writer_capability": getattr(catalog_module, "_WRITER_CAPABILITY_TOKEN", object()),
    }.items():
        object.__setattr__(forged, field_name, value)

    with pytest.raises(ConfigError, match="verifier-issued|authentic"):
        apply_managed_config(config_path, catalog_path, verification=forged)


@pytest.mark.parametrize(
    "line",
    [
        "# user note: # >>> codex-model-switcher managed start",
        'note = "# <<< codex-model-switcher managed end"',
    ],
)
def test_marker_substrings_inside_user_content_are_rejected(line) -> None:
    with pytest.raises(ConfigError, match="marker"):
        _replace_or_append_managed_block(line + "\n", "managed")


def test_duplicate_managed_blocks_are_rejected() -> None:
    block = "\n".join((MANAGED_START, "managed", MANAGED_END))

    with pytest.raises(ConfigError, match="exactly one|duplicate|marker"):
        _replace_or_append_managed_block(block + "\n" + block, "managed")


def test_marker_lines_inside_toml_multiline_string_are_rejected() -> None:
    config_text = (
        'notes = """\n'
        "# >>> codex-model-switcher managed start\n"
        "# <<< codex-model-switcher managed end\n"
        '"""\n'
    )

    with pytest.raises(ConfigError, match="multiline"):
        _replace_or_append_managed_block(config_text, "managed")
