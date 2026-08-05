import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

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
    ConfigPostCommitError,
    ConfigReceipt,
    ConfigTransactionStateError,
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


def _assert_child_replace_is_blocked(external_path, target_path) -> None:
    probe = (
        "import os, sys\n"
        "try:\n"
        "    os.replace(sys.argv[1], sys.argv[2])\n"
        "except OSError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(external_path), str(target_path)],
        check=False,
    )
    assert result.returncode == 0, "child os.replace crossed the protected rename boundary"


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


def test_windows_atomic_replace_requires_lease(tmp_path, monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows replacement handoff is Windows-specific")

    import codex_model_switcher.config as config_module

    temporary_path = tmp_path / "candidate.tmp"
    target_path = tmp_path / "config.toml"
    temporary_path.write_bytes(b"new\n")
    target_path.write_bytes(b"old\n")

    def forbidden_replace(*args, **kwargs):
        raise AssertionError("Windows replacement must not bypass the path lease")

    monkeypatch.setattr(config_module.os, "replace", forbidden_replace)

    with pytest.raises(ConfigError, match="active path lease"):
        config_module._replace_temp_file(
            temporary_path,
            target_path,
            expected=b"new\n",
        )

    assert target_path.read_bytes() == b"old\n"


def test_cooperative_lock_requires_fcntl(tmp_path, monkeypatch) -> None:
    import builtins

    import codex_model_switcher.config as config_module

    lease = config_module._PathLease(tmp_path / "config.toml", create=True)
    real_import = builtins.__import__

    def missing_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("test missing fcntl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_fcntl)

    with pytest.raises(ConfigError, match="cooperative config locking"):
        lease._acquire_cooperative()

    assert lease._cooperative_stream is None


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


def test_apply_without_real_receipt_does_not_create_missing_config(tmp_path) -> None:
    config_path = tmp_path / "missing-config.toml"
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(json.dumps(_safe_catalog()), encoding="utf-8")

    with pytest.raises(ConfigError, match="UNVERIFIED"):
        apply_managed_config(config_path, catalog_path)

    assert not config_path.exists()


def test_apply_restore_preserves_original_bytes_with_low_level_render_seam(
    tmp_path, monkeypatch
) -> None:
    import codex_model_switcher.config as config_module

    config_dir = tmp_path / "codex-home"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    original = b"# keep bytes\r\nmodel = \"original\"\r\n"
    config_path.write_bytes(original)
    catalog_path = tmp_path / "candidate.json"
    managed_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "{}"',
            MANAGED_END,
        )
    )

    monkeypatch.setattr(
        config_module,
        "render_managed_config",
        lambda _path, *, verification=None: managed_block,
    )

    receipt = apply_managed_config(config_path, catalog_path)
    assert receipt.backup_path.read_bytes() == original
    assert config_path.read_bytes() != original

    restore_managed_config(config_path, receipt)

    assert config_path.read_bytes() == original


def test_windows_new_config_is_removed_after_precommit_failure(tmp_path, monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows OPEN_ALWAYS rollback is Windows-specific")

    import codex_model_switcher.config as config_module

    config_path = tmp_path / "new-config.toml"
    catalog_path = tmp_path / "candidate.json"
    managed_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "{}"',
            MANAGED_END,
        )
    )
    monkeypatch.setattr(
        config_module,
        "render_managed_config",
        lambda _path, *, verification=None: managed_block,
    )

    def fail_before_replacement(*args, **kwargs):
        raise ConfigError("injected pre-commit backup failure")

    monkeypatch.setattr(config_module, "_atomic_write", fail_before_replacement)

    with pytest.raises(ConfigError, match="pre-commit backup failure"):
        apply_managed_config(config_path, catalog_path)

    assert not config_path.exists()


