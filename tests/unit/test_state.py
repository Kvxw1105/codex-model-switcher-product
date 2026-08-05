import sqlite3
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest
from cryptography.fernet import Fernet

from codex_model_switcher.state import (
    DatabaseCorruptionError,
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


def test_new_database_uses_wal_and_current_schema(tmp_path, secret_key_provider) -> None:
    path = tmp_path / "state.sqlite3"

    store = StateStore(path, secret_key_provider)

    assert store.schema_version == 3
    assert store.journal_mode == "wal"
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
        INSERT INTO response_links VALUES ('old-local', 'old-upstream', 'legacy-route', 1);
        """
    )
    connection.commit()
    connection.close()

    store = StateStore(path, secret_key_provider)

    assert store.schema_version == 3
    assert store.get_response_link("old-local").upstream_id == "old-upstream"
    store.close()


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
