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
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Callable, TypeVar

from .crypto import FernetCipher, SecretKeyProvider

SCHEMA_VERSION = 3
MAX_IDENTIFIER_LENGTH = 512
MAX_CONTEXT_FRAGMENT_CHARS = 64 * 1024
SNAPSHOT_COPY_ATTEMPTS = 4
SNAPSHOT_COPY_DELAY_SECONDS = 0.01
SQLITE_HEADER = b"SQLite format 3\x00"
T = TypeVar("T")
_DATABASE_INIT_LOCK = RLock()


class StateError(Exception):
    """Base error for durable router state."""


class DatabaseCorruptionError(StateError):
    """Raised when an existing database cannot be safely opened."""


class DatabaseSnapshotError(DatabaseCorruptionError):
    """Raised when a consistent database snapshot cannot be captured."""


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


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{field} must be a non-empty short identifier")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL")
    return value


def _optional_expiry(value: int | None) -> int | None:
    if value is not None and not isinstance(value, int):
        raise TypeError("expires_at must be an integer timestamp or None")
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
            except DatabaseCorruptionError:
                self.close()
                raise
            except UnsupportedSchemaError:
                self.close()
                raise
            except (OSError, sqlite3.DatabaseError) as exc:
                if existed:
                    quarantine = self._quarantine_existing()
                    self.close()
                    raise DatabaseCorruptionError(
                        "existing state database failed integrity checks; "
                        f"quarantined as {quarantine.name}"
                    ) from exc
                self.close()
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

    def _verify_existing_before_write(self) -> None:
        source: sqlite3.Connection | None = None
        snapshot: sqlite3.Connection | None = None
        with TemporaryDirectory(prefix=".state-preflight-", dir=self.path.parent) as directory:
            snapshot_dir = Path(directory) / "snapshot"
            evidence_dir = Path(directory) / "evidence"
            snapshot_dir.mkdir()
            evidence_dir.mkdir()
            evidence_captured = False
            try:
                self._copy_database_set(evidence_dir)
                evidence_captured = True
                with self.path.open("rb") as database_file:
                    if database_file.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
                        raise DatabaseSnapshotError("state database header is invalid")
                source = sqlite3.connect(
                    self.path,
                    timeout=30.0,
                    isolation_level=None,
                    check_same_thread=False,
                )
                source.execute("BEGIN")
                snapshot_path = snapshot_dir / self.path.name
                snapshot = sqlite3.connect(snapshot_path)
                source.backup(snapshot, pages=0, sleep=SNAPSHOT_COPY_DELAY_SECONDS)
                result = snapshot.execute("PRAGMA integrity_check").fetchone()
                if not result or str(result[0]).lower() != "ok":
                    raise sqlite3.DatabaseError("state database integrity check failed")
                snapshot.close()
                snapshot = None
                source.close()
                source = None
            except (OSError, sqlite3.Error, DatabaseSnapshotError) as original_error:
                failure = original_error
                if snapshot is not None:
                    snapshot.close()
                    snapshot = None
                if source is not None:
                    with suppress(sqlite3.Error):
                        source.rollback()
                    source.close()
                    source = None
                if not evidence_captured:
                    try:
                        self._copy_database_set(evidence_dir)
                    except DatabaseSnapshotError as evidence_error:
                        failure = evidence_error
                quarantine = self._quarantine_existing(evidence_dir)
                raise DatabaseCorruptionError(
                    "existing state database snapshot failed; "
                    f"quarantined as {quarantine.name}"
                ) from failure
            finally:
                if snapshot is not None:
                    snapshot.close()
                if source is not None:
                    source.close()

    def _copy_database_set(self, destination: Path) -> None:
        for sidecar_suffix in ("", "-wal", "-shm"):
            source = self.path if not sidecar_suffix else self.path.with_name(
                f"{self.path.name}{sidecar_suffix}"
            )
            if source.exists():
                self._copy_file_with_retry(source, destination / source.name)

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
        StateStore._add_column_if_missing(connection, "response_links", "event_sequence", "INTEGER")
        StateStore._add_column_if_missing(
            connection, "context_fragments", "event_sequence", "INTEGER"
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
            connection, "compact_boundaries", "boundary_event_sequence", "INTEGER"
        )
        connection.execute(
            "UPDATE response_links SET event_sequence = rowid WHERE event_sequence IS NULL"
        )
        response_max = int(
            connection.execute(
                "SELECT COALESCE(MAX(event_sequence), 0) FROM response_links"
            ).fetchone()[0]
        )
        connection.execute(
            """
            UPDATE context_fragments
            SET event_sequence = ? + rowid
            WHERE event_sequence IS NULL
            """,
            (response_max,),
        )
        context_max = int(
            connection.execute(
                "SELECT COALESCE(MAX(event_sequence), 0) FROM context_fragments"
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE event_counters SET value = ? WHERE counter_name = 'state'",
            (max(response_max, context_max),),
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
        if not isinstance(created_at, int):
            raise TypeError("created_at must be an integer timestamp")

        def insert(connection: sqlite3.Connection) -> ResponseLink:
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
        if not isinstance(selected_at, int):
            raise TypeError("selected_at must be an integer timestamp")
        selection = RouteSelection(codex_task_id, turn_id, route_id, selected_at)

        def insert(connection: sqlite3.Connection) -> RouteSelection:
            self._next_event_sequence(connection)
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
        if created_at is not None and not isinstance(created_at, int):
            raise TypeError("created_at must be an integer timestamp")
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
        if not isinstance(created_at, int):
            raise TypeError("created_at must be an integer timestamp")
        receipt = ConfigReceipt(receipt_id, config_sha256, created_at)

        def insert(connection: sqlite3.Connection) -> ConfigReceipt:
            self._next_event_sequence(connection)
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
        if not isinstance(created_at, int):
            raise TypeError("created_at must be an integer timestamp")
        metadata = CancelHandleMetadata(handle_id, codex_task_id, route_id, created_at, expires_at)

        def insert(connection: sqlite3.Connection) -> CancelHandleMetadata:
            self._next_event_sequence(connection)
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
        if not isinstance(now, int):
            raise TypeError("now must be an integer timestamp")

        def delete(connection: sqlite3.Connection) -> int:
            removed = 0
            for table in ("response_links", "context_fragments", "cancel_handles"):
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
                )
                removed += cursor.rowcount
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
        for _pass in range(2):
            for sidecar_suffix in ("", "-wal", "-shm"):
                if sidecar_suffix in copied_suffixes:
                    continue
                source = source_base if not sidecar_suffix else source_base.with_name(
                    f"{source_base.name}{sidecar_suffix}"
                )
                if source.exists():
                    target = quarantine.with_name(f"{quarantine.name}{sidecar_suffix}")
                    self._copy_file_with_retry(source, target)
                    copied_suffixes.add(sidecar_suffix)
        return quarantine

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()
