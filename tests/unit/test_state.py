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

    assert store.schema_version == 2
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

    assert store.schema_version == 2
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


def test_corrupt_database_is_quarantined_and_not_recreated(tmp_path, secret_key_provider) -> None:
    path = tmp_path / "state.sqlite3"
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(DatabaseCorruptionError):
        StateStore(path, secret_key_provider)

    quarantine_files = list(tmp_path.glob("state.sqlite3.quarantine-*"))
    assert len(quarantine_files) == 1
    assert path.read_bytes() == b"not a sqlite database"


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
