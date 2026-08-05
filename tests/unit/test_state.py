import sqlite3
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread

import pytest
from cryptography.fernet import Fernet

from codex_model_switcher import state as state_module
from codex_model_switcher.state import (
    DatabaseCorruptionError,
    DatabaseQuarantineError,
    StateError,
    StateStore,
)


class FakeSecretKeyProvider:
    def __init__(self) -> None:
        self.key = Fernet.generate_key()

    def get_key(self) -> bytes:
        return self.key


@pytest.fixture
def secret_key_provider() -> FakeSecretKeyProvider:
    return FakeSecretKeyProvider()


def _seed_semantically_invalid_source(
    path: Path, version: int, response_values: tuple[object, ...]
) -> sqlite3.Connection:
    seed = sqlite3.connect(path)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("PRAGMA wal_autocheckpoint=0")
    if version == 1:
        seed.executescript(
            """
            CREATE TABLE route_selections (
                selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                codex_task_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                selected_at INTEGER NOT NULL
            );
            CREATE TABLE response_links (
                local_response_id TEXT PRIMARY KEY,
                upstream_response_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE context_fragments (
                fragment_id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE config_receipts (
                receipt_id TEXT PRIMARY KEY,
                config_sha256 TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE cancel_handles (
                handle_id TEXT PRIMARY KEY,
                codex_task_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        response_sql = "INSERT INTO response_links VALUES (?, ?, ?, ?)"
    elif version == 2:
        seed.executescript(
            """
            CREATE TABLE route_selections (
                selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                codex_task_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                selected_at INTEGER NOT NULL
            );
            CREATE TABLE response_links (
                local_response_id TEXT PRIMARY KEY,
                upstream_response_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                codex_task_id TEXT,
                expires_at INTEGER
            );
            CREATE TABLE context_fragments (
                fragment_id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER
            );
            CREATE TABLE config_receipts (
                receipt_id TEXT PRIMARY KEY,
                config_sha256 TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE cancel_handles (
                handle_id TEXT PRIMARY KEY,
                codex_task_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER
            );
            CREATE TABLE compact_boundaries (
                codex_task_id TEXT PRIMARY KEY,
                boundary_response_id TEXT NOT NULL,
                boundary_created_at INTEGER NOT NULL
            );
            """
        )
        response_sql = "INSERT INTO response_links VALUES (?, ?, ?, ?, ?, ?)"
    else:
        raise AssertionError(f"unsupported test fixture version: {version}")
    seed.execute(f"PRAGMA user_version = {version}")
    seed.execute(response_sql, response_values)
    seed.execute(
        "INSERT INTO config_receipts VALUES (?, ?, ?)",
        ("receipt-1", "a" * 64, 2),
    )
    seed.commit()
    return seed


def _seed_v2_boundary_source(
    path: Path, boundary: tuple[str, str, int]
) -> sqlite3.Connection:
    seed = sqlite3.connect(path)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("PRAGMA wal_autocheckpoint=0")
    seed.executescript(
        """
        CREATE TABLE route_selections (
            selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
            codex_task_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            selected_at INTEGER NOT NULL
        );
        CREATE TABLE response_links (
            local_response_id TEXT PRIMARY KEY,
            upstream_response_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            codex_task_id TEXT,
            expires_at INTEGER
        );
        CREATE TABLE context_fragments (
            fragment_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            ciphertext BLOB NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER
        );
        CREATE TABLE config_receipts (
            receipt_id TEXT PRIMARY KEY,
            config_sha256 TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE cancel_handles (
            handle_id TEXT PRIMARY KEY,
            codex_task_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER
        );
        CREATE TABLE compact_boundaries (
            codex_task_id TEXT PRIMARY KEY,
            boundary_response_id TEXT NOT NULL,
            boundary_created_at INTEGER NOT NULL
        );
        """
    )
    seed.execute("PRAGMA user_version = 2")
    seed.execute(
        "INSERT INTO response_links VALUES (?, ?, ?, ?, ?, ?)",
        ("r-boundary", "u-boundary", "chat", 300, "task-1", None),
    )
    seed.execute(
        "INSERT INTO compact_boundaries VALUES (?, ?, ?)",
        boundary,
    )
    seed.execute(
        "INSERT INTO config_receipts VALUES (?, ?, ?)",
        ("receipt-1", "a" * 64, 300),
    )
    seed.commit()
    return seed


def test_new_database_uses_wal_and_current_schema(tmp_path, secret_key_provider) -> None:
    path = tmp_path / "state.sqlite3"

    store = StateStore(path, secret_key_provider)

    assert store.schema_version == 3
    assert store.journal_mode == "wal"
    connection = store._require_connection()
    for table, column in (
        ("response_links", "event_sequence"),
        ("context_fragments", "event_sequence"),
        ("compact_boundaries", "boundary_event_sequence"),
    ):
        not_null = next(
            row[3]
            for row in connection.execute(f"PRAGMA table_info({table})")
            if row[1] == column
        )
        assert not_null == 1
    store.close()


@pytest.mark.parametrize(
    ("version", "response_values", "error_pattern"),
    [
        pytest.param(
            1,
            ("", "upstream-valid", "route-valid", 1),
            "identifier",
            id="v1-empty-id",
        ),
        pytest.param(
            1,
            ("local-valid", "upstream-valid", "route\x00invalid", 1),
            "identifier",
            id="v1-nul-id",
        ),
        pytest.param(
            1,
            ("local-valid", "x" * 513, "route-valid", 1),
            "identifier",
            id="v1-oversize-id",
        ),
        pytest.param(
            1,
            ("local-valid", "upstream-valid", "route-valid", -1),
            "non-negative",
            id="v1-negative-created-at",
        ),
        pytest.param(
            2,
            ("local-valid", "upstream-valid", "route-valid", 1, "\x00task", None),
            "identifier",
            id="v2-nul-id",
        ),
        pytest.param(
            2,
            ("local-valid", "upstream-valid", "route-valid", -1, "task-valid", None),
            "non-negative",
            id="v2-negative-created-at",
        ),
        pytest.param(
            2,
            ("local-valid", "upstream-valid", "route-valid", 1, "task-valid", -1),
            "non-negative",
            id="v2-negative-expiry",
        ),
    ],
)
def test_legacy_and_v2_semantic_values_are_quarantined_before_writes(
    tmp_path, secret_key_provider, monkeypatch, version, response_values, error_pattern
) -> None:
    path = tmp_path / "state.sqlite3"
    seed = _seed_semantically_invalid_source(path, version, response_values)
    try:
        sidecars = {
            "-wal": path.with_name(f"{path.name}-wal"),
            "-shm": path.with_name(f"{path.name}-shm"),
        }
        assert all(sidecar.exists() for sidecar in sidecars.values())
        evidence = {"": path.read_bytes()}
        evidence.update({suffix: sidecar.read_bytes() for suffix, sidecar in sidecars.items()})
        connection_modes: list[str] = []
        operation_order: list[str] = []
        original_connect = state_module.sqlite3.connect
        original_copy_file = StateStore._copy_file_with_retry

        def record_connect(database, *args, **kwargs):
            connection_modes.append(
                "ro"
                if kwargs.get("uri") and "?mode=ro" in str(database)
                else "writable"
            )
            return original_connect(database, *args, **kwargs)

        def record_copy_file(source, destination):
            operation_order.append(
                "quarantine_file_copy"
                if ".quarantine-" in Path(destination).name
                else "file_snapshot"
            )
            return original_copy_file(source, destination)

        monkeypatch.setattr(state_module.sqlite3, "connect", record_connect)
        monkeypatch.setattr(
            StateStore, "_copy_file_with_retry", staticmethod(record_copy_file)
        )

        with pytest.raises(DatabaseCorruptionError, match=error_pattern):
            StateStore(path, secret_key_provider)

        assert connection_modes == ["ro", "writable"]
        assert operation_order
        assert operation_order[0] == "quarantine_file_copy"
        assert "file_snapshot" not in operation_order
        assert path.read_bytes() == evidence[""]
        assert sidecars["-wal"].read_bytes() == evidence["-wal"]
        assert sidecars["-shm"].stat().st_size == len(evidence["-shm"])
        quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
        quarantine_main = next(
            item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
        )
        assert quarantine_main.read_bytes() == evidence[""]
        assert (tmp_path / f"{quarantine_main.name}-wal").read_bytes() == evidence["-wal"]
        assert (tmp_path / f"{quarantine_main.name}-shm").stat().st_size == len(
            evidence["-shm"]
        )
    finally:
        seed.close()


@pytest.mark.parametrize(
    "operation",
    [
        "link-created-at",
        "link-expires-at",
        "route-selected-at",
        "fragment-created-at",
        "fragment-expires-at",
        "receipt-created-at",
        "cancel-created-at",
        "cancel-expires-at",
        "purge-now",
    ],
)
def test_public_write_apis_reject_negative_timestamps(
    tmp_path, secret_key_provider, operation
) -> None:
    store = StateStore(tmp_path / "state.sqlite3", secret_key_provider)
    operations = {
        "link-created-at": lambda: store.link_response(
            "local", "upstream", route_id="chat", created_at=-1
        ),
        "link-expires-at": lambda: store.link_response(
            "local", "upstream", route_id="chat", expires_at=-1
        ),
        "route-selected-at": lambda: store.save_route_selection(
            "task", "turn", "chat", selected_at=-1
        ),
        "fragment-created-at": lambda: store.save_chat_fragment(
            "task", "fragment", "fixture", created_at=-1
        ),
        "fragment-expires-at": lambda: store.save_chat_fragment(
            "task", "fragment", "fixture", expires_at=-1
        ),
        "receipt-created-at": lambda: store.save_config_receipt(
            "receipt", "a" * 64, created_at=-1
        ),
        "cancel-created-at": lambda: store.save_cancel_handle(
            "handle", codex_task_id="task", route_id="chat", created_at=-1
        ),
        "cancel-expires-at": lambda: store.save_cancel_handle(
            "handle", codex_task_id="task", route_id="chat", expires_at=-1
        ),
        "purge-now": lambda: store.purge_expired(now=-1),
    }
    try:
        with pytest.raises(ValueError, match="non-negative"):
            operations[operation]()
    finally:
        store.close()


def test_response_link_survives_process_reopen(tmp_path, secret_key_provider) -> None:
    path = tmp_path / "state.sqlite3"
    first = StateStore(path, secret_key_provider)
    first.link_response("local-1", "upstream-7", route_id="deepseek")
    first.close()

    second = StateStore(path, secret_key_provider)
    assert second.get_response_link("local-1").upstream_id == "upstream-7"
    assert second.get_response_link("local-1").route_id == "deepseek"
    second.close()


def test_read_only_source_preflight_precedes_file_snapshot(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    first = StateStore(path, secret_key_provider)
    first.link_response("local-1", "upstream-1", route_id="chat")
    first.close()

    events: list[str] = []
    read_only_sources: list[str] = []
    original_connect = state_module.sqlite3.connect
    original_copy_database_set = StateStore._copy_database_set

    def record_connect(database, *args, **kwargs):
        if kwargs.get("uri") and "?mode=ro" in str(database):
            events.append("read_only_source_preflight")
            read_only_sources.append(str(database))
        else:
            events.append("writable_sqlite_connection")
        return original_connect(database, *args, **kwargs)

    def record_file_snapshot(store, destination):
        events.append("writable_file_snapshot")
        return original_copy_database_set(store, destination)

    monkeypatch.setattr(state_module.sqlite3, "connect", record_connect)
    monkeypatch.setattr(StateStore, "_copy_database_set", record_file_snapshot)

    reopened = StateStore(path, secret_key_provider)
    reopened.close()

    assert read_only_sources == [f"{path.as_uri()}?mode=ro"]
    assert events.index("read_only_source_preflight") < events.index(
        "writable_file_snapshot"
    )


def test_event_sequence_survives_process_reopen(tmp_path, secret_key_provider) -> None:
    path = tmp_path / "state.sqlite3"
    first = StateStore(path, secret_key_provider)
    first_link = first.link_response("local-1", "upstream-1", route_id="chat")
    first.close()

    second = StateStore(path, secret_key_provider)
    second_link = second.link_response("local-2", "upstream-2", route_id="chat")

    assert second_link.event_sequence > first_link.event_sequence
    assert second.get_response_link("local-2").event_sequence == second_link.event_sequence
    second.close()


def test_metadata_writes_keep_event_counter_equal_after_reopen(
    tmp_path, secret_key_provider
) -> None:
    path = tmp_path / "state.sqlite3"
    first = StateStore(path, secret_key_provider)
    first_link = first.link_response("local-1", "upstream-1", route_id="chat")
    first.save_route_selection("task-1", "turn-1", "chat")
    first.save_config_receipt("receipt-1", "a" * 64)
    first.save_cancel_handle("cancel-1", codex_task_id="task-1", route_id="chat")

    def event_state(store: StateStore) -> tuple[int, int]:
        connection = store._require_connection()
        counter = connection.execute(
            "SELECT value FROM event_counters WHERE counter_name = 'state'"
        ).fetchone()[0]
        maximum = connection.execute(
            """
            SELECT MAX(event_sequence) FROM (
                SELECT event_sequence FROM response_links
                UNION ALL
                SELECT event_sequence FROM context_fragments
            )
            """
        ).fetchone()[0]
        return counter, 0 if maximum is None else maximum

    assert first_link.event_sequence == 1
    assert event_state(first) == (1, 1)
    first.close()

    reopened = StateStore(path, secret_key_provider)
    assert event_state(reopened) == (1, 1)
    second_link = reopened.link_response("local-2", "upstream-2", route_id="chat")
    assert second_link.event_sequence == 2
    assert event_state(reopened) == (2, 2)
    reopened.close()


def test_migration_preserves_v1_response_link(tmp_path, secret_key_provider) -> None:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA user_version = 1;
        CREATE TABLE response_links (
            local_response_id TEXT PRIMARY KEY,
            upstream_response_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE route_selections (
            selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
            codex_task_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            selected_at INTEGER NOT NULL
        );
        CREATE TABLE context_fragments (
            fragment_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            ciphertext BLOB NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE config_receipts (
            receipt_id TEXT PRIMARY KEY,
            config_sha256 TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE cancel_handles (
            handle_id TEXT PRIMARY KEY,
            codex_task_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        INSERT INTO response_links VALUES ('old-local', 'old-upstream', 'legacy-route', 1);
        INSERT INTO config_receipts VALUES (
            'receipt-1',
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            2
        );
        """
    )
    connection.commit()
    connection.close()

    store = StateStore(path, secret_key_provider)

    assert store.schema_version == 3
    assert store.get_response_link("old-local").upstream_id == "old-upstream"
    store.close()


def test_v1_invalid_config_sha256_is_quarantined_before_migration_writes(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    seed = sqlite3.connect(path)
    try:
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("PRAGMA wal_autocheckpoint=0")
        seed.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE response_links (
                local_response_id TEXT PRIMARY KEY,
                upstream_response_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE config_receipts (
                receipt_id TEXT PRIMARY KEY,
                config_sha256 TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE route_selections (
                selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                codex_task_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                selected_at INTEGER NOT NULL
            );
            CREATE TABLE context_fragments (
                fragment_id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE cancel_handles (
                handle_id TEXT PRIMARY KEY,
                codex_task_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            INSERT INTO response_links VALUES (
                'old-local', 'old-upstream', 'legacy-route', 1
            );
            INSERT INTO config_receipts VALUES (
                'receipt-1', 'invalid-fixture', 2
            );
            """
        )
        seed.commit()

        sidecars = {
            "-wal": path.with_name(f"{path.name}-wal"),
            "-shm": path.with_name(f"{path.name}-shm"),
        }
        assert all(sidecar.exists() for sidecar in sidecars.values())
        evidence = {"": path.read_bytes()}
        evidence.update({suffix: sidecar.read_bytes() for suffix, sidecar in sidecars.items()})
        connection_modes: list[str] = []
        original_connect = state_module.sqlite3.connect

        def record_connect(database, *args, **kwargs):
            connection_modes.append(
                "ro"
                if kwargs.get("uri") and "?mode=ro" in str(database)
                else "writable"
            )
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(state_module.sqlite3, "connect", record_connect)

        with pytest.raises(DatabaseCorruptionError, match="config_sha256"):
            StateStore(path, secret_key_provider)

        assert connection_modes == ["ro", "writable"]
        assert path.read_bytes() == evidence[""]
        assert sidecars["-wal"].read_bytes() == evidence["-wal"]
        assert sidecars["-shm"].stat().st_size == len(evidence["-shm"])
        quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
        quarantine_main = next(
            item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
        )
        assert quarantine_main.read_bytes() == evidence[""]
        assert (tmp_path / f"{quarantine_main.name}-wal").read_bytes() == evidence["-wal"]
        assert (tmp_path / f"{quarantine_main.name}-shm").stat().st_size == len(
            evidence["-shm"]
        )
    finally:
        seed.close()


@pytest.mark.parametrize(
    ("response_link_definition", "response_link_values"),
    [
        pytest.param(
            """
            CREATE TABLE response_links (
                local_response_id TEXT PRIMARY KEY,
                upstream_response_id TEXT NOT NULL,
                route_id TEXT NOT NULL
            )
            """,
            ("old-local", "old-upstream", "legacy-route"),
            id="missing-created-at",
        ),
        pytest.param(
            """
            CREATE TABLE response_links (
                local_response_id TEXT PRIMARY KEY,
                upstream_response_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            ("old-local", "old-upstream", "legacy-route", "not-an-integer"),
            id="created-at-wrong-type",
        ),
        pytest.param(
            """
            CREATE TABLE response_links (
                local_response_id TEXT PRIMARY KEY,
                upstream_response_id TEXT NOT NULL,
                route_id TEXT,
                created_at INTEGER NOT NULL
            )
            """,
            ("old-local", "old-upstream", None, 1),
            id="route-id-nullable",
        ),
    ],
)
def test_v1_incomplete_schema_is_quarantined_before_writable_snapshot(
    tmp_path, secret_key_provider, monkeypatch, response_link_definition, response_link_values
) -> None:
    path = tmp_path / "state.sqlite3"
    seed = sqlite3.connect(path)
    try:
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("PRAGMA wal_autocheckpoint=0")
        seed.executescript(
            """
            CREATE TABLE route_selections (
                selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                codex_task_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                selected_at INTEGER NOT NULL
            );
            CREATE TABLE context_fragments (
                fragment_id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE config_receipts (
                receipt_id TEXT PRIMARY KEY,
                config_sha256 TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE cancel_handles (
                handle_id TEXT PRIMARY KEY,
                codex_task_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )
        seed.execute(response_link_definition)
        placeholders = ", ".join("?" for _ in response_link_values)
        seed.execute(
            f"INSERT INTO response_links VALUES ({placeholders})", response_link_values
        )
        seed.execute(
            "INSERT INTO config_receipts VALUES (?, ?, ?)",
            ("receipt-1", "a" * 64, 2),
        )
        seed.commit()

        sidecars = {
            "-wal": path.with_name(f"{path.name}-wal"),
            "-shm": path.with_name(f"{path.name}-shm"),
        }
        assert all(sidecar.exists() for sidecar in sidecars.values())
        evidence = {"": path.read_bytes()}
        evidence.update({suffix: sidecar.read_bytes() for suffix, sidecar in sidecars.items()})
        connection_modes: list[str] = []
        original_connect = state_module.sqlite3.connect

        def record_connect(database, *args, **kwargs):
            connection_modes.append(
                "ro"
                if kwargs.get("uri") and "?mode=ro" in str(database)
                else "writable"
            )
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(state_module.sqlite3, "connect", record_connect)

        with pytest.raises(DatabaseCorruptionError, match="schema"):
            StateStore(path, secret_key_provider)

        assert connection_modes == ["ro", "writable"]
        assert path.read_bytes() == evidence[""]
        assert sidecars["-wal"].read_bytes() == evidence["-wal"]
        assert sidecars["-shm"].stat().st_size == len(evidence["-shm"])
        quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
        quarantine_main = next(
            item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
        )
        assert quarantine_main.read_bytes() == evidence[""]
        assert (tmp_path / f"{quarantine_main.name}-wal").read_bytes() == evidence["-wal"]
        assert (tmp_path / f"{quarantine_main.name}-shm").stat().st_size == len(
            evidence["-shm"]
        )
    finally:
        seed.close()


def test_v2_migration_interleaves_history_before_compact_prune(
    tmp_path, secret_key_provider
) -> None:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA user_version = 2;
        CREATE TABLE route_selections (
            selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
            codex_task_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            selected_at INTEGER NOT NULL
        );
        CREATE TABLE response_links (
            local_response_id TEXT PRIMARY KEY,
            upstream_response_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            codex_task_id TEXT,
            expires_at INTEGER
        );
        CREATE TABLE context_fragments (
            fragment_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            ciphertext BLOB NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER
        );
        CREATE TABLE config_receipts (
            receipt_id TEXT PRIMARY KEY,
            config_sha256 TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE cancel_handles (
            handle_id TEXT PRIMARY KEY,
            codex_task_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER
        );
        CREATE TABLE compact_boundaries (
            codex_task_id TEXT PRIMARY KEY,
            boundary_response_id TEXT NOT NULL,
            boundary_created_at INTEGER NOT NULL
        );
        """
    )
    connection.executemany(
        "INSERT INTO response_links VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("r-old", "u-old", "chat", 100, "task-1", None),
            ("r-tie", "u-tie", "chat", 250, "task-1", None),
            ("r-boundary", "u-boundary", "chat", 300, "task-1", None),
        ],
    )
    connection.execute(
        "INSERT INTO compact_boundaries VALUES (?, ?, ?)",
        ("task-1", "r-boundary", 300),
    )
    cipher = Fernet(secret_key_provider.get_key())
    connection.executemany(
        "INSERT INTO context_fragments VALUES (?, ?, ?, ?, ?)",
        [
            ("f-old", "task-1", cipher.encrypt(b"old"), 200, None),
            ("f-tie", "task-1", cipher.encrypt(b"tie"), 250, None),
            ("f-new", "task-1", cipher.encrypt(b"new"), 400, None),
        ],
    )
    connection.commit()
    connection.close()

    store = StateStore(path, secret_key_provider)
    rows = store._require_connection().execute(
        """
        SELECT local_response_id AS legacy_id, event_sequence, 'response' AS record_type
        FROM response_links
        UNION ALL
        SELECT fragment_id, event_sequence, 'fragment'
        FROM context_fragments
        ORDER BY event_sequence
        """
    ).fetchall()
    assert [(row[0], row[2]) for row in rows] == [
        ("r-old", "response"),
        ("f-old", "fragment"),
        ("r-tie", "response"),
        ("f-tie", "fragment"),
        ("r-boundary", "response"),
        ("f-new", "fragment"),
    ]
    assert [row[1] for row in rows] == list(range(1, 7))
    assert store._require_connection().execute(
        "SELECT boundary_response_id, boundary_created_at, boundary_event_sequence "
        "FROM compact_boundaries WHERE codex_task_id = 'task-1'"
    ).fetchone() == ("r-boundary", 300, 5)

    assert store.prune_after_compact("task-1", "r-boundary") == 4
    assert store.get_response_link("r-old") is None
    assert store.get_chat_fragment("f-old") is None
    assert store.get_chat_fragment("f-tie") is None
    assert store.get_response_link("r-boundary") is not None
    assert store.get_chat_fragment("f-new") == "new"
    store.close()


@pytest.mark.parametrize(
    "boundary",
    [
        pytest.param(
            ("task-1", "missing-response", 300), id="missing-response"
        ),
        pytest.param(("task-2", "r-boundary", 300), id="different-task"),
        pytest.param(("task-1", "r-boundary", 301), id="created-at-mismatch"),
    ],
)
def test_v2_compact_boundary_semantics_are_quarantined_before_writes(
    tmp_path, secret_key_provider, monkeypatch, boundary
) -> None:
    path = tmp_path / "state.sqlite3"
    seed = _seed_v2_boundary_source(path, boundary)
    try:
        sidecars = {
            "-wal": path.with_name(f"{path.name}-wal"),
            "-shm": path.with_name(f"{path.name}-shm"),
        }
        assert all(sidecar.exists() for sidecar in sidecars.values())
        evidence = {"": path.read_bytes()}
        evidence.update({suffix: sidecar.read_bytes() for suffix, sidecar in sidecars.items()})
        connection_modes: list[str] = []
        original_connect = state_module.sqlite3.connect

        def record_connect(database, *args, **kwargs):
            connection_modes.append(
                "ro"
                if kwargs.get("uri") and "?mode=ro" in str(database)
                else "writable"
            )
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(state_module.sqlite3, "connect", record_connect)

        with pytest.raises(DatabaseCorruptionError, match="compact boundary"):
            StateStore(path, secret_key_provider)

        assert connection_modes == ["ro", "writable"]
        assert path.read_bytes() == evidence[""]
        assert sidecars["-wal"].read_bytes() == evidence["-wal"]
        assert sidecars["-shm"].stat().st_size == len(evidence["-shm"])
        quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
        quarantine_main = next(
            item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
        )
        assert quarantine_main.read_bytes() == evidence[""]
        assert (tmp_path / f"{quarantine_main.name}-wal").read_bytes() == evidence["-wal"]
        assert (tmp_path / f"{quarantine_main.name}-shm").stat().st_size == len(
            evidence["-shm"]
        )
    finally:
        seed.close()


def test_incomplete_v3_schema_is_quarantined_before_first_write(
    tmp_path, secret_key_provider
) -> None:
    path = tmp_path / "state.sqlite3"
    seed = sqlite3.connect(path)
    try:
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("PRAGMA wal_autocheckpoint=0")
        seed.execute(
            """
            CREATE TABLE response_links (
                local_response_id TEXT PRIMARY KEY,
                upstream_response_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                codex_task_id TEXT,
                expires_at INTEGER,
                event_sequence INTEGER
            )
            """
        )
        seed.execute("PRAGMA user_version = 3")
        seed.execute(
            "INSERT INTO response_links VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("local-1", "upstream-1", "chat", 1, "task-1", None, 1),
        )
        seed.commit()

        sidecars = {
            "-wal": path.with_name(f"{path.name}-wal"),
            "-shm": path.with_name(f"{path.name}-shm"),
        }
        assert all(sidecar.exists() for sidecar in sidecars.values())
        evidence = {"": path.read_bytes()}
        evidence.update({suffix: sidecar.read_bytes() for suffix, sidecar in sidecars.items()})

        with pytest.raises(DatabaseCorruptionError, match="schema"):
            StateStore(path, secret_key_provider)

        quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
        quarantine_main = next(
            item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
        )
        assert quarantine_main.read_bytes() == evidence[""]
        assert (tmp_path / f"{quarantine_main.name}-wal").read_bytes() == evidence["-wal"]
        assert (tmp_path / f"{quarantine_main.name}-shm").stat().st_size == len(
            evidence["-shm"]
        )
        assert path.read_bytes() == evidence[""]
        assert sidecars["-wal"].read_bytes() == evidence["-wal"]
        assert sidecars["-shm"].stat().st_size == len(evidence["-shm"])
    finally:
        seed.close()


@pytest.mark.parametrize(
    ("boundary_definition", "boundary_value", "error_pattern"),
    [
        pytest.param("INTEGER", None, "schema", id="boundary-sequence-null"),
        pytest.param(
            "INTEGER NOT NULL", -1, "boundary event sequence", id="boundary-sequence-negative"
        ),
    ],
)
def test_v3_compact_boundary_sequence_is_required_and_validated(
    tmp_path, secret_key_provider, monkeypatch, boundary_definition, boundary_value, error_pattern
) -> None:
    path = tmp_path / "state.sqlite3"
    seed = sqlite3.connect(path)
    try:
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("PRAGMA wal_autocheckpoint=0")
        seed.executescript(
            """
            CREATE TABLE route_selections (
                selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                codex_task_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                selected_at INTEGER NOT NULL
            );
            CREATE TABLE response_links (
                local_response_id TEXT PRIMARY KEY,
                upstream_response_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                codex_task_id TEXT,
                expires_at INTEGER,
                event_sequence INTEGER NOT NULL
            );
            CREATE TABLE context_fragments (
                fragment_id TEXT PRIMARY KEY,
                scope_id TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER,
                event_sequence INTEGER NOT NULL
            );
            CREATE TABLE config_receipts (
                receipt_id TEXT PRIMARY KEY,
                config_sha256 TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE cancel_handles (
                handle_id TEXT PRIMARY KEY,
                codex_task_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER
            );
            CREATE TABLE event_counters (
                counter_name TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            """
        )
        seed.execute(
            f"""
            CREATE TABLE compact_boundaries (
                codex_task_id TEXT PRIMARY KEY,
                boundary_response_id TEXT NOT NULL,
                boundary_created_at INTEGER NOT NULL,
                boundary_event_sequence {boundary_definition}
            )
            """
        )
        seed.execute("PRAGMA user_version = 3")
        seed.execute(
            "INSERT INTO response_links VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("local-1", "upstream-1", "chat", 10, "task-1", None, 1),
        )
        seed.execute(
            "INSERT INTO compact_boundaries VALUES (?, ?, ?, ?)",
            ("task-1", "local-1", 10, boundary_value),
        )
        seed.execute(
            "INSERT INTO event_counters VALUES ('state', 1)"
        )
        seed.commit()

        sidecars = {
            "-wal": path.with_name(f"{path.name}-wal"),
            "-shm": path.with_name(f"{path.name}-shm"),
        }
        assert all(sidecar.exists() for sidecar in sidecars.values())
        evidence = {"": path.read_bytes()}
        evidence.update({suffix: sidecar.read_bytes() for suffix, sidecar in sidecars.items()})
        connection_modes: list[str] = []
        original_connect = state_module.sqlite3.connect

        def record_connect(database, *args, **kwargs):
            connection_modes.append(
                "ro"
                if kwargs.get("uri") and "?mode=ro" in str(database)
                else "writable"
            )
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(state_module.sqlite3, "connect", record_connect)

        with pytest.raises(DatabaseCorruptionError, match=error_pattern):
            StateStore(path, secret_key_provider)

        assert connection_modes == ["ro", "writable"]
        assert path.read_bytes() == evidence[""]
        assert sidecars["-wal"].read_bytes() == evidence["-wal"]
        assert sidecars["-shm"].stat().st_size == len(evidence["-shm"])
        quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
        quarantine_main = next(
            item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
        )
        assert quarantine_main.read_bytes() == evidence[""]
        assert (tmp_path / f"{quarantine_main.name}-wal").read_bytes() == evidence["-wal"]
        assert (tmp_path / f"{quarantine_main.name}-shm").stat().st_size == len(
            evidence["-shm"]
        )
    finally:
        seed.close()


def test_incomplete_v2_schema_is_quarantined_before_wal_setup(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    seed = sqlite3.connect(path)
    try:
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("PRAGMA wal_autocheckpoint=0")
        seed.execute(
            """
            CREATE TABLE response_links (
                local_response_id TEXT PRIMARY KEY,
                upstream_response_id TEXT NOT NULL,
                route_id TEXT NOT NULL,
                codex_task_id TEXT,
                expires_at INTEGER
            )
            """
        )
        seed.execute("PRAGMA user_version = 2")
        seed.execute(
            "INSERT INTO response_links VALUES (?, ?, ?, ?, ?)",
            ("local-1", "upstream-1", "chat", "task-1", None),
        )
        seed.commit()

        sidecars = {
            "-wal": path.with_name(f"{path.name}-wal"),
            "-shm": path.with_name(f"{path.name}-shm"),
        }
        assert all(sidecar.exists() for sidecar in sidecars.values())
        evidence = {"": path.read_bytes()}
        evidence.update({suffix: sidecar.read_bytes() for suffix, sidecar in sidecars.items()})
        connection_modes: list[str] = []
        original_connect = state_module.sqlite3.connect

        def record_connect(database, *args, **kwargs):
            connection_modes.append(
                "ro"
                if kwargs.get("uri") and "?mode=ro" in str(database)
                else "writable"
            )
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(state_module.sqlite3, "connect", record_connect)
        monkeypatch.setattr(
            StateStore,
            "_configure_connection",
            lambda _store: pytest.fail("writable WAL setup ran before v2 preflight"),
        )

        with pytest.raises(DatabaseCorruptionError, match="schema"):
            StateStore(path, secret_key_provider)

        assert connection_modes == ["ro", "writable"]
        quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
        quarantine_main = next(
            item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
        )
        assert quarantine_main.read_bytes() == evidence[""]
        assert (tmp_path / f"{quarantine_main.name}-wal").read_bytes() == evidence["-wal"]
        assert (tmp_path / f"{quarantine_main.name}-shm").stat().st_size == len(
            evidence["-shm"]
        )
        assert path.read_bytes() == evidence[""]
        assert sidecars["-wal"].read_bytes() == evidence["-wal"]
        assert sidecars["-shm"].stat().st_size == len(evidence["-shm"])
    finally:
        seed.close()


def test_reopen_rejects_invalid_existing_config_sha256_before_writes(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    store = StateStore(path, secret_key_provider)
    store.save_config_receipt("receipt-1", "a" * 64)
    store.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE config_receipts SET config_sha256 = 'invalid-fixture' "
            "WHERE receipt_id = 'receipt-1'"
        )
    original_main = path.read_bytes()
    monkeypatch.setattr(
        StateStore,
        "_configure_connection",
        lambda _store: pytest.fail("writable WAL setup ran before hash preflight"),
    )

    with pytest.raises(DatabaseCorruptionError, match="config_sha256"):
        StateStore(path, secret_key_provider)

    assert path.read_bytes() == original_main
    quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
    quarantine_main = next(
        item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
    )
    assert quarantine_main.read_bytes() == original_main


def test_v2_migration_rejects_invalid_existing_config_sha256_before_writes(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA user_version = 2;
        CREATE TABLE route_selections (
            selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
            codex_task_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            selected_at INTEGER NOT NULL
        );
        CREATE TABLE response_links (
            local_response_id TEXT PRIMARY KEY,
            upstream_response_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            codex_task_id TEXT,
            expires_at INTEGER
        );
        CREATE TABLE context_fragments (
            fragment_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            ciphertext BLOB NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER
        );
        CREATE TABLE config_receipts (
            receipt_id TEXT PRIMARY KEY,
            config_sha256 TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE cancel_handles (
            handle_id TEXT PRIMARY KEY,
            codex_task_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER
        );
        CREATE TABLE compact_boundaries (
            codex_task_id TEXT PRIMARY KEY,
            boundary_response_id TEXT NOT NULL,
            boundary_created_at INTEGER NOT NULL
        );
        INSERT INTO config_receipts VALUES ('receipt-1', 'invalid-fixture', 1);
        """
    )
    connection.commit()
    connection.close()
    original_main = path.read_bytes()
    monkeypatch.setattr(
        StateStore,
        "_configure_connection",
        lambda _store: pytest.fail("writable WAL setup ran before v2 hash preflight"),
    )

    with pytest.raises(DatabaseCorruptionError, match="config_sha256"):
        StateStore(path, secret_key_provider)

    assert path.read_bytes() == original_main
    quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
    quarantine_main = next(
        item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
    )
    assert quarantine_main.read_bytes() == original_main


@pytest.mark.parametrize(
    "corruption", ["counter_zero", "counter_high", "duplicate", "missing", "invalid"]
)
def test_inconsistent_event_state_is_quarantined_before_writes(
    tmp_path, secret_key_provider, monkeypatch, corruption
) -> None:
    path = tmp_path / "state.sqlite3"
    store = StateStore(path, secret_key_provider)
    store.link_response("local-1", "upstream-1", route_id="chat", codex_task_id="task-1")
    store.link_response("local-2", "upstream-2", route_id="chat", codex_task_id="task-1")
    store.close()

    with sqlite3.connect(path) as connection:
        if corruption == "counter_zero":
            connection.execute(
                "UPDATE event_counters SET value = 0 WHERE counter_name = 'state'"
            )
        elif corruption == "counter_high":
            connection.execute(
                "UPDATE event_counters SET value = 3 WHERE counter_name = 'state'"
            )
        elif corruption == "duplicate":
            connection.execute(
                "UPDATE response_links SET event_sequence = 1 "
                "WHERE local_response_id = 'local-2'"
            )
        elif corruption == "missing":
            connection.execute("ALTER TABLE response_links RENAME TO response_links_old")
            connection.execute(
                """
                CREATE TABLE response_links (
                    local_response_id TEXT PRIMARY KEY,
                    upstream_response_id TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    codex_task_id TEXT,
                    expires_at INTEGER,
                    event_sequence INTEGER
                )
                """
            )
            connection.execute(
                """
                INSERT INTO response_links (
                    local_response_id, upstream_response_id, route_id, created_at,
                    codex_task_id, expires_at, event_sequence
                )
                SELECT local_response_id, upstream_response_id, route_id, created_at,
                       codex_task_id, expires_at,
                       CASE local_response_id
                           WHEN 'local-2' THEN NULL
                           ELSE event_sequence
                       END
                FROM response_links_old
                """
            )
            connection.execute("DROP TABLE response_links_old")
        else:
            connection.execute(
                "UPDATE response_links SET event_sequence = 0 "
                "WHERE local_response_id = 'local-2'"
            )

    original_main = path.read_bytes()
    monkeypatch.setattr(
        StateStore,
        "_configure_connection",
        lambda _store: pytest.fail("writable WAL setup ran before event-state preflight"),
    )
    with pytest.raises(DatabaseCorruptionError, match="(event (sequence|counter)|schema)"):
        StateStore(path, secret_key_provider)

    assert path.read_bytes() == original_main
    quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
    quarantine_main = next(
        item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
    )
    assert quarantine_main.read_bytes() == original_main


def test_invalid_shm_sidecar_is_quarantined_beside_valid_database(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    store = StateStore(path, secret_key_provider)
    store.link_response("local-1", "upstream-1", route_id="chat")
    store.close()

    shm_path = path.with_name(f"{path.name}-shm")
    shm_path.write_bytes(b"invalid shm fixture")
    original_main = path.read_bytes()
    original_shm = shm_path.read_bytes()
    monkeypatch.setattr(
        StateStore,
        "_configure_connection",
        lambda _store: pytest.fail("invalid shm must fail before writable setup"),
    )

    with pytest.raises(DatabaseCorruptionError, match="shm"):
        StateStore(path, secret_key_provider)

    assert path.read_bytes() == original_main
    assert shm_path.read_bytes() == original_shm
    quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
    quarantine_main = next(
        item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
    )
    assert quarantine_main.read_bytes() == original_main
    assert (tmp_path / f"{quarantine_main.name}-shm").read_bytes() == original_shm


def test_quarantine_sidecar_failure_is_explicit_and_not_success(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    wal_path = path.with_name(f"{path.name}-wal")
    shm_path = path.with_name(f"{path.name}-shm")
    path.write_bytes(b"not a sqlite database")
    wal_path.write_bytes(b"wal evidence")
    shm_path.write_bytes(b"shm evidence")
    original_copy2 = state_module.shutil.copy2

    def fail_quarantine_wal(source, destination, *args, **kwargs):
        if Path(destination).name.endswith("-wal") and ".quarantine-" in Path(
            destination
        ).name:
            raise PermissionError(33, "sharing violation", str(source))
        return original_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(state_module.shutil, "copy2", fail_quarantine_wal)

    with pytest.raises(DatabaseQuarantineError, match="quarantine incomplete") as error:
        StateStore(path, secret_key_provider)

    assert set(error.value.copied_suffixes) == {"main", "-shm"}
    assert error.value.failed_suffixes == ("-wal",)
    quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
    quarantine_main = next(
        item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
    )
    assert quarantine_main.read_bytes() == b"not a sqlite database"
    assert not (tmp_path / f"{quarantine_main.name}-wal").exists()
    assert (tmp_path / f"{quarantine_main.name}-shm").read_bytes() == b"shm evidence"
    assert path.read_bytes() == b"not a sqlite database"
    assert wal_path.read_bytes() == b"wal evidence"
    assert shm_path.read_bytes() == b"shm evidence"


def test_chat_fragment_is_encrypted_and_reopens(tmp_path, secret_key_provider) -> None:
    path = tmp_path / "state.sqlite3"
    fragment = "只保存第三方 Chat 继续一轮所需的最小文本片段"
    first = StateStore(path, secret_key_provider)
    first.save_chat_fragment("task-1", "fragment-1", fragment)
    first.close()

    raw = path.read_bytes()
    assert fragment.encode("utf-8") not in raw

    second = StateStore(path, secret_key_provider)
    assert second.get_chat_fragment("fragment-1") == fragment
    second.close()


def test_compact_prune_removes_only_old_chain_and_keeps_boundary(
    tmp_path, secret_key_provider
) -> None:
    path = tmp_path / "state.sqlite3"
    store = StateStore(path, secret_key_provider)
    store.link_response("local-old", "upstream-old", route_id="chat", codex_task_id="task-1")
    store.save_chat_fragment("task-1", "fragment-old", "old continuation fragment")
    store.link_response(
        "local-boundary", "upstream-boundary", route_id="chat", codex_task_id="task-1"
    )
    store.save_chat_fragment("task-1", "fragment-boundary", "boundary continuation fragment")
    store.link_response("local-new", "upstream-new", route_id="chat", codex_task_id="task-1")

    removed = store.prune_after_compact("task-1", "local-boundary")

    assert removed == 2
    assert store.get_response_link("local-old") is None
    assert store.get_response_link("local-boundary").upstream_id == "upstream-boundary"
    assert store.get_response_link("local-new").upstream_id == "upstream-new"
    assert store.get_chat_fragment("fragment-old") is None
    assert store.get_chat_fragment("fragment-boundary") == "boundary continuation fragment"
    store.close()


def test_compact_prune_uses_event_order_when_timestamps_are_equal(
    tmp_path, secret_key_provider
) -> None:
    path = tmp_path / "state.sqlite3"
    store = StateStore(path, secret_key_provider)
    store.link_response(
        "local-old", "upstream-old", route_id="chat", codex_task_id="task-1", created_at=100
    )
    store.save_chat_fragment(
        "task-1", "fragment-old", "old continuation fragment", created_at=100
    )
    store.link_response(
        "local-boundary",
        "upstream-boundary",
        route_id="chat",
        codex_task_id="task-1",
        created_at=100,
    )
    store.save_chat_fragment(
        "task-1", "fragment-boundary", "boundary continuation fragment", created_at=100
    )

    removed = store.prune_after_compact("task-1", "local-boundary")

    assert removed == 2
    assert store.get_response_link("local-old") is None
    assert store.get_chat_fragment("fragment-old") is None
    assert store.get_response_link("local-boundary").upstream_id == "upstream-boundary"
    assert store.get_chat_fragment("fragment-boundary") == "boundary continuation fragment"
    store.close()


def test_compact_boundary_rejects_response_mutation_and_expiry_purge(
    tmp_path, secret_key_provider
) -> None:
    path = tmp_path / "state.sqlite3"
    store = StateStore(path, secret_key_provider)
    store.link_response(
        "local-boundary",
        "upstream-boundary",
        route_id="chat",
        codex_task_id="task-1",
        created_at=100,
        expires_at=20,
    )
    store.prune_after_compact("task-1", "local-boundary")
    store.link_response(
        "local-other",
        "upstream-other",
        route_id="chat",
        codex_task_id="task-1",
        expires_at=20,
    )
    boundary_before = store.get_response_link("local-boundary")
    counter_before = store._require_connection().execute(
        "SELECT value FROM event_counters WHERE counter_name = 'state'"
    ).fetchone()[0]

    with pytest.raises(StateError, match="compact boundary"):
        store.link_response(
            "local-boundary",
            "upstream-mutated",
            route_id="other-route",
            codex_task_id="task-2",
            created_at=101,
        )

    assert store.get_response_link("local-boundary") == boundary_before
    assert store._require_connection().execute(
        "SELECT value FROM event_counters WHERE counter_name = 'state'"
    ).fetchone()[0] == counter_before

    with pytest.raises(StateError, match="compact boundary"):
        store.purge_expired(now=20)

    assert store.get_response_link("local-boundary") == boundary_before
    assert store.get_response_link("local-other") is not None
    assert store._require_connection().execute(
        "SELECT value FROM event_counters WHERE counter_name = 'state'"
    ).fetchone()[0] == counter_before
    store.close()

    reopened = StateStore(path, secret_key_provider)
    assert reopened.get_response_link("local-boundary") == boundary_before
    reopened.close()


def test_expired_mapping_cleanup_is_transactional_and_scoped(tmp_path, secret_key_provider) -> None:
    path = tmp_path / "state.sqlite3"
    store = StateStore(path, secret_key_provider)
    store.link_response(
        "expired", "upstream-expired", route_id="chat", codex_task_id="task-1", expires_at=10
    )
    store.link_response(
        "live", "upstream-live", route_id="chat", codex_task_id="task-1", expires_at=20
    )

    assert store.purge_expired(now=15) == 1
    assert store.get_response_link("expired") is None
    assert store.get_response_link("live").upstream_id == "upstream-live"
    store.close()


def test_expiring_highest_event_reconciles_counter_across_reopen(
    tmp_path, secret_key_provider
) -> None:
    path = tmp_path / "state.sqlite3"
    store = StateStore(path, secret_key_provider)
    store.link_response(
        "expired", "upstream-expired", route_id="chat", expires_at=10
    )

    assert store.purge_expired(now=10) == 1
    connection = store._require_connection()
    assert connection.execute(
        "SELECT value FROM event_counters WHERE counter_name = 'state'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT MAX(event_sequence) FROM response_links"
    ).fetchone()[0] is None
    store.close()

    reopened = StateStore(path, secret_key_provider)
    next_link = reopened.link_response("next", "upstream-next", route_id="chat")
    assert next_link.event_sequence == 1
    reopened.close()


def test_corrupt_database_is_quarantined_with_wal_sidecars_before_writes(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    path.write_bytes(b"not a sqlite database")
    wal_path = path.with_name(f"{path.name}-wal")
    shm_path = path.with_name(f"{path.name}-shm")
    wal_path.write_bytes(b"wal evidence")
    shm_path.write_bytes(b"shm evidence")

    def unexpected_write_pragma(_store) -> None:
        pytest.fail("corruption must be checked before the writable connection is configured")

    monkeypatch.setattr(StateStore, "_configure_connection", unexpected_write_pragma)

    with pytest.raises(DatabaseCorruptionError):
        StateStore(path, secret_key_provider)

    quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
    assert len(quarantine_files) == 3
    quarantine_main = next(
        item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
    )
    quarantine_wal = tmp_path / f"{quarantine_main.name}-wal"
    quarantine_shm = tmp_path / f"{quarantine_main.name}-shm"
    assert quarantine_wal.read_bytes() == b"wal evidence"
    assert quarantine_shm.read_bytes() == b"shm evidence"
    assert quarantine_main.read_bytes() == b"not a sqlite database"
    assert path.read_bytes() == b"not a sqlite database"
    assert wal_path.read_bytes() == b"wal evidence"
    assert shm_path.read_bytes() == b"shm evidence"


def test_corrupt_wal_is_quarantined_with_the_matching_database_set(
    tmp_path, secret_key_provider
) -> None:
    path = tmp_path / "state.sqlite3"
    seed = sqlite3.connect(path)
    try:
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("PRAGMA wal_autocheckpoint=0")
        seed.execute("CREATE TABLE marker (value BLOB NOT NULL)")
        seed.commit()
        seed.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        seed.execute("INSERT INTO marker VALUES (?)", (b"x" * 100_000,))
        seed.commit()

        wal_path = path.with_name(f"{path.name}-wal")
        shm_path = path.with_name(f"{path.name}-shm")
        main_before = path.read_bytes()
        shm_before = shm_path.read_bytes()
        wal_bytes = bytearray(wal_path.read_bytes())
        page_size = int.from_bytes(wal_bytes[8:12], "big")
        frame_size = 24 + page_size
        frame_count = (len(wal_bytes) - 32) // frame_size
        last_frame = 32 + (frame_count - 1) * frame_size
        wal_bytes[last_frame + 24] ^= 0xFF
        wal_path.write_bytes(wal_bytes)
        corrupted_wal = bytes(wal_bytes)

        with pytest.raises(DatabaseCorruptionError):
            StateStore(path, secret_key_provider)

        quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
        quarantine_main = next(
            item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
        )
        assert quarantine_main.read_bytes() == main_before
        assert (tmp_path / f"{quarantine_main.name}-wal").read_bytes() == corrupted_wal
        quarantine_shm = tmp_path / f"{quarantine_main.name}-shm"
        assert quarantine_shm.exists()
        assert quarantine_shm.stat().st_size == len(shm_before)
    finally:
        seed.close()


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
def test_orphaned_sidecar_is_quarantined_without_creating_empty_database(
    tmp_path, secret_key_provider, sidecar_suffix
) -> None:
    path = tmp_path / "state.sqlite3"
    sidecar_path = path.with_name(f"{path.name}{sidecar_suffix}")
    sidecar_path.write_bytes(b"orphan sidecar evidence")

    with pytest.raises(DatabaseCorruptionError, match="orphan"):
        StateStore(path, secret_key_provider)

    assert not path.exists()
    quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
    assert len(quarantine_files) == 1
    assert quarantine_files[0].name.endswith(sidecar_suffix)
    assert quarantine_files[0].read_bytes() == b"orphan sidecar evidence"


def test_orphaned_sidecar_quarantine_captures_main_if_it_appears(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    wal_path = path.with_name(f"{path.name}-wal")
    shm_path = path.with_name(f"{path.name}-shm")
    wal_path.write_bytes(b"orphan wal evidence")
    shm_path.write_bytes(b"orphan shm evidence")

    original_copy2 = state_module.shutil.copy2

    def create_main_during_wal_copy(source, destination, *args, **kwargs):
        if Path(source) == wal_path and not path.exists():
            path.write_bytes(b"main appeared evidence")
        return original_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(state_module.shutil, "copy2", create_main_during_wal_copy)

    with pytest.raises(DatabaseCorruptionError, match="orphan"):
        StateStore(path, secret_key_provider)

    quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
    assert len(quarantine_files) == 3
    quarantine_main = next(
        item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
    )
    assert quarantine_main.read_bytes() == b"main appeared evidence"
    assert (tmp_path / f"{quarantine_main.name}-wal").read_bytes() == b"orphan wal evidence"
    assert (tmp_path / f"{quarantine_main.name}-shm").read_bytes() == b"orphan shm evidence"
    assert path.read_bytes() == b"main appeared evidence"


def test_reopen_retries_transient_wal_copy_permission_error_during_writer(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    initial = StateStore(path, secret_key_provider)
    initial.link_response("local-1", "upstream-1", route_id="chat")
    initial.close()

    seed = sqlite3.connect(path)
    try:
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("PRAGMA wal_autocheckpoint=0")
        seed.execute("CREATE TABLE writer_marker (value BLOB NOT NULL)")
        seed.commit()
        seed.execute("INSERT INTO writer_marker VALUES (?)", (b"x" * 100_000,))
        seed.commit()

        original_copy2 = state_module.shutil.copy2
        failures = 0

        def flaky_copy2(source, destination, *args, **kwargs):
            nonlocal failures
            if str(source).endswith("-wal") and failures < 2:
                failures += 1
                raise PermissionError(33, "sharing violation", str(source))
            return original_copy2(source, destination, *args, **kwargs)

        monkeypatch.setattr(state_module.shutil, "copy2", flaky_copy2)

        reopened = StateStore(path, secret_key_provider)
        assert reopened.get_response_link("local-1").upstream_id == "upstream-1"
        reopened.close()
        assert failures == 2
    finally:
        seed.close()


def test_reopen_captures_wal_snapshot_during_concurrent_writer(
    tmp_path, secret_key_provider
) -> None:
    path = tmp_path / "state.sqlite3"
    initial = StateStore(path, secret_key_provider)
    initial.link_response("local-1", "upstream-1", route_id="chat")
    initial.close()

    writer_started = Event()
    stop_writer = Event()
    writer_errors: list[BaseException] = []

    def write_wal_rows() -> None:
        writer = sqlite3.connect(path, timeout=30.0)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE writer_marker (value BLOB NOT NULL)")
            writer.commit()
            for _index in range(100):
                if stop_writer.is_set():
                    break
                writer.execute("INSERT INTO writer_marker VALUES (?)", (b"writer",))
                writer.commit()
                writer_started.set()
                stop_writer.wait(0.005)
        except BaseException as exc:  # pragma: no cover - surfaced below
            writer_errors.append(exc)
        finally:
            writer.close()

    writer_thread = Thread(target=write_wal_rows)
    writer_thread.start()
    assert writer_started.wait(5)

    try:
        reopened = StateStore(path, secret_key_provider)
        assert reopened.get_response_link("local-1").upstream_id == "upstream-1"
        reopened.close()
    finally:
        stop_writer.set()
        writer_thread.join(5)
        assert not writer_thread.is_alive()
        assert not writer_errors


def test_persistent_wal_snapshot_permission_error_is_explicit_and_quarantined(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    path.write_bytes(b"not a sqlite database")
    wal_path = path.with_name(f"{path.name}-wal")
    wal_path.write_bytes(b"wal evidence")

    original_copy2 = state_module.shutil.copy2

    def always_fail_wal(source, destination, *args, **kwargs):
        if str(source).endswith("-wal"):
            raise PermissionError(33, "sharing violation", str(source))
        return original_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(state_module.shutil, "copy2", always_fail_wal)

    with pytest.raises(DatabaseCorruptionError, match="snapshot"):
        StateStore(path, secret_key_provider)

    quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
    assert any(not item.name.endswith(("-wal", "-shm")) for item in quarantine_files)
    assert path.read_bytes() == b"not a sqlite database"
    assert wal_path.read_bytes() == b"wal evidence"


def test_open_failure_closes_connection_before_quarantine(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    initial = StateStore(path, secret_key_provider)
    initial.link_response("local-1", "upstream-1", route_id="chat")
    initial.close()

    seed = sqlite3.connect(path)
    try:
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("PRAGMA wal_autocheckpoint=0")
        seed.execute("CREATE TABLE open_failure_marker (value BLOB NOT NULL)")
        seed.execute("INSERT INTO open_failure_marker VALUES (?)", (b"fixture",))
        seed.commit()
        sidecars = {
            "-wal": path.with_name(f"{path.name}-wal"),
            "-shm": path.with_name(f"{path.name}-shm"),
        }
        assert all(sidecar.exists() for sidecar in sidecars.values())
        quarantine_evidence: dict[str, bytes] = {}
        original_quarantine = StateStore._quarantine_existing

        def fail_configuration(_store: StateStore) -> None:
            raise sqlite3.DatabaseError("injected open failure")

        def quarantine_after_close(store: StateStore, *args, **kwargs):
            assert store._connection is None
            evidence = {"": path.read_bytes()}
            evidence.update(
                {suffix: sidecar.read_bytes() for suffix, sidecar in sidecars.items()}
            )
            quarantine_evidence.update(evidence)
            return original_quarantine(store, *args, **kwargs)

        monkeypatch.setattr(StateStore, "_configure_connection", fail_configuration)
        monkeypatch.setattr(StateStore, "_quarantine_existing", quarantine_after_close)

        with pytest.raises(DatabaseCorruptionError, match="failed integrity"):
            StateStore(path, secret_key_provider)

        quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
        quarantine_main = next(
            item for item in quarantine_files if not item.name.endswith(("-wal", "-shm"))
        )
        assert quarantine_main.read_bytes() == quarantine_evidence[""]
        assert (
            (tmp_path / f"{quarantine_main.name}-wal").read_bytes()
            == quarantine_evidence["-wal"]
        )
        assert (
            (tmp_path / f"{quarantine_main.name}-shm").read_bytes()
            == quarantine_evidence["-shm"]
        )
    finally:
        seed.close()


def test_preflight_failure_preserves_writer_commit_and_quarantine_evidence(
    tmp_path, secret_key_provider, monkeypatch
) -> None:
    path = tmp_path / "state.sqlite3"
    initial = StateStore(path, secret_key_provider)
    initial.link_response(
        "local-1", "upstream-1", route_id="chat", codex_task_id="task-1"
    )
    initial.close()

    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE writer_marker (value BLOB NOT NULL)")
        writer.execute("INSERT INTO writer_marker VALUES (?)", (b"fixture",))
        writer.commit()
        quarantine_main_paths: list[Path] = []
        original_quarantine = StateStore._quarantine_existing

        def fail_after_writer_commit(connection: sqlite3.Connection) -> None:
            writer.execute(
                "INSERT INTO response_links VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("local-2", "upstream-2", "chat", 2, "task-1", None, 2),
            )
            writer.execute(
                "UPDATE event_counters SET value = 2 WHERE counter_name = 'state'"
            )
            writer.commit()
            raise DatabaseCorruptionError("injected preflight failure")

        def quarantine_after_failure(store: StateStore, *args, **kwargs):
            assert store._connection is None
            quarantine = original_quarantine(store, *args, **kwargs)
            quarantine_main_paths.append(quarantine)
            return quarantine

        monkeypatch.setattr(
            StateStore, "_validate_schema", staticmethod(fail_after_writer_commit)
        )
        monkeypatch.setattr(StateStore, "_quarantine_existing", quarantine_after_failure)

        with pytest.raises(
            DatabaseCorruptionError, match="snapshot/schema validation"
        ) as error:
            StateStore(path, secret_key_provider)
        assert "live evidence was preserved" in str(error.value)

        quarantine_main = quarantine_main_paths[0]
        live_connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            assert live_connection.execute(
                "SELECT upstream_response_id FROM response_links "
                "WHERE local_response_id = 'local-2'"
            ).fetchone() == ("upstream-2",)
        finally:
            live_connection.close()
        quarantine_connection = sqlite3.connect(
            f"{quarantine_main.as_uri()}?mode=ro", uri=True
        )
        try:
            assert quarantine_connection.execute(
                "SELECT upstream_response_id FROM response_links "
                "WHERE local_response_id = 'local-2'"
            ).fetchone() == ("upstream-2",)
        finally:
            quarantine_connection.close()
    finally:
        writer.close()

    monkeypatch.undo()
    reopened = StateStore(path, secret_key_provider)
    try:
        assert reopened.get_response_link("local-2").upstream_id == "upstream-2"
    finally:
        reopened.close()


def test_concurrent_stores_can_commit_without_losing_links(tmp_path, secret_key_provider) -> None:
    path = tmp_path / "state.sqlite3"

    def write_link(index: int) -> None:
        store = StateStore(path, secret_key_provider)
        try:
            store.link_response(
                f"local-{index}",
                f"upstream-{index}",
                route_id="chat",
                codex_task_id="task-concurrent",
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write_link, range(12)))

    store = StateStore(path, secret_key_provider)
    assert [store.get_response_link(f"local-{i}").upstream_id for i in range(12)] == [
        f"upstream-{i}" for i in range(12)
    ]
    store.close()


def test_receipts_and_cancel_handles_store_metadata_only(tmp_path, secret_key_provider) -> None:
    path = tmp_path / "state.sqlite3"
    digest = sha256(b"isolated config receipt fixture").hexdigest()
    store = StateStore(path, secret_key_provider)

    store.save_config_receipt("receipt-1", digest)
    store.save_cancel_handle("cancel-1", codex_task_id="task-1", route_id="chat", expires_at=100)

    assert store.get_config_receipt("receipt-1").config_sha256 == digest
    assert store.get_cancel_handle("cancel-1").codex_task_id == "task-1"
    assert store.get_cancel_handle("cancel-1").route_id == "chat"
    store.close()


@pytest.mark.parametrize(
    "invalid_digest",
    [
        "token-like-value",
        "a" * 63,
        "g" * 64,
        "a" * 65,
        Fernet.generate_key().decode("ascii"),
    ],
)
def test_config_receipt_rejects_non_sha256_values(
    tmp_path, secret_key_provider, invalid_digest
) -> None:
    store = StateStore(tmp_path / "state.sqlite3", secret_key_provider)

    with pytest.raises(ValueError):
        store.save_config_receipt("receipt-invalid", invalid_digest)

    store.close()