def test_windows_new_config_is_removed_after_replacement_failure(tmp_path, monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows replacement rollback is Windows-specific")

    import codex_model_switcher.config as config_module

    config_path = tmp_path / "new-config.toml"
    catalog_path = tmp_path / "candidate.json"
    managed_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "{}"',
            MANAGED_END,
        )
    )
    monkeypatch.setattr(
        config_module,
        "render_managed_config",
        lambda _path, *, verification=None: managed_block,
    )
    real_replace_temp_file = config_module._replace_temp_file

    def fail_config_replacement(temporary_path, target_path, *, lease=None, expected=None):
        if target_path.resolve() == config_path.resolve():
            raise ConfigError("injected pre-commit replacement failure")
        return real_replace_temp_file(
            temporary_path,
            target_path,
            lease=lease,
            expected=expected,
        )

    monkeypatch.setattr(config_module, "_replace_temp_file", fail_config_replacement)

    with pytest.raises(ConfigError, match="pre-commit replacement failure"):
        apply_managed_config(config_path, catalog_path)

    assert not config_path.exists()


def test_windows_postcommit_cleanup_failure_keeps_backup_and_status(
    tmp_path, monkeypatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows post-commit cleanup is Windows-specific")

    import codex_model_switcher.config as config_module

    config_path = tmp_path / "config.toml"
    original = b"# original bytes\r\n"
    config_path.write_bytes(original)
    catalog_path = tmp_path / "candidate.json"
    managed_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "{}"',
            MANAGED_END,
        )
    )
    monkeypatch.setattr(
        config_module,
        "render_managed_config",
        lambda _path, *, verification=None: managed_block,
    )
    real_replace_temp_file = config_module._replace_temp_file

    def replace_then_fail(temporary_path, target_path, *, lease=None, expected=None):
        result = real_replace_temp_file(
            temporary_path,
            target_path,
            lease=lease,
            expected=expected,
        )
        if target_path.resolve() == config_path.resolve():
            raise OSError("injected post-commit cleanup failure")
        return result

    monkeypatch.setattr(config_module, "_replace_temp_file", replace_then_fail)

    with pytest.raises(ConfigError, match="committed") as caught:
        apply_managed_config(config_path, catalog_path)

    error = caught.value
    assert getattr(error, "committed", False) is True
    backup_path = getattr(error, "backup_path", None)
    assert backup_path is not None
    assert backup_path.read_bytes() == original
    assert b'model_provider = "example-provider"' in config_path.read_bytes()


def test_windows_postcommit_close_failure_keeps_backup_and_status(
    tmp_path, monkeypatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows post-commit handle cleanup is Windows-specific")

    import codex_model_switcher.config as config_module

    config_path = tmp_path / "config.toml"
    original = b"# original bytes\r\n"
    config_path.write_bytes(original)
    catalog_path = tmp_path / "candidate.json"
    managed_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "{}"',
            MANAGED_END,
        )
    )
    monkeypatch.setattr(
        config_module,
        "render_managed_config",
        lambda _path, *, verification=None: managed_block,
    )
    real_release = config_module._PathLease._release_windows_handle
    injected = [False]

    def fail_new_handle_close(self, kernel32, handle, overlapped):
        if (
            self.path == config_path.resolve()
            and self._replacement_committed
            and overlapped is None
            and not injected[0]
        ):
            injected[0] = True
            return 123
        return real_release(self, kernel32, handle, overlapped)

    monkeypatch.setattr(
        config_module._PathLease,
        "_release_windows_handle",
        fail_new_handle_close,
    )

    with pytest.raises(ConfigError, match="committed") as caught:
        apply_managed_config(config_path, catalog_path)

    error = caught.value
    assert injected == [True]
    assert getattr(error, "committed", False) is True
    backup_path = getattr(error, "backup_path", None)
    assert backup_path is not None
    assert backup_path.read_bytes() == original
    assert b'model_provider = "example-provider"' in config_path.read_bytes()


