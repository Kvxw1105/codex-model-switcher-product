"""Durable, minimal state owned by the local protocol router.

Codex remains authoritative for tasks, transcripts, and compaction.  The
database below stores only routing links, encrypted continuation fragments,
configuration receipts, and cancellation metadata needed by an adapter.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Callable, TypeVar

from .crypto import FernetCipher, SecretKeyProvider


@dataclass(frozen=True, slots=True)
class _ColumnSpec:
    sqlite_type: str
    not_null: bool = False
    primary_key: bool = False
    identifier: bool = False
    timestamp: bool = False


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    exists: bool
    identity: tuple[int, int] | None
    size: int | None
    digest: bytes | None


SCHEMA_VERSION = 3
MAX_IDENTIFIER_LENGTH = 512
MAX_CONTEXT_FRAGMENT_CHARS = 64 * 1024
SNAPSHOT_COPY_ATTEMPTS = 4
SNAPSHOT_COPY_DELAY_SECONDS = 0.01
SQLITE_HEADER = b"SQLite format 3\x00"
SHM_MAGIC = b"\x18\xe2\x2d\x00"
SHM_PAGE_SIZE = 32 * 1024
V1_SCHEMA_COLUMN_SPECS = {
    "route_selections": {
        "selection_id": _ColumnSpec("INTEGER", primary_key=True),
        "codex_task_id": _ColumnSpec("TEXT", not_null=True, identifier=True),
        "turn_id": _ColumnSpec("TEXT", not_null=True, identifier=True),
        "route_id": _ColumnSpec("TEXT", not_null=True, identifier=True),
        "selected_at": _ColumnSpec("INTEGER", not_null=True, timestamp=True),
    },
    "response_links": {
        "local_response_id": _ColumnSpec("TEXT", primary_key=True, identifier=True),
        "upstream_response_id": _ColumnSpec("TEXT", not_null=True, identifier=True),
        "route_id": _ColumnSpec("TEXT", not_null=True, identifier=True),
        "created_at": _ColumnSpec("INTEGER", not_null=True, timestamp=True),
    },
    "context_fragments": {
        "fragment_id": _ColumnSpec("TEXT", primary_key=True, identifier=True),
        "scope_id": _ColumnSpec("TEXT", not_null=True, identifier=True),
        "ciphertext": _ColumnSpec("BLOB", not_null=True),
        "created_at": _ColumnSpec("INTEGER", not_null=True, timestamp=True),
    },
    "config_receipts": {
        "receipt_id": _ColumnSpec("TEXT", primary_key=True, identifier=True),
        "config_sha256": _ColumnSpec("TEXT", not_null=True),
        "created_at": _ColumnSpec("INTEGER", not_null=True, timestamp=True),
    },
    "cancel_handles": {
        "handle_id": _ColumnSpec("TEXT", primary_key=True, identifier=True),
        "codex_task_id": _ColumnSpec("TEXT", not_null=True, identifier=True),
        "route_id": _ColumnSpec("TEXT", not_null=True, identifier=True),
        "created_at": _ColumnSpec("INTEGER", not_null=True, timestamp=True),
    },
}
V2_SCHEMA_COLUMN_SPECS = {
    "route_selections": dict(V1_SCHEMA_COLUMN_SPECS["route_selections"]),
    "response_links": {
        **V1_SCHEMA_COLUMN_SPECS["response_links"],
        "codex_task_id": _ColumnSpec("TEXT", identifier=True),
        "expires_at": _ColumnSpec("INTEGER", timestamp=True),
    },
    "context_fragments": {
        **V1_SCHEMA_COLUMN_SPECS["context_fragments"],
        "expires_at": _ColumnSpec("INTEGER", timestamp=True),
    },
    "config_receipts": dict(V1_SCHEMA_COLUMN_SPECS["config_receipts"]),
    "cancel_handles": {
        **V1_SCHEMA_COLUMN_SPECS["cancel_handles"],
        "expires_at": _ColumnSpec("INTEGER", timestamp=True),
    },
    "compact_boundaries": {
        "codex_task_id": _ColumnSpec("TEXT", primary_key=True, identifier=True),
        "boundary_response_id": _ColumnSpec("TEXT", not_null=True, identifier=True),
        "boundary_created_at": _ColumnSpec(
            "INTEGER", not_null=True, timestamp=True
        ),
    },
}
SCHEMA_COLUMN_SPECS = {
    "route_selections": dict(V2_SCHEMA_COLUMN_SPECS["route_selections"]),
    "response_links": {
        **V2_SCHEMA_COLUMN_SPECS["response_links"],
        "event_sequence": _ColumnSpec("INTEGER", not_null=True),
    },
    "context_fragments": {
        **V2_SCHEMA_COLUMN_SPECS["context_fragments"],
        "event_sequence": _ColumnSpec("INTEGER", not_null=True),
    },
    "config_receipts": dict(V2_SCHEMA_COLUMN_SPECS["config_receipts"]),
    "cancel_handles": dict(V2_SCHEMA_COLUMN_SPECS["cancel_handles"]),
    "compact_boundaries": {
        **V2_SCHEMA_COLUMN_SPECS["compact_boundaries"],
        "boundary_event_sequence": _ColumnSpec("INTEGER", not_null=True),
    },
    "event_counters": {
        "counter_name": _ColumnSpec("TEXT", primary_key=True, identifier=True),
        "value": _ColumnSpec("INTEGER", not_null=True),
    },
}
REQUIRED_SCHEMA_COLUMNS = {
    table: set(columns) for table, columns in SCHEMA_COLUMN_SPECS.items()
}
REQUIRED_V2_SCHEMA_COLUMNS = {
    table: set(columns) for table, columns in V2_SCHEMA_COLUMN_SPECS.items()
}
V1_TIMESTAMP_COLUMNS = {
    "route_selections": "selected_at",
    "response_links": "created_at",
    "context_fragments": "created_at",
    "config_receipts": "created_at",
    "cancel_handles": "created_at",
}
V2_TIMESTAMP_COLUMNS = {
    **V1_TIMESTAMP_COLUMNS,
    "compact_boundaries": "boundary_created_at",
}
T = TypeVar("T")
_DATABASE_INIT_LOCK = RLock()


class StateError(Exception):
    """Base error for durable router state."""


class DatabaseCorruptionError(StateError):
    """Raised when an existing database cannot be safely opened."""


class DatabaseSnapshotError(DatabaseCorruptionError):
    """Raised when a consistent database snapshot cannot be captured."""


class DatabaseQuarantineError(DatabaseCorruptionError):
    """Raised when quarantine could not preserve the complete evidence set."""

    def __init__(
        self,
        quarantine: Path,
        copied_suffixes: tuple[str, ...],
        failed_suffixes: tuple[str, ...],
    ) -> None:
        self.quarantine = quarantine
        self.copied_suffixes = copied_suffixes
        self.failed_suffixes = failed_suffixes
        copied = ",".join(copied_suffixes) or "none"
        failed = ",".join(failed_suffixes) or "none"
        super().__init__(
            "state database quarantine incomplete during snapshot/evidence preservation; "
            f"quarantine={quarantine.name}; copied={copied}; failed={failed}"
        )


class DatabaseSchemaError(DatabaseCorruptionError):
    """Raised when the database schema is incomplete or inconsistent."""


class UnsupportedSchemaError(StateError):
    """Raised when a database was created by a newer implementation."""


class StateNotFoundError(StateError):
    """Raised when a required compact boundary is absent."""


@dataclass(frozen=True, slots=True)
class ResponseLink:
    local_response_id: str
    upstream_id: str
    route_id: str
    codex_task_id: str | None
    created_at: int
    expires_at: int | None
    event_sequence: int

    @property
    def upstream_response_id(self) -> str:
        return self.upstream_id


@dataclass(frozen=True, slots=True)
class RouteSelection:
    codex_task_id: str
    turn_id: str
    route_id: str
    selected_at: int


@dataclass(frozen=True, slots=True)
class ConfigReceipt:
    receipt_id: str
    config_sha256: str
    created_at: int


@dataclass(frozen=True, slots=True)
class CancelHandleMetadata:
    handle_id: str
    codex_task_id: str
    route_id: str
    created_at: int
    expires_at: int | None


def _timestamp() -> int:
    return time.time_ns()


def _timestamp_value(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be an integer timestamp")
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{field} must be a non-empty short identifier")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL")
    return value


def _optional_expiry(value: int | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("expires_at must be an integer timestamp or None")
    if value < 0:
        raise ValueError("expires_at must be non-negative")
    return value


class StateStore:
    """SQLite-backed state store with one transaction per mutation."""

    def __init__(self, path: str | Path, secret_key_provider: SecretKeyProvider) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        self._cipher = FernetCipher(secret_key_provider)

        with _DATABASE_INIT_LOCK:
            existed = self.path.exists()
            if existed:
                self._validate_existing_path()
                self._verify_existing_before_write()
            else:
                self._reject_orphaned_sidecars()

            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not existed and self.path.exists():
                existed = True
                self._validate_existing_path()
                self._verify_existing_before_write()
            elif not existed:
                self._reject_orphaned_sidecars()

            try:
                self._connection = sqlite3.connect(
                    self.path,
                    timeout=30.0,
                    isolation_level=None,
                    check_same_thread=False,
                )
                self._configure_connection()
                self._verify_integrity()
                self._migrate()
                self._validate_schema(self._require_connection())
            except DatabaseCorruptionError as exc:
                self.close()
                if existed:
                    quarantine = self._quarantine_existing()
                    raise DatabaseCorruptionError(
                        "existing state database schema validation failed; "
                        f"quarantined as {quarantine.name}"
                    ) from exc
                raise
            except UnsupportedSchemaError:
                self.close()
                raise
            except (OSError, sqlite3.DatabaseError) as exc:
                self.close()
                if existed:
                    quarantine = self._quarantine_existing()
                    raise DatabaseCorruptionError(
                        "existing state database failed integrity checks; "
                        f"quarantined as {quarantine.name}"
                    ) from exc
                raise DatabaseCorruptionError(
                    "new state database could not be initialized"
                ) from exc

    def _validate_existing_path(self) -> None:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            quarantine = self._quarantine_existing()
            raise DatabaseCorruptionError(
                "existing state database is empty or not a file; "
                f"quarantined as {quarantine.name}"
            )

    def _reject_orphaned_sidecars(self) -> None:
        if self.path.exists():
            return
        if not any(
            self.path.with_name(f"{self.path.name}{suffix}").exists()
            for suffix in ("-wal", "-shm")
        ):
            return
        quarantine = self._quarantine_existing()
        raise DatabaseCorruptionError(
            "orphaned SQLite sidecar exists without the main database; "
            f"quarantined as {quarantine.name}"
        )

    @staticmethod
    def _fingerprint_file(path: Path) -> _FileFingerprint:
        try:
            before = path.stat()
        except FileNotFoundError:
            return _FileFingerprint(False, None, None, None)

        digest = sha256()
        try:
            with path.open("rb") as database_file:
                for chunk in iter(lambda: database_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = path.stat()
        except FileNotFoundError as exc:
            raise DatabaseSnapshotError(
                f"state database file disappeared while fingerprinting {path.name}"
            ) from exc
        if (
            (before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
        ):
            raise DatabaseSnapshotError(
                f"state database file changed while fingerprinting {path.name}"
            )
        return _FileFingerprint(
            True,
            (int(after.st_dev), int(after.st_ino)),
            int(after.st_size),
            digest.digest(),
        )

    def _capture_database_set_fingerprints(self) -> dict[str, _FileFingerprint]:
        return {
            suffix: self._fingerprint_file(
                self.path
                if not suffix
                else self.path.with_name(f"{self.path.name}{suffix}")
            )
            for suffix in ("", "-wal", "-shm")
        }

    @staticmethod
    def _file_identity_and_size(
        path: Path,
    ) -> tuple[bool, tuple[int, int] | None, int | None]:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False, None, None
        return True, (int(stat.st_dev), int(stat.st_ino)), int(stat.st_size)

    def _capture_initial_database_state(
        self,
    ) -> dict[str, _FileFingerprint]:
        for _attempt in range(SNAPSHOT_COPY_ATTEMPTS):
            before = self._capture_database_set_fingerprints()
            after = self._capture_database_set_fingerprints()
            if before != after:
                time.sleep(SNAPSHOT_COPY_DELAY_SECONDS)
                continue
            return before
        raise DatabaseSnapshotError(
            "state database files changed while capturing the initial evidence point"
        )

    def _capture_database_set_evidence(
        self,
    ) -> tuple[dict[str, _FileFingerprint], dict[str, bytes]]:
        for _attempt in range(SNAPSHOT_COPY_ATTEMPTS):
            before = self._capture_database_set_fingerprints()
            evidence: dict[str, bytes] = {}
            try:
                for suffix, fingerprint in before.items():
                    if not fingerprint.exists:
                        continue
                    path = self.path if not suffix else self.path.with_name(
                        f"{self.path.name}{suffix}"
                    )
                    evidence[suffix] = path.read_bytes()
            except OSError:
                time.sleep(SNAPSHOT_COPY_DELAY_SECONDS)
                continue
            if any(
                sha256(evidence[suffix]).digest() != fingerprint.digest
                for suffix, fingerprint in before.items()
                if fingerprint.exists
            ):
                time.sleep(SNAPSHOT_COPY_DELAY_SECONDS)
                continue
            return before, evidence
        raise DatabaseSnapshotError(
            "state database files changed while capturing quarantine evidence"
        )

    def _write_database_set_evidence(
        self, destination: Path, evidence: dict[str, bytes]
    ) -> None:
        for suffix, content in evidence.items():
            path = destination / self.path.name
            if suffix:
                path = path.with_name(f"{path.name}{suffix}")
            path.write_bytes(content)

    def _verify_existing_before_write(self) -> None:
        source: sqlite3.Connection | None = None
        snapshot: sqlite3.Connection | None = None
        source_preflight_started = False
        initial_fingerprints: dict[str, _FileFingerprint] | None = None
        with TemporaryDirectory(prefix=".state-preflight-", dir=self.path.parent) as directory:
            snapshot_dir = Path(directory) / "snapshot"
            evidence_dir = Path(directory) / "evidence"
            snapshot_dir.mkdir()
            evidence_dir.mkdir()
            try:
                try:
                    initial_fingerprints = self._capture_initial_database_state()
                except (OSError, DatabaseSnapshotError):
                    initial_fingerprints = None
                source_path = self.path.resolve()
                self._validate_database_sidecars(source_path.parent)
                with source_path.open("rb") as database_file:
                    if database_file.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                        raise DatabaseSnapshotError("state database header is invalid")
                read_only_uri = f"{source_path.as_uri()}?mode=ro"
                source = sqlite3.connect(read_only_uri, uri=True, timeout=30.0)
                source_preflight_started = True
                source.execute("BEGIN")
                result = source.execute("PRAGMA integrity_check").fetchone()
                if not result or str(result[0]).lower() != "ok":
                    raise sqlite3.DatabaseError("state database integrity check failed")
                snapshot_version = int(source.execute("PRAGMA user_version").fetchone()[0])
                if snapshot_version in (0, 1):
                    self._validate_legacy_schema(source)
                elif snapshot_version == 2:
                    self._validate_v2_schema(source)
                elif snapshot_version == SCHEMA_VERSION:
                    self._validate_schema(source)
                else:
                    raise UnsupportedSchemaError(
                        f"state database schema {snapshot_version} is unsupported"
                    )
                self._copy_database_set(evidence_dir)
                snapshot_path = snapshot_dir / self.path.name
                snapshot = sqlite3.connect(snapshot_path)
                source.backup(snapshot, pages=0, sleep=SNAPSHOT_COPY_DELAY_SECONDS)
                snapshot.close()
                snapshot = None
                source.close()
                source = None
            except (OSError, sqlite3.Error, DatabaseCorruptionError) as original_error:
                failure = original_error
                if snapshot is not None:
                    with suppress(sqlite3.Error):
                        snapshot.close()
                    snapshot = None
                if source is not None:
                    with suppress(sqlite3.Error):
                        source.rollback()
                    with suppress(sqlite3.Error):
                        source.close()
                    source = None
                preflight_fingerprints: dict[str, _FileFingerprint] | None = None
                preflight_evidence: dict[str, bytes] | None = None
                if source_preflight_started:
                    try:
                        preflight_fingerprints, preflight_evidence = (
                            self._capture_database_set_evidence()
                        )
                    except (OSError, DatabaseSnapshotError) as fingerprint_error:
                        failure = DatabaseSnapshotError(
                            "state database files could not be fingerprinted after "
                            "read-only preflight"
                        )
                        failure.__cause__ = fingerprint_error

                recovery_lock: sqlite3.Connection | None = None
                quarantine_source_directory: Path | None = None
                try:
                    if preflight_evidence is not None:
                        self._write_database_set_evidence(evidence_dir, preflight_evidence)
                        quarantine_source_directory = evidence_dir
                    if (
                        source_preflight_started
                        and preflight_fingerprints is not None
                    ):
                        try:
                            recovery_lock = sqlite3.connect(
                                self.path,
                                timeout=30.0,
                                isolation_level=None,
                                check_same_thread=False,
                            )
                            recovery_lock.execute("BEGIN IMMEDIATE")
                            locked_main_wal = {
                                suffix: self._fingerprint_file(
                                    self.path
                                    if not suffix
                                    else self.path.with_name(f"{self.path.name}{suffix}")
                                )
                                for suffix in ("", "-wal")
                            }
                            shm_path = self.path.with_name(f"{self.path.name}-shm")
                            # The writer lock can hold the WAL-index against raw
                            # reads on Windows.  The SHM digest was verified when
                            # preflight_evidence was captured; at the lock point
                            # compare identity/size and never write an old index.
                            locked_shm = self._file_identity_and_size(shm_path)
                            main_wal_unchanged = (
                                initial_fingerprints is None
                                or all(
                                    initial_fingerprints[suffix]
                                    == locked_main_wal[suffix]
                                    for suffix in ("", "-wal")
                                )
                            )
                            live_main_wal_unchanged = all(
                                preflight_fingerprints[suffix] == locked_main_wal[suffix]
                                for suffix in ("", "-wal")
                            )
                            live_shm_identity_unchanged = (
                                preflight_fingerprints["-shm"].exists == locked_shm[0]
                                and preflight_fingerprints["-shm"].identity
                                == locked_shm[1]
                                and preflight_fingerprints["-shm"].size == locked_shm[2]
                            )
                            live_set_unchanged = (
                                live_main_wal_unchanged
                                and live_shm_identity_unchanged
                            )
                            if not live_set_unchanged:
                                failure = DatabaseSnapshotError(
                                    "state database changed during preflight; "
                                    "live evidence was preserved"
                                )
                                quarantine_source_directory = None
                            elif not main_wal_unchanged:
                                failure = DatabaseSnapshotError(
                                    "state database changed during preflight; "
                                    "live evidence was preserved"
                                )
                            if not live_main_wal_unchanged:
                                quarantine_source_directory = None
                        except (
                            OSError,
                            sqlite3.Error,
                            DatabaseSnapshotError,
                        ) as recovery_error:
                            failure = DatabaseSnapshotError(
                                "state database writer lock/fingerprint validation failed; "
                                "live evidence was preserved"
                            )
                            failure.__cause__ = recovery_error
                    quarantine = self._quarantine_existing(quarantine_source_directory)
                finally:
                    if recovery_lock is not None:
                        with suppress(sqlite3.DatabaseError):
                            recovery_lock.rollback()
                        with suppress(sqlite3.DatabaseError):
                            recovery_lock.close()
                raise DatabaseCorruptionError(
                    "existing state database snapshot/schema validation failed: "
                    f"{failure}; "
                    f"quarantined as {quarantine.name}"
                ) from failure
            finally:
                if snapshot is not None:
                    with suppress(sqlite3.Error):
                        snapshot.close()
                if source is not None:
                    with suppress(sqlite3.Error):
                        source.close()

    def _copy_database_set(self, destination: Path) -> None:
        failures: dict[str, DatabaseSnapshotError] = {}
        for sidecar_suffix in ("", "-wal", "-shm"):
            source = self.path if not sidecar_suffix else self.path.with_name(
                f"{self.path.name}{sidecar_suffix}"
            )
            if source.exists():
                try:
                    self._copy_file_with_retry(source, destination / source.name)
                except DatabaseSnapshotError as exc:
                    failures[sidecar_suffix or "main"] = exc
        if failures:
            failed = ",".join(failures)
            raise DatabaseSnapshotError(f"snapshot copy failed for: {failed}") from next(
                iter(failures.values())
            )

    def _validate_database_sidecars(self, directory: Path) -> None:
        shm_path = directory / f"{self.path.name}-shm"
        if not shm_path.exists():
            return
        main_path = directory / self.path.name
        with main_path.open("rb") as database_file:
            header = database_file.read(20)
        if len(header) < 20 or header[: len(SQLITE_HEADER)] != SQLITE_HEADER:
            raise DatabaseSnapshotError("state database header is invalid")
        if header[18] != 2:
            raise DatabaseSnapshotError("state database has an invalid shm sidecar")
        try:
            shm_size = shm_path.stat().st_size
            with shm_path.open("rb") as shm_file:
                shm_header = shm_file.read(52)
        except OSError as exc:
            raise DatabaseSnapshotError("state database shm sidecar cannot be read") from exc
        database_page_size = int.from_bytes(header[16:18], "big")
        if database_page_size == 1:
            database_page_size = 65536
        shm_page_size = int.from_bytes(shm_header[14:16], "little")
        if (
            shm_size < SHM_PAGE_SIZE
            or shm_size % SHM_PAGE_SIZE != 0
            or len(shm_header) < 52
            or shm_header[:4] != SHM_MAGIC
            or shm_header[48:52] != SHM_MAGIC
            or shm_header[12] != 1
            or shm_page_size != database_page_size
        ):
            raise DatabaseSnapshotError("state database has an invalid shm sidecar")

    def _clone_database_set(self, source_directory: Path, destination: Path) -> None:
        for sidecar_suffix in ("", "-wal", "-shm"):
            source = source_directory / self.path.name
            if sidecar_suffix:
                source = source.with_name(f"{source.name}{sidecar_suffix}")
            if source.exists():
                shutil.copyfile(source, destination / source.name)

    @staticmethod
    def _copy_file_with_retry(source: Path, destination: Path) -> None:
        for attempt in range(SNAPSHOT_COPY_ATTEMPTS):
            try:
                shutil.copy2(source, destination)
                return
            except PermissionError as exc:
                if attempt + 1 == SNAPSHOT_COPY_ATTEMPTS:
                    raise DatabaseSnapshotError(
                        f"snapshot copy failed after retries for {source.name}"
                    ) from exc
                time.sleep(SNAPSHOT_COPY_DELAY_SECONDS * (attempt + 1))
            except FileNotFoundError as exc:
                if source.name.endswith(("-wal", "-shm")) and not source.exists():
                    return
                raise DatabaseSnapshotError(
                    f"snapshot source disappeared for {source.name}"
                ) from exc
            except OSError as exc:
                raise DatabaseSnapshotError(
                    f"snapshot copy failed for {source.name}"
                ) from exc

    def _configure_connection(self) -> None:
        connection = self._require_connection()
        connection.execute("PRAGMA busy_timeout = 30000")
        journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            raise sqlite3.DatabaseError("WAL mode was not enabled")
        connection.execute("PRAGMA foreign_keys = ON")

    def _verify_integrity(self) -> None:
        result = self._require_connection().execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise sqlite3.DatabaseError("state database integrity check failed")

    def _migrate(self) -> None:
        connection = self._require_connection()
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise UnsupportedSchemaError(
                        f"state database schema {version} is newer than supported "
                        f"schema {SCHEMA_VERSION}"
                    )
                while version < SCHEMA_VERSION:
                    if version == 0:
                        self._migrate_zero_to_one(connection)
                        version = 1
                    elif version == 1:
                        self._migrate_one_to_two(connection)
                        version = 2
                    elif version == 2:
                        self._migrate_two_to_three(connection)
                        version = 3
                    else:
                        raise UnsupportedSchemaError(f"unsupported state database schema {version}")
                    connection.execute(f"PRAGMA user_version = {version}")
                connection.execute("COMMIT")
            except BaseException:
                with suppress(sqlite3.DatabaseError):
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise DatabaseSchemaError(
                f"state database schema version {version} is not {SCHEMA_VERSION}"
            )
        StateStore._validate_declared_schema(
            connection, SCHEMA_COLUMN_SPECS, "state database"
        )
        StateStore._validate_schema_values(connection, SCHEMA_COLUMN_SPECS, "state database")
        StateStore._validate_config_hashes(connection)
        StateStore._validate_event_state(connection)

    @staticmethod
    def _validate_legacy_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, 1):
            raise DatabaseSchemaError(
                f"state database schema version {version} is not a legacy/v1 source"
            )
        StateStore._validate_declared_schema(
            connection, V1_SCHEMA_COLUMN_SPECS, "legacy/v1 state database"
        )
        StateStore._validate_schema_values(
            connection, V1_SCHEMA_COLUMN_SPECS, "legacy/v1 state database"
        )
        StateStore._validate_config_hashes(connection)

    @staticmethod
    def _validate_v2_schema(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != 2:
            raise DatabaseSchemaError(
                f"state database schema version {version} is not the v2 migration source"
            )
        StateStore._validate_declared_schema(
            connection, V2_SCHEMA_COLUMN_SPECS, "state database v2"
        )
        StateStore._validate_schema_values(
            connection, V2_SCHEMA_COLUMN_SPECS, "state database v2"
        )
        StateStore._validate_config_hashes(connection)
        StateStore._validate_compact_boundary_references(connection)

    @staticmethod
    def _validate_declared_schema(
        connection: sqlite3.Connection,
        specifications: dict[str, dict[str, _ColumnSpec]],
        label: str,
    ) -> None:
        failures: list[str] = []
        for table, columns in specifications.items():
            table_info = {
                row[1]: row for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if not table_info:
                failures.append(f"table {table}")
                continue
            for column, specification in columns.items():
                info = table_info.get(column)
                if info is None:
                    failures.append(f"{table}.{column}")
                    continue
                declared_type = str(info[2]).strip().upper()
                if declared_type != specification.sqlite_type:
                    failures.append(
                        f"{table}.{column} type {declared_type or '<empty>'} "
                        f"is not {specification.sqlite_type}"
                    )
                if specification.not_null and info[3] != 1:
                    failures.append(f"{table}.{column} must be NOT NULL")
                if specification.primary_key and info[5] != 1:
                    failures.append(f"{table}.{column} must be a PRIMARY KEY")
        if failures:
            raise DatabaseSchemaError(
                f"{label} schema is incomplete or invalid: " + ", ".join(failures)
            )

    @staticmethod
    def _validate_schema_values(
        connection: sqlite3.Connection,
        specifications: dict[str, dict[str, _ColumnSpec]],
        label: str,
    ) -> None:
        for table, columns in specifications.items():
            column_names = ", ".join(columns)
            for row in connection.execute(f"SELECT {column_names} FROM {table}"):
                for (column, specification), value in zip(columns.items(), row):
                    if value is None:
                        if specification.not_null or specification.primary_key:
                            raise DatabaseSchemaError(
                                f"{label} {table}.{column} must not contain NULL"
                            )
                        continue
                    valid_type = (
                        specification.sqlite_type == "INTEGER"
                        and isinstance(value, int)
                        and not isinstance(value, bool)
                    ) or (
                        specification.sqlite_type == "TEXT" and isinstance(value, str)
                    ) or (
                        specification.sqlite_type == "BLOB"
                        and isinstance(value, (bytes, bytearray, memoryview))
                    )
                    if not valid_type:
                        raise DatabaseSchemaError(
                            f"{label} {table}.{column} values must be "
                            f"{specification.sqlite_type}"
                        )
                    if specification.identifier:
                        try:
                            _identifier(value, f"{table}.{column}")
                        except (TypeError, ValueError) as exc:
                            raise DatabaseSchemaError(
                                f"{label} {table}.{column} is not a valid identifier"
                            ) from exc
                    if specification.timestamp and value < 0:
                        raise DatabaseSchemaError(
                            f"{label} {table}.{column} must be non-negative"
                        )

    @staticmethod
    def _validate_config_hashes(connection: sqlite3.Connection) -> None:
        for (value,) in connection.execute(
            "SELECT config_sha256 FROM config_receipts"
        ):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
                raise DatabaseSchemaError(
                    "state database config_sha256 values must be exactly 64 hexadecimal characters"
                )

    @staticmethod
    def _validate_compact_boundary_references(connection: sqlite3.Connection) -> None:
        for task_id, response_id, boundary_created_at in connection.execute(
            "SELECT codex_task_id, boundary_response_id, boundary_created_at "
            "FROM compact_boundaries"
        ):
            response = connection.execute(
                "SELECT codex_task_id, created_at FROM response_links "
                "WHERE local_response_id = ?",
                (response_id,),
            ).fetchone()
            if response is None:
                raise DatabaseSchemaError(
                    "state database compact boundary response link is missing"
                )
            response_task_id, response_created_at = response
            if response_task_id != task_id:
                raise DatabaseSchemaError(
                    "state database compact boundary response link task does not match "
                    "the boundary task"
                )
            if response_created_at != boundary_created_at:
                raise DatabaseSchemaError(
                    "state database compact boundary created_at does not match "
                    "the response link"
                )

    @staticmethod
    def _validate_event_state(connection: sqlite3.Connection) -> None:
        sequences: list[int] = []
        for table in ("response_links", "context_fragments"):
            for (value,) in connection.execute(f"SELECT event_sequence FROM {table}"):
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise DatabaseSchemaError(
                        f"state database event sequence in {table} must be a positive integer"
                    )
                sequences.append(value)
        if len(sequences) != len(set(sequences)):
            raise DatabaseSchemaError("state database event sequence values must be unique")

        StateStore._validate_compact_boundary_references(connection)
        for task_id, response_id, boundary_sequence in connection.execute(
            "SELECT codex_task_id, boundary_response_id, boundary_event_sequence "
            "FROM compact_boundaries"
        ):
            if (
                not isinstance(boundary_sequence, int)
                or isinstance(boundary_sequence, bool)
                or boundary_sequence <= 0
            ):
                raise DatabaseSchemaError(
                    "state database boundary event sequence must be a positive integer"
                )
            response_sequence = connection.execute(
                "SELECT event_sequence FROM response_links "
                "WHERE local_response_id = ? AND codex_task_id = ?",
                (response_id, task_id),
            ).fetchone()
            if response_sequence is None or response_sequence[0] != boundary_sequence:
                raise DatabaseSchemaError(
                    "state database compact boundary event sequence does not match "
                    "its response link"
                )

        counter_rows = connection.execute(
            "SELECT value FROM event_counters WHERE counter_name = 'state'"
        ).fetchall()
        if len(counter_rows) != 1:
            raise DatabaseSchemaError("state database event counter state is missing or duplicated")
        counter = counter_rows[0][0]
        if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
            raise DatabaseSchemaError(
                "state database event counter state must be a non-negative integer"
            )
        maximum = max(sequences, default=0)
        if counter != maximum:
            raise DatabaseSchemaError(
                "state database event counter must equal the highest event sequence"
            )

    @staticmethod
    def _migrate_zero_to_one(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS route_selections (
                selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                codex_task_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                selected_at INTEGER NOT NULL
            )
            """,
            """

            CREATE TABLE IF NOT EXISTS response_links (
                local_response_id TEXT PRIMARY KEY,
                upstream_response_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """,
            """

            CREATE TABLE IF NOT EXISTS context_fragments (
                fragment_id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                created_at INTEGER NOT NULL
            )
            """,
            """

            CREATE TABLE IF NOT EXISTS config_receipts (
                receipt_id TEXT PRIMARY KEY,
                config_sha256 TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """,
            """

            CREATE TABLE IF NOT EXISTS cancel_handles (
                handle_id TEXT PRIMARY KEY,
                codex_task_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS route_selections_task_turn "
            "ON route_selections (codex_task_id, turn_id, selected_at DESC)"
        )

    @staticmethod
    def _migrate_one_to_two(connection: sqlite3.Connection) -> None:
        StateStore._migrate_zero_to_one(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS response_links (
                local_response_id TEXT PRIMARY KEY,
                upstream_response_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                codex_task_id TEXT,
                expires_at INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS context_fragments (
                fragment_id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cancel_handles (
                handle_id TEXT PRIMARY KEY,
                codex_task_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER
            )
            """
        )
        StateStore._add_column_if_missing(connection, "response_links", "codex_task_id", "TEXT")
        StateStore._add_column_if_missing(connection, "response_links", "expires_at", "INTEGER")
        StateStore._add_column_if_missing(connection, "context_fragments", "expires_at", "INTEGER")
        StateStore._add_column_if_missing(connection, "cancel_handles", "expires_at", "INTEGER")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS compact_boundaries (
                codex_task_id TEXT PRIMARY KEY,
                boundary_response_id TEXT NOT NULL,
                boundary_created_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS response_links_task_order "
            "ON response_links (codex_task_id, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS context_fragments_scope_order "
            "ON context_fragments (scope_id, created_at)"
        )

    @staticmethod
    def _migrate_two_to_three(connection: sqlite3.Connection) -> None:
        StateStore._migrate_zero_to_one(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS event_counters (
                counter_name TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO event_counters (counter_name, value) VALUES ('state', 0)"
        )
        StateStore._add_column_if_missing(
            connection, "response_links", "event_sequence", "INTEGER NOT NULL DEFAULT 0"
        )
        StateStore._add_column_if_missing(
            connection,
            "context_fragments",
            "event_sequence",
            "INTEGER NOT NULL DEFAULT 0",
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS compact_boundaries (
                codex_task_id TEXT PRIMARY KEY,
                boundary_response_id TEXT NOT NULL,
                boundary_created_at INTEGER NOT NULL
            )
            """
        )
        StateStore._add_column_if_missing(
            connection,
            "compact_boundaries",
            "boundary_event_sequence",
            "INTEGER NOT NULL DEFAULT 0",
        )
        legacy_events = connection.execute(
            """
            SELECT local_response_id AS legacy_id, created_at,
                   0 AS record_type, 'response' AS record_kind
            FROM response_links
            UNION ALL
            SELECT fragment_id AS legacy_id, created_at,
                   1 AS record_type, 'fragment' AS record_kind
            FROM context_fragments
            ORDER BY created_at, record_type, legacy_id
            """
        ).fetchall()
        for event_sequence, (legacy_id, _created_at, _record_type, record_kind) in enumerate(
            legacy_events, start=1
        ):
            table = "response_links" if record_kind == "response" else "context_fragments"
            identifier_column = (
                "local_response_id" if record_kind == "response" else "fragment_id"
            )
            connection.execute(
                f"UPDATE {table} SET event_sequence = ? WHERE {identifier_column} = ?",
                (event_sequence, legacy_id),
            )
        connection.execute(
            """
            UPDATE compact_boundaries
            SET boundary_event_sequence = (
                SELECT event_sequence FROM response_links
                WHERE local_response_id = compact_boundaries.boundary_response_id
            )
            WHERE boundary_event_sequence IS NULL OR boundary_event_sequence = 0
            """
        )
        connection.execute(
            "UPDATE event_counters SET value = ? WHERE counter_name = 'state'",
            (len(legacy_events),),
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS response_links_event_order "
            "ON response_links (codex_task_id, event_sequence)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS context_fragments_event_order "
            "ON context_fragments (scope_id, event_sequence)"
        )

    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._require_connection().execute("PRAGMA user_version").fetchone()[0])

    @property
    def journal_mode(self) -> str:
        with self._lock:
            return str(
                self._require_connection().execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StateError("state store is closed")
        return self._connection

    def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        connection = self._require_connection()
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = operation(connection)
                connection.execute("COMMIT")
                return result
            except BaseException:
                with suppress(sqlite3.DatabaseError):
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _next_event_sequence(connection: sqlite3.Connection) -> int:
        updated = connection.execute(
            "UPDATE event_counters SET value = value + 1 WHERE counter_name = 'state'"
        ).rowcount
        if updated != 1:
            raise StateError("state event counter is missing")
        return int(
            connection.execute(
                "SELECT value FROM event_counters WHERE counter_name = 'state'"
            ).fetchone()[0]
        )

    @staticmethod
    def _sync_event_counter(connection: sqlite3.Connection) -> None:
        maximum = connection.execute(
            """
            SELECT MAX(event_sequence) FROM (
                SELECT event_sequence FROM response_links
                UNION ALL
                SELECT event_sequence FROM context_fragments
            )
            """
        ).fetchone()[0]
        updated = connection.execute(
            "UPDATE event_counters SET value = ? WHERE counter_name = 'state'",
            (0 if maximum is None else maximum,),
        ).rowcount
        if updated != 1:
            raise StateError("state event counter is missing")

    def link_response(
        self,
        local_response_id: str,
        upstream_response_id: str,
        *,
        route_id: str,
        codex_task_id: str | None = None,
        expires_at: int | None = None,
        created_at: int | None = None,
    ) -> ResponseLink:
        local_response_id = _identifier(local_response_id, "local_response_id")
        upstream_response_id = _identifier(upstream_response_id, "upstream_response_id")
        route_id = _identifier(route_id, "route_id")
        if codex_task_id is not None:
            codex_task_id = _identifier(codex_task_id, "codex_task_id")
        expires_at = _optional_expiry(expires_at)
        created_at = _timestamp() if created_at is None else created_at
        created_at = _timestamp_value(created_at, "created_at")

        def insert(connection: sqlite3.Connection) -> ResponseLink:
            boundary_reference = connection.execute(
                "SELECT 1 FROM compact_boundaries "
                "WHERE boundary_response_id = ? LIMIT 1",
                (local_response_id,),
            ).fetchone()
            if boundary_reference is not None:
                raise StateError(
                    "cannot update a response link referenced by a compact boundary"
                )
            event_sequence = self._next_event_sequence(connection)
            connection.execute(
                """
                INSERT INTO response_links (
                    local_response_id, upstream_response_id, route_id,
                    created_at, codex_task_id, expires_at, event_sequence
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_response_id) DO UPDATE SET
                    upstream_response_id = excluded.upstream_response_id,
                    route_id = excluded.route_id,
                    created_at = excluded.created_at,
                    codex_task_id = excluded.codex_task_id,
                    expires_at = excluded.expires_at,
                    event_sequence = excluded.event_sequence
                """,
                (
                    local_response_id,
                    upstream_response_id,
                    route_id,
                    created_at,
                    codex_task_id,
                    expires_at,
                    event_sequence,
                ),
            )
            return ResponseLink(
                local_response_id,
                upstream_response_id,
                route_id,
                codex_task_id,
                created_at,
                expires_at,
                event_sequence,
            )

        return self._write(insert)

    def get_response_link(self, local_response_id: str) -> ResponseLink | None:
        local_response_id = _identifier(local_response_id, "local_response_id")
        with self._lock:
            row = self._require_connection().execute(
                """
                SELECT local_response_id, upstream_response_id, route_id,
                       codex_task_id, created_at, expires_at, event_sequence
                FROM response_links WHERE local_response_id = ?
                """,
                (local_response_id,),
            ).fetchone()
        if row is None:
            return None
        return ResponseLink(*row)

    def save_route_selection(
        self,
        codex_task_id: str,
        turn_id: str,
        route_id: str,
        *,
        selected_at: int | None = None,
    ) -> RouteSelection:
        codex_task_id = _identifier(codex_task_id, "codex_task_id")
        turn_id = _identifier(turn_id, "turn_id")
        route_id = _identifier(route_id, "route_id")
        selected_at = _timestamp() if selected_at is None else selected_at
        selected_at = _timestamp_value(selected_at, "selected_at")
        selection = RouteSelection(codex_task_id, turn_id, route_id, selected_at)

        def insert(connection: sqlite3.Connection) -> RouteSelection:
            connection.execute(
                """
                INSERT INTO route_selections (codex_task_id, turn_id, route_id, selected_at)
                VALUES (?, ?, ?, ?)
                """,
                (codex_task_id, turn_id, route_id, selected_at),
            )
            return selection

        return self._write(insert)

    def get_route_selection(self, codex_task_id: str, turn_id: str) -> RouteSelection | None:
        codex_task_id = _identifier(codex_task_id, "codex_task_id")
        turn_id = _identifier(turn_id, "turn_id")
        with self._lock:
            row = self._require_connection().execute(
                """
                SELECT codex_task_id, turn_id, route_id, selected_at
                FROM route_selections
                WHERE codex_task_id = ? AND turn_id = ?
                ORDER BY selected_at DESC, selection_id DESC LIMIT 1
                """,
                (codex_task_id, turn_id),
            ).fetchone()
        return None if row is None else RouteSelection(*row)

    def save_chat_fragment(
        self,
        scope_id: str,
        fragment_id: str,
        text: str,
        *,
        expires_at: int | None = None,
        created_at: int | None = None,
    ) -> None:
        scope_id = _identifier(scope_id, "scope_id")
        fragment_id = _identifier(fragment_id, "fragment_id")
        if not isinstance(text, str):
            raise TypeError("chat fragments must be text")
        if len(text) > MAX_CONTEXT_FRAGMENT_CHARS:
            raise ValueError("chat fragments exceed the maximum supported size")
        expires_at = _optional_expiry(expires_at)
        if created_at is not None:
            created_at = _timestamp_value(created_at, "created_at")
        ciphertext = self._cipher.encrypt_text(text)

        def insert(connection: sqlite3.Connection) -> None:
            event_sequence = self._next_event_sequence(connection)
            connection.execute(
                """
                INSERT INTO context_fragments (
                    fragment_id, scope_id, ciphertext, created_at, expires_at, event_sequence
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fragment_id) DO UPDATE SET
                    scope_id = excluded.scope_id,
                    ciphertext = excluded.ciphertext,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    event_sequence = excluded.event_sequence
                """,
                (
                    fragment_id,
                    scope_id,
                    sqlite3.Binary(ciphertext),
                    _timestamp() if created_at is None else created_at,
                    expires_at,
                    event_sequence,
                ),
            )

        self._write(insert)

    def get_chat_fragment(self, fragment_id: str) -> str | None:
        fragment_id = _identifier(fragment_id, "fragment_id")
        with self._lock:
            row = self._require_connection().execute(
                "SELECT ciphertext FROM context_fragments WHERE fragment_id = ?",
                (fragment_id,),
            ).fetchone()
        return None if row is None else self._cipher.decrypt_text(bytes(row[0]))

    def save_config_receipt(
        self, receipt_id: str, config_sha256: str, *, created_at: int | None = None
    ) -> ConfigReceipt:
        receipt_id = _identifier(receipt_id, "receipt_id")
        if (
            not isinstance(config_sha256, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", config_sha256) is None
        ):
            raise ValueError("config_sha256 must be exactly 64 hexadecimal characters")
        created_at = _timestamp() if created_at is None else created_at
        created_at = _timestamp_value(created_at, "created_at")
        receipt = ConfigReceipt(receipt_id, config_sha256, created_at)

        def insert(connection: sqlite3.Connection) -> ConfigReceipt:
            connection.execute(
                """
                INSERT INTO config_receipts (receipt_id, config_sha256, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(receipt_id) DO UPDATE SET
                    config_sha256 = excluded.config_sha256,
                    created_at = excluded.created_at
                """,
                (receipt_id, config_sha256, created_at),
            )
            return receipt

        return self._write(insert)

    def get_config_receipt(self, receipt_id: str) -> ConfigReceipt | None:
        receipt_id = _identifier(receipt_id, "receipt_id")
        with self._lock:
            row = self._require_connection().execute(
                "SELECT receipt_id, config_sha256, created_at "
                "FROM config_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        return None if row is None else ConfigReceipt(*row)

    def save_cancel_handle(
        self,
        handle_id: str,
        *,
        codex_task_id: str,
        route_id: str,
        expires_at: int | None = None,
        created_at: int | None = None,
    ) -> CancelHandleMetadata:
        handle_id = _identifier(handle_id, "handle_id")
        codex_task_id = _identifier(codex_task_id, "codex_task_id")
        route_id = _identifier(route_id, "route_id")
        expires_at = _optional_expiry(expires_at)
        created_at = _timestamp() if created_at is None else created_at
        created_at = _timestamp_value(created_at, "created_at")
        metadata = CancelHandleMetadata(handle_id, codex_task_id, route_id, created_at, expires_at)

        def insert(connection: sqlite3.Connection) -> CancelHandleMetadata:
            connection.execute(
                """
                INSERT INTO cancel_handles (
                    handle_id, codex_task_id, route_id, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(handle_id) DO UPDATE SET
                    codex_task_id = excluded.codex_task_id,
                    route_id = excluded.route_id,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (handle_id, codex_task_id, route_id, created_at, expires_at),
            )
            return metadata

        return self._write(insert)

    def get_cancel_handle(self, handle_id: str) -> CancelHandleMetadata | None:
        handle_id = _identifier(handle_id, "handle_id")
        with self._lock:
            row = self._require_connection().execute(
                """
                SELECT handle_id, codex_task_id, route_id, created_at, expires_at
                FROM cancel_handles WHERE handle_id = ?
                """,
                (handle_id,),
            ).fetchone()
        return None if row is None else CancelHandleMetadata(*row)

    def purge_expired(self, *, now: int | None = None) -> int:
        now = _timestamp() if now is None else now
        now = _timestamp_value(now, "now")

        def delete(connection: sqlite3.Connection) -> int:
            boundary_reference = connection.execute(
                """
                SELECT 1
                FROM compact_boundaries AS boundaries
                JOIN response_links AS links
                  ON links.local_response_id = boundaries.boundary_response_id
                WHERE links.expires_at IS NOT NULL AND links.expires_at <= ?
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if boundary_reference is not None:
                raise StateError(
                    "cannot purge a response link referenced by a compact boundary"
                )
            removed = 0
            for table in ("response_links", "context_fragments", "cancel_handles"):
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
                )
                removed += cursor.rowcount
            self._sync_event_counter(connection)
            return removed

        return self._write(delete)

    def prune_after_compact(self, codex_task_id: str, boundary_response_id: str) -> int:
        codex_task_id = _identifier(codex_task_id, "codex_task_id")
        boundary_response_id = _identifier(boundary_response_id, "boundary_response_id")

        def prune(connection: sqlite3.Connection) -> int:
            boundary = connection.execute(
                """
                SELECT created_at, event_sequence FROM response_links
                WHERE local_response_id = ? AND codex_task_id = ?
                """,
                (boundary_response_id, codex_task_id),
            ).fetchone()
            if boundary is None:
                raise StateNotFoundError("compact boundary response link was not found")

            boundary_created_at, boundary_event_sequence = boundary
            connection.execute(
                """
                INSERT INTO compact_boundaries (
                    codex_task_id, boundary_response_id, boundary_created_at,
                    boundary_event_sequence
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(codex_task_id) DO UPDATE SET
                    boundary_response_id = excluded.boundary_response_id,
                    boundary_created_at = excluded.boundary_created_at,
                    boundary_event_sequence = excluded.boundary_event_sequence
                """,
                (
                    codex_task_id,
                    boundary_response_id,
                    boundary_created_at,
                    boundary_event_sequence,
                ),
            )
            connection.execute(
                """
                UPDATE response_links
                SET expires_at = ?
                WHERE codex_task_id = ? AND event_sequence < ?
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (
                    boundary_created_at,
                    codex_task_id,
                    boundary_event_sequence,
                    boundary_created_at,
                ),
            )
            removed_links = connection.execute(
                """
                DELETE FROM response_links
                WHERE codex_task_id = ? AND event_sequence < ? AND expires_at <= ?
                """,
                (codex_task_id, boundary_event_sequence, boundary_created_at),
            ).rowcount
            connection.execute(
                """
                UPDATE context_fragments
                SET expires_at = ?
                WHERE scope_id = ? AND event_sequence < ?
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (
                    boundary_created_at,
                    codex_task_id,
                    boundary_event_sequence,
                    boundary_created_at,
                ),
            )
            removed_fragments = connection.execute(
                """
                DELETE FROM context_fragments
                WHERE scope_id = ? AND event_sequence < ? AND expires_at <= ?
                """,
                (codex_task_id, boundary_event_sequence, boundary_created_at),
            ).rowcount
            self._sync_event_counter(connection)
            return removed_links + removed_fragments

        return self._write(prune)

    compact_prune = prune_after_compact

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _quarantine_existing(self, source_directory: Path | None = None) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine = self.path.with_name(f"{self.path.name}.quarantine-{timestamp}")
        suffix = 1
        while quarantine.exists():
            quarantine = self.path.with_name(f"{self.path.name}.quarantine-{timestamp}-{suffix}")
            suffix += 1
        source_base = self.path if source_directory is None else source_directory / self.path.name
        copied_suffixes: set[str] = set()
        failures: dict[str, DatabaseSnapshotError] = {}
        for _pass in range(2):
            for sidecar_suffix in ("", "-wal", "-shm"):
                label = sidecar_suffix or "main"
                if label in copied_suffixes:
                    continue
                source = source_base if not sidecar_suffix else source_base.with_name(
                    f"{source_base.name}{sidecar_suffix}"
                )
                fallback = self.path if not sidecar_suffix else self.path.with_name(
                    f"{self.path.name}{sidecar_suffix}"
                )
                if not source.exists() and fallback.exists():
                    source = fallback
                if source.exists():
                    target = quarantine.with_name(f"{quarantine.name}{sidecar_suffix}")
                    try:
                        self._copy_file_with_retry(source, target)
                    except DatabaseSnapshotError as exc:
                        failures[label] = exc
                        continue
                    if not target.exists():
                        failures[label] = DatabaseSnapshotError(
                            f"quarantine copy produced no evidence for {label}"
                        )
                        continue
                    copied_suffixes.add(label)
                    failures.pop(label, None)
        if failures:
            raise DatabaseQuarantineError(
                quarantine,
                tuple(sorted(copied_suffixes)),
                tuple(sorted(failures)),
            ) from next(iter(failures.values()))
        return quarantine

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()
