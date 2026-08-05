"""Atomic, byte-preserving management of the project's config block."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .catalog import CatalogValidationError, load_catalog

MANAGED_START = "# >>> codex-model-switcher managed start"
MANAGED_END = "# <<< codex-model-switcher managed end"


class ConfigError(RuntimeError):
    """Raised when a managed config cannot be safely applied or restored."""


class ConfigChangedError(ConfigError):
    """Raised when the target changed after this project wrote it."""


@dataclass(frozen=True)
class ConfigReceipt:
    config_path: Path
    backup_path: Path
    original_hash: str
    written_hash: str
    timestamp: str


def render_managed_config(catalog_path: Path) -> str:
    """Render only provider and catalog fields; no endpoint or credentials."""

    try:
        catalog = load_catalog(Path(catalog_path))
    except CatalogValidationError as error:
        raise ConfigError(str(error)) from error
    catalog_json = json.dumps(catalog.to_mapping(), ensure_ascii=False, separators=(",", ":"))
    return "\n".join(
        (
            MANAGED_START,
            f"model_provider = {json.dumps(catalog.provider_id, ensure_ascii=False)}",
            f"model_catalog_json = {json.dumps(catalog_json, ensure_ascii=False)}",
            MANAGED_END,
        )
    )


def apply_managed_config(config_path: Path, catalog_path: Path) -> ConfigReceipt:
    config_path = Path(config_path).resolve()
    catalog_path = Path(catalog_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        original = config_path.read_bytes() if config_path.exists() else b""
        original_text = original.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError("config must be readable UTF-8 bytes") from error

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = config_path.with_name(f"{config_path.name}.bak.{timestamp}")
    _atomic_write(backup_path, original)
    try:
        managed_block = render_managed_config(catalog_path)
        rendered = _replace_or_append_managed_block(original_text, managed_block)
        written = rendered.encode("utf-8")
        _atomic_write(config_path, written)
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return ConfigReceipt(
        config_path=config_path,
        backup_path=backup_path,
        original_hash=_sha256(original),
        written_hash=_sha256(written),
        timestamp=timestamp,
    )


def restore_managed_config(config_path: Path, receipt: ConfigReceipt) -> None:
    config_path = Path(config_path).resolve()
    if config_path != receipt.config_path:
        raise ConfigError("receipt belongs to a different config path")
    try:
        current = config_path.read_bytes()
        backup = receipt.backup_path.read_bytes()
    except OSError as error:
        raise ConfigError("config or its backup is unavailable") from error
    if _sha256(current) != receipt.written_hash:
        raise ConfigChangedError("config changed after this project wrote it; refusing restore")
    if _sha256(backup) != receipt.original_hash:
        raise ConfigError("backup hash does not match the apply receipt")
    _atomic_write(config_path, backup)


def _replace_or_append_managed_block(original: str, block: str) -> str:
    start = original.find(MANAGED_START)
    end = original.find(MANAGED_END)
    if (start == -1) != (end == -1):
        raise ConfigError("managed config block is incomplete")
    if start != -1:
        if end < start:
            raise ConfigError("managed config block markers are out of order")
        start_line = original.rfind("\n", 0, start) + 1
        end_line_end = original.find("\n", end)
        if end_line_end == -1:
            newline = ""
            after = ""
        else:
            newline_start = (
                end_line_end - 1
                if end_line_end > 0 and original[end_line_end - 1] == "\r"
                else end_line_end
            )
            newline = original[newline_start : end_line_end + 1]
            after = original[end_line_end + 1 :]
        return original[:start_line] + block + newline + after

    separator = "" if not original or original.endswith(("\n", "\r")) else "\n"
    return original + separator + block + "\n"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