def test_windows_commit_success_then_throw_keeps_backup_and_status(
    tmp_path, monkeypatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows commit boundary is Windows-specific")

    import codex_model_switcher.config as config_module

    config_path = tmp_path / "config.toml"
    original = b"# original bytes\r\n"
    config_path.write_bytes(original)
    catalog_path = tmp_path / "candidate.json"
    managed_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "{}"',
            MANAGED_END,
        )
    )
    monkeypatch.setattr(
        config_module,
        "render_managed_config",
        lambda _path, *, verification=None: managed_block,
    )
    original_commit = config_module._commit_windows_transaction
    commit_results: list[bool] = []

    def commit_success_then_throw(commit_transaction, transaction_handle):
        committed = original_commit(commit_transaction, transaction_handle)
        commit_results.append(committed)
        if len(commit_results) == 2 and committed:
            raise RuntimeError("injected after native CommitTransaction success")
        return committed

    monkeypatch.setattr(
        config_module,
        "_commit_windows_transaction",
        commit_success_then_throw,
    )

    with pytest.raises(ConfigPostCommitError) as caught:
        apply_managed_config(config_path, catalog_path)

    error = caught.value
    assert commit_results == [True, True]
    assert error.committed is True
    assert error.backup_path is not None
    assert error.backup_path.read_bytes() == original
    assert b'model_provider = "example-provider"' in config_path.read_bytes()


def test_windows_rollback_failure_retains_backup_and_status(tmp_path, monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows transaction rollback is Windows-specific")

    import codex_model_switcher.config as config_module

    config_path = tmp_path / "config.toml"
    original = b"# original bytes\r\n"
    config_path.write_bytes(original)
    catalog_path = tmp_path / "candidate.json"
    managed_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "{}"',
            MANAGED_END,
        )
    )
    monkeypatch.setattr(
        config_module,
        "render_managed_config",
        lambda _path, *, verification=None: managed_block,
    )
    original_commit = config_module._commit_windows_transaction
    commit_calls = [0]

    def fail_target_commit(commit_transaction, transaction_handle):
        commit_calls[0] += 1
        if commit_calls[0] == 2:
            return False
        return original_commit(commit_transaction, transaction_handle)

    monkeypatch.setattr(
        config_module,
        "_commit_windows_transaction",
        fail_target_commit,
    )
    monkeypatch.setattr(
        config_module,
        "_rollback_windows_transaction",
        lambda _rollback_transaction, _transaction_handle: False,
    )

    with pytest.raises(ConfigTransactionStateError) as caught:
        apply_managed_config(config_path, catalog_path)

    error = caught.value
    assert commit_calls == [2]
    assert error.committed is False
    assert error.state_uncertain is True
    assert error.backup_path is not None
    assert error.backup_path.read_bytes() == original
    assert config_path.read_bytes() == original


def test_windows_precommit_handle_close_failures_retain_backup_and_status(
    tmp_path, monkeypatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows pre-commit handle cleanup is Windows-specific")

    import codex_model_switcher.config as config_module

    config_path = tmp_path / "config.toml"
    original = b"# original bytes\r\n"
    config_path.write_bytes(original)
    catalog_path = tmp_path / "candidate.json"
    managed_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "{}"',
            MANAGED_END,
        )
    )
    monkeypatch.setattr(
        config_module,
        "render_managed_config",
        lambda _path, *, verification=None: managed_block,
    )
    original_commit = config_module._commit_windows_transaction
    commit_calls = [0]

    def fail_target_commit(commit_transaction, transaction_handle):
        commit_calls[0] += 1
        if commit_calls[0] == 2:
            return False
        return original_commit(commit_transaction, transaction_handle)

    monkeypatch.setattr(
        config_module,
        "_commit_windows_transaction",
        fail_target_commit,
    )
    monkeypatch.setattr(
        config_module,
        "_rollback_windows_transaction",
        lambda _rollback_transaction, _transaction_handle: True,
    )
    close_calls = [0]

    def fail_two_precommit_closes(close_handle, handle):
        close_calls[0] += 1
        if close_calls[0] <= 2:
            return False
        return bool(close_handle(handle))

    monkeypatch.setattr(
        config_module,
        "_close_precommit_handle",
        fail_two_precommit_closes,
    )

    with pytest.raises(ConfigTransactionStateError) as caught:
        apply_managed_config(config_path, catalog_path)

    error = caught.value
    assert commit_calls == [2]
    assert close_calls == [2]
    assert error.committed is False
    assert error.state_uncertain is True
    assert error.backup_path is not None
    assert error.backup_path.read_bytes() == original
    assert config_path.read_bytes() == original


