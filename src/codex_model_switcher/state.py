"""Durable, minimal state owned by the local protocol router.

Codex remains authoritative for tasks, transcripts, and compaction.  The
database below stores only routing links, encrypted continuation fragments,
configuration receipts, and cancellation metadata needed by an adapter.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable, TypeVar

from .crypto import FernetCipher, SecretKeyProvider

SCHEMA_VERSION = 2
MAX_IDENTIFIER_LENGTH = 512
MAX_CONTEXT_FRAGMENT_CHARS = 64 * 1024
T = TypeVar("T")
_DATABASE_INIT_LOCK = RLock()


class StateError(Exception):
    """Base error for durable router state."""


class DatabaseCorruptionError(StateError):
    """Raised when an existing database cannot be safely opened."""


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
            if existed and (not self.path.is_file() or self.path.stat().st_size == 0):
                quarantine = self._quarantine_existing()
                raise DatabaseCorruptionError(
                    "existing state database is empty or not a file; "
                    f"quarantined as {quarantine.name}"
                )

            self.path.parent.mkdir(parents=True, exist_ok=True)
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
            connection.execute(
                """
                INSERT INTO response_links (
                    local_response_id, upstream_response_id, route_id,
                    created_at, codex_task_id, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_response_id) DO UPDATE SET
                    upstream_response_id = excluded.upstream_response_id,
                    route_id = excluded.route_id,
                    created_at = excluded.created_at,
                    codex_task_id = excluded.codex_task_id,
                    expires_at = excluded.expires_at
                """,
                (
                    local_response_id,
                    upstream_response_id,
                    route_id,
                    created_at,
                    codex_task_id,
                    expires_at,
                ),
            )
            return ResponseLink(
                local_response_id,
                upstream_response_id,
                route_id,
                codex_task_id,
                created_at,
                expires_at,
            )

        return self._write(insert)

    def get_response_link(self, local_response_id: str) -> ResponseLink | None:
        local_response_id = _identifier(local_response_id, "local_response_id")
        with self._lock:
            row = self._require_connection().execute(
                """
                SELECT local_response_id, upstream_response_id, route_id,
                       codex_task_id, created_at, expires_at
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
    ) -> None:
        scope_id = _identifier(scope_id, "scope_id")
        fragment_id = _identifier(fragment_id, "fragment_id")
        if not isinstance(text, str):
            raise TypeError("chat fragments must be text")
        if len(text) > MAX_CONTEXT_FRAGMENT_CHARS:
            raise ValueError("chat fragments exceed the maximum supported size")
        expires_at = _optional_expiry(expires_at)
        ciphertext = self._cipher.encrypt_text(text)

        def insert(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO context_fragments (
                    fragment_id, scope_id, ciphertext, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(fragment_id) DO UPDATE SET
                    scope_id = excluded.scope_id,
                    ciphertext = excluded.ciphertext,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (fragment_id, scope_id, sqlite3.Binary(ciphertext), _timestamp(), expires_at),
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
            or not config_sha256
            or len(config_sha256) > 128
            or any(character.isspace() for character in config_sha256)
        ):
            raise ValueError("config_sha256 must be a short digest receipt")
        created_at = _timestamp() if created_at is None else created_at
        if not isinstance(created_at, int):
            raise TypeError("created_at must be an integer timestamp")
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
        if not isinstance(created_at, int):
            raise TypeError("created_at must be an integer timestamp")
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
                SELECT rowid, created_at FROM response_links
                WHERE local_response_id = ? AND codex_task_id = ?
                """,
                (boundary_response_id, codex_task_id),
            ).fetchone()
            if boundary is None:
                raise StateNotFoundError("compact boundary response link was not found")

            boundary_rowid, boundary_created_at = boundary
            connection.execute(
                """
                INSERT INTO compact_boundaries (
                    codex_task_id, boundary_response_id, boundary_created_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(codex_task_id) DO UPDATE SET
                    boundary_response_id = excluded.boundary_response_id,
                    boundary_created_at = excluded.boundary_created_at
                """,
                (codex_task_id, boundary_response_id, boundary_created_at),
            )
            connection.execute(
                """
                UPDATE response_links
                SET expires_at = ?
                WHERE codex_task_id = ? AND rowid < ?
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (boundary_created_at, codex_task_id, boundary_rowid, boundary_created_at),
            )
            removed_links = connection.execute(
                """
                DELETE FROM response_links
                WHERE codex_task_id = ? AND rowid < ? AND expires_at <= ?
                """,
                (codex_task_id, boundary_rowid, boundary_created_at),
            ).rowcount
            connection.execute(
                """
                UPDATE context_fragments
                SET expires_at = ?
                WHERE scope_id = ? AND created_at < ?
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (boundary_created_at, codex_task_id, boundary_created_at, boundary_created_at),
            )
            removed_fragments = connection.execute(
                """
                DELETE FROM context_fragments
                WHERE scope_id = ? AND created_at < ? AND expires_at <= ?
                """,
                (codex_task_id, boundary_created_at, boundary_created_at),
            ).rowcount
            return removed_links + removed_fragments

        return self._write(prune)

    compact_prune = prune_after_compact

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _quarantine_existing(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine = self.path.with_name(f"{self.path.name}.quarantine-{timestamp}")
        suffix = 1
        while quarantine.exists():
            quarantine = self.path.with_name(f"{self.path.name}.quarantine-{timestamp}-{suffix}")
            suffix += 1
        shutil.copy2(self.path, quarantine)
        return quarantine

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()