def test_windows_precommit_lease_close_failure_retains_backup_and_status(
    tmp_path, monkeypatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows lease cleanup is Windows-specific")

    import codex_model_switcher.config as config_module

    config_path = tmp_path / "config.toml"
    original = b"# original bytes\r\n"
    config_path.write_bytes(original)
    catalog_path = tmp_path / "candidate.json"
    managed_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "{}"',
            MANAGED_END,
        )
    )
    monkeypatch.setattr(
        config_module,
        "render_managed_config",
        lambda _path, *, verification=None: managed_block,
    )
    real_replace_temp_file = config_module._replace_temp_file
    real_release = config_module._PathLease._release_windows_handle
    injected = [False]

    def fail_config_replacement(temporary_path, target_path, *, lease=None, expected=None):
        if target_path.resolve() == config_path.resolve():
            raise ConfigError("injected pre-commit replacement failure")
        return real_replace_temp_file(
            temporary_path,
            target_path,
            lease=lease,
            expected=expected,
        )

    def fail_main_lease_close(self, kernel32, handle, overlapped):
        if (
            self.path == config_path.resolve()
            and not self._replacement_committed
            and not injected[0]
        ):
            injected[0] = True
            real_release(self, kernel32, handle, overlapped)
            return 123
        return real_release(self, kernel32, handle, overlapped)

    monkeypatch.setattr(config_module, "_replace_temp_file", fail_config_replacement)
    monkeypatch.setattr(
        config_module._PathLease,
        "_release_windows_handle",
        fail_main_lease_close,
    )

    with pytest.raises(ConfigTransactionStateError) as caught:
        apply_managed_config(config_path, catalog_path)

    error = caught.value
    assert injected == [True]
    assert error.committed is False
    assert error.state_uncertain is True
    assert error.backup_path is not None
    assert error.backup_path.read_bytes() == original
    assert config_path.read_bytes() == original


def test_windows_lock_acquisition_cleanup_uncertainty_is_state_error(
    tmp_path, monkeypatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows lock acquisition is Windows-specific")

    import codex_model_switcher.config as config_module

    config_path = tmp_path / "new-config.toml"
    real_close = config_module._close_windows_handle
    real_unlink = config_module.Path.unlink

    def close_then_report_failure(close_handle, handle):
        real_close(close_handle, handle)
        return False

    def fail_rollback_unlink(path, *args, **kwargs):
        if Path(path).resolve() == config_path.resolve():
            raise OSError("injected rollback unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(config_module, "_lock_windows_file", lambda *args: False)
    monkeypatch.setattr(
        config_module,
        "_close_windows_handle",
        close_then_report_failure,
    )
    monkeypatch.setattr(config_module.Path, "unlink", fail_rollback_unlink)

    lease = config_module._PathLease(config_path, create=True)
    with pytest.raises(ConfigTransactionStateError, match="close|rollback") as caught:
        lease._acquire_windows()

    error = caught.value
    assert error.committed is False
    assert error.state_uncertain is True
    assert config_path.exists()
    assert "LockFileEx" in str(error)


def test_apply_rejects_concurrent_edit_before_replace(tmp_path, monkeypatch) -> None:
    if os.name == "nt":
        pytest.skip("Windows target locking is covered by the OS-level probe")

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

    def racing_atomic_write(path, data, *, expected=None, lease=None):
        if expected is not None and path.resolve() == config_path.resolve():
            config_path.write_bytes(external)
        if expected is None:
            return real_atomic_write(path, data)
        return real_atomic_write(path, data, expected=expected, lease=lease)

    monkeypatch.setattr(config_module, "_atomic_write", racing_atomic_write)

    with pytest.raises(ConfigChangedError, match="changed"):
        apply_managed_config(config_path, catalog_path)

    assert config_path.read_bytes() == external


def test_windows_apply_lock_blocks_external_replace_probe(tmp_path, monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("the OS-level replacement probe is Windows-specific")

    import codex_model_switcher.config as config_module

    config_dir = tmp_path / "codex-home"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    config_path.write_bytes(b'model = "original"\n')
    catalog_path = tmp_path / "candidate.json"
    external_path = tmp_path / "external.toml"
    managed_block = "\n".join(
        (
            MANAGED_START,
            'model_provider = "example-provider"',
            'model_catalog_json = "{}"',
            MANAGED_END,
        )
    )
    blocked: list[str] = []
    real_replace_temp_file = config_module._replace_temp_file

    monkeypatch.setattr(
        config_module,
        "render_managed_config",
        lambda _path, *, verification=None: managed_block,
    )

    def probe_external_replace(temporary_path, target_path, *, lease=None, expected=None):
        if target_path.resolve() == config_path.resolve():
            external_path.write_bytes(b"user-edit\n")
            _assert_child_replace_is_blocked(external_path, target_path)
            blocked.append("before")
        result = real_replace_temp_file(
            temporary_path,
            target_path,
            lease=lease,
            expected=expected,
        )
        if target_path.resolve() == config_path.resolve():
            external_path.write_bytes(b"user-edit-after-replace\n")
            _assert_child_replace_is_blocked(external_path, target_path)
            blocked.append("after")
        return result

    monkeypatch.setattr(config_module, "_replace_temp_file", probe_external_replace)

    apply_managed_config(config_path, catalog_path)

    assert blocked == ["before", "after"]
    assert b'model_provider = "example-provider"' in config_path.read_bytes()


def test_windows_transaction_commit_handoff_blocks_external_replace(
    tmp_path, monkeypatch
) -> None:
    if os.name != "nt":
        pytest.skip("the OS-level replacement probe is Windows-specific")

    import codex_model_switcher.config as config_module

    config_dir = tmp_path / "codex-home"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    config_path.write_bytes(b"old\n")
    temporary_path = config_dir / ".candidate.tmp"
    temporary_path.write_bytes(b"new\n")
    external_path = tmp_path / "external.toml"
    original_commit = config_module._commit_windows_transaction
    handoff_checked: list[bool] = []

    def commit_then_probe(commit_transaction, transaction_handle):
        committed = original_commit(commit_transaction, transaction_handle)
        external_path.write_bytes(b"user-edit-at-commit\n")
        _assert_child_replace_is_blocked(external_path, config_path)
        handoff_checked.append(committed)
        return committed

    monkeypatch.setattr(
        config_module,
        "_commit_windows_transaction",
        commit_then_probe,
    )

    with config_module._exclusive_path_lock(config_path, create=False) as lease:
        config_module._replace_temp_file(
            temporary_path,
            config_path,
            lease=lease,
            expected=b"new\n",
        )

    assert handoff_checked == [True]
    assert config_path.read_bytes() == b"new\n"


def test_windows_restore_lock_blocks_external_replace_probe(tmp_path, monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("the OS-level replacement probe is Windows-specific")

    import codex_model_switcher.config as config_module

    config_dir = tmp_path / "codex-home"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    original = b'model = "original"\n'
    written = b'model = "managed"\n'
    config_path.write_bytes(written)
    backup_path = tmp_path / "config.toml.bak"
    backup_path.write_bytes(original)
    receipt = ConfigReceipt(
        config_path=config_path.resolve(),
        backup_path=backup_path,
        original_hash=hashlib.sha256(original).hexdigest(),
        written_hash=hashlib.sha256(written).hexdigest(),
        timestamp="20260805T000000000000Z",
    )
    external_path = tmp_path / "external.toml"
    blocked: list[str] = []
    real_replace_temp_file = config_module._replace_temp_file

    def probe_external_replace(temporary_path, target_path, *, lease=None, expected=None):
        if target_path.resolve() == config_path.resolve():
            external_path.write_bytes(b"user-edit\n")
            _assert_child_replace_is_blocked(external_path, target_path)
            blocked.append("before")
        result = real_replace_temp_file(
            temporary_path,
            target_path,
            lease=lease,
            expected=expected,
        )
        if target_path.resolve() == config_path.resolve():
            external_path.write_bytes(b"user-edit-after-replace\n")
            _assert_child_replace_is_blocked(external_path, target_path)
            blocked.append("after")
        return result

    monkeypatch.setattr(config_module, "_replace_temp_file", probe_external_replace)

    restore_managed_config(config_path, receipt)

    assert blocked == ["before", "after"]
    assert config_path.read_bytes() == original


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
