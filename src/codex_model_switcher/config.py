"""Atomic, byte-preserving management of the project's config block."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .catalog import (
    CatalogValidationError,
    PickerVerificationReceipt,
    load_catalog,
    validate_picker_verification,
)

MANAGED_START = "# >>> codex-model-switcher managed start"
MANAGED_END = "# <<< codex-model-switcher managed end"
_CONFIG_LOCKS_GUARD = threading.Lock()
_CONFIG_LOCKS: dict[Path, object] = {}


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


class _WindowsOverlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_void_p),
        ("internal_high", ctypes.c_void_p),
        ("offset", ctypes.c_uint32),
        ("offset_high", ctypes.c_uint32),
        ("event", ctypes.c_void_p),
    ]


class _WindowsFileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32),
        ("creation_low", ctypes.c_uint32),
        ("creation_high", ctypes.c_uint32),
        ("access_low", ctypes.c_uint32),
        ("access_high", ctypes.c_uint32),
        ("write_low", ctypes.c_uint32),
        ("write_high", ctypes.c_uint32),
        ("volume_serial", ctypes.c_uint32),
        ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32),
        ("links", ctypes.c_uint32),
        ("index_high", ctypes.c_uint32),
        ("index_low", ctypes.c_uint32),
    ]


class _PathLease:
    """Hold the path lock used for the complete read-to-replace transaction.

    Windows obtains an open-requiring oplock as part of opening the target and
    keeps a byte-range lock for in-process readers.  The replacement path
    opens the new target handle inside the same TxF transaction before commit,
    with delete sharing denied.  That handle already protects the new target
    when the transaction commits; there is no old-handle/new-handle relock
    window.  Other platforms use a cooperative sidecar lock; the process lock
    remains the only guarantee when an external editor does not participate.
    """

    def __init__(self, path: Path, *, create: bool) -> None:
        self.path = Path(path).resolve()
        self.create = create
        self._handle: int | None = None
        self._kernel32: object | None = None
        self._overlapped: _WindowsOverlapped | None = None
        self._locked = False
        self._cooperative_stream = None
        self._fcntl = None

    def __enter__(self) -> _PathLease:
        if os.name == "nt":
            self._acquire_windows()
        else:
            self._acquire_cooperative()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if os.name == "nt":
            self._release_windows(exc_type is None)
        else:
            self._release_cooperative()
        return False

    def assert_target(self, path: Path) -> None:
        if Path(path).resolve() != self.path:
            raise ConfigError("atomic replacement lease belongs to a different path")

    def read_bytes(self) -> bytes:
        if os.name != "nt" or self._handle is None or self._kernel32 is None:
            return self.path.read_bytes()

        return self._read_windows_handle(self._handle, self._kernel32)

    def _read_windows_handle(self, handle: int, kernel32: object) -> bytes:
        get_file_size = kernel32.GetFileSizeEx
        get_file_size.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)]
        get_file_size.restype = ctypes.c_int
        file_size = ctypes.c_int64()
        if not get_file_size(handle, ctypes.byref(file_size)):
            error = ctypes.get_last_error()
            raise ConfigError(f"unable to read locked config size (error {error})")
        if file_size.value < 0 or file_size.value > 0xFFFFFFFF:
            raise ConfigError("locked config is too large to read safely")

        set_file_pointer = kernel32.SetFilePointerEx
        set_file_pointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int64,
            ctypes.POINTER(ctypes.c_int64),
            ctypes.c_uint32,
        ]
        set_file_pointer.restype = ctypes.c_int
        if not set_file_pointer(handle, 0, None, 0):  # FILE_BEGIN
            error = ctypes.get_last_error()
            raise ConfigError(f"unable to seek locked config (error {error})")

        read_file = kernel32.ReadFile
        read_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        read_file.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(file_size.value)
        bytes_read = ctypes.c_uint32()
        if file_size.value and not read_file(
            handle,
            buffer,
            file_size.value,
            ctypes.byref(bytes_read),
            None,
        ):
            error = ctypes.get_last_error()
            raise ConfigError(f"unable to read locked config (error {error})")
        return buffer.raw[: bytes_read.value]

    def _acquire_windows(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        disposition = 4 if self.create else 3  # OPEN_ALWAYS / OPEN_EXISTING
        handle = create_file(
            str(self.path),
            0x80000000,  # GENERIC_READ; the transaction needs no target write access
            # The oplock is the OS-level namespace guard.  The transaction
            # below uses the handle opened in its transaction view to take
            # over protection of the committed target before the old handle
            # is released.
            0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
            None,
            disposition,
            0x00000080 | 0x00040000,  # NORMAL | FILE_FLAG_OPEN_REQUIRING_OPLOCK
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in (None, invalid_handle):
            error = ctypes.get_last_error()
            raise ConfigError(f"unable to open config for Windows locking (error {error})")

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        lock_file = kernel32.LockFileEx
        lock_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(_WindowsOverlapped),
        ]
        lock_file.restype = ctypes.c_int
        overlapped = _WindowsOverlapped()
        if not lock_file(
            handle,
            0x00000002 | 0x00000001,  # LOCKFILE_EXCLUSIVE_LOCK | FAIL_IMMEDIATELY
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(overlapped),
        ):
            error = ctypes.get_last_error()
            close_handle(handle)
            raise ConfigError(f"unable to lock config on Windows (error {error})")

        self._kernel32 = kernel32
        self._handle = int(handle)
        self._overlapped = overlapped
        self._locked = True
        try:
            self._verify_windows_handle_path_identity(self._handle, create_file, close_handle)
        except Exception:
            self._release_windows(False)
            raise

    def _verify_windows_handle_path_identity(
        self,
        locked_handle: int,
        create_file,
        close_handle,
    ) -> None:
        current_handle = create_file(
            str(self.path),
            0x80000000,  # GENERIC_READ
            0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
            None,
            3,  # OPEN_EXISTING
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if current_handle in (None, invalid_handle):
            raise ConfigChangedError("config path changed while acquiring its Windows lock")
        try:
            get_info = self._kernel32.GetFileInformationByHandle
            get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WindowsFileInfo)]
            get_info.restype = ctypes.c_int
            locked_info = _WindowsFileInfo()
            current_info = _WindowsFileInfo()
            if not get_info(locked_handle, ctypes.byref(locked_info)):
                raise ConfigError("unable to inspect locked config identity")
            if not get_info(current_handle, ctypes.byref(current_info)):
                raise ConfigChangedError("unable to inspect current config identity")
            locked_identity = (
                locked_info.volume_serial,
                locked_info.index_high,
                locked_info.index_low,
            )
            current_identity = (
                current_info.volume_serial,
                current_info.index_high,
                current_info.index_low,
            )
            if locked_identity != current_identity:
                raise ConfigChangedError("config path changed while acquiring its Windows lock")
        finally:
            close_handle(current_handle)

    def replace_with_windows_transaction(
        self,
        temporary_path: Path,
        target_path: Path,
        expected: bytes,
    ) -> None:
        """Commit a protected Windows replacement without a path-lock gap.

        The target handle is opened with ``FILE_FLAG_OPEN_REQUIRING_OPLOCK``
        before this method is called.  The transaction moves the old target
        into a private shadow name, creates the new target link, deletes both
        private names, and opens the new target handle with delete sharing
        denied *before* ``CommitTransaction``.  The handle therefore already
        protects the committed target when the commit returns.
        """

        if os.name != "nt" or self._handle is None or self._kernel32 is None:
            raise ConfigError("Windows atomic replacement requires an active path lease")
        self.assert_target(target_path)
        kernel32 = self._kernel32
        old_handle = self._handle
        old_overlapped = self._overlapped
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        shadow_descriptor, shadow_name = tempfile.mkstemp(
            prefix=f".{target_path.name}.shadow.",
            suffix=".tmp",
            dir=target_path.parent,
        )
        os.close(shadow_descriptor)
        shadow_path = Path(shadow_name)
        shadow_path.unlink(missing_ok=True)

        source_handle = create_file(
            str(temporary_path),
            0x80000000,  # GENERIC_READ; Python fsync completed before this open
            0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
            None,
            3,  # OPEN_EXISTING
            0x00000080 | 0x80000000 | 0x00040000,  # NORMAL | WRITE_THROUGH | oplock
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if source_handle in (None, invalid_handle):
            error = ctypes.get_last_error()
            shadow_path.unlink(missing_ok=True)
            raise ConfigError(f"unable to open temporary config for locking (error {error})")

        source_handle_int = int(source_handle)
        transaction = None
        new_handle: int | None = None
        committed = False
        try:
            if self._read_windows_handle(source_handle_int, kernel32) != expected:
                raise ConfigChangedError(
                    "temporary config changed before atomic replacement; refusing overwrite"
                )

            try:
                transaction_api = ctypes.WinDLL("KtmW32", use_last_error=True)
                create_transaction = transaction_api.CreateTransaction
                create_transaction.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                    ctypes.c_uint32,
                    ctypes.c_uint32,
                    ctypes.c_uint32,
                    ctypes.c_wchar_p,
                ]
                create_transaction.restype = ctypes.c_void_p
                move_file_transacted = kernel32.MoveFileTransactedW
                move_file_transacted.argtypes = [
                    ctypes.c_wchar_p,
                    ctypes.c_wchar_p,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                ]
                move_file_transacted.restype = ctypes.c_int
                create_hard_link_transacted = kernel32.CreateHardLinkTransactedW
                create_hard_link_transacted.argtypes = [
                    ctypes.c_wchar_p,
                    ctypes.c_wchar_p,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                ]
                create_hard_link_transacted.restype = ctypes.c_int
                delete_file_transacted = kernel32.DeleteFileTransactedW
                delete_file_transacted.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
                delete_file_transacted.restype = ctypes.c_int
                create_file_transacted = kernel32.CreateFileTransactedW
                create_file_transacted.argtypes = [
                    ctypes.c_wchar_p,
                    ctypes.c_uint32,
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                ]
                create_file_transacted.restype = ctypes.c_void_p
                commit_transaction = transaction_api.CommitTransaction
                commit_transaction.argtypes = [ctypes.c_void_p]
                commit_transaction.restype = ctypes.c_int
                rollback_transaction = transaction_api.RollbackTransaction
                rollback_transaction.argtypes = [ctypes.c_void_p]
                rollback_transaction.restype = ctypes.c_int
            except (AttributeError, OSError) as error:
                raise ConfigError(
                    "Windows transacted replacement is unavailable; refusing non-atomic apply"
                ) from error

            transaction = create_transaction(None, None, 0, 0, 0, 0, None)
            if transaction in (None, invalid_handle):
                error = ctypes.get_last_error()
                raise ConfigError(f"unable to start Windows config transaction (error {error})")

            transaction_handle = int(transaction)
            if not move_file_transacted(
                str(target_path),
                str(shadow_path),
                None,
                None,
                0x00000001 | 0x00000008,  # REPLACE_EXISTING | WRITE_THROUGH
                transaction_handle,
            ):
                error = ctypes.get_last_error()
                if error in {5, 32, 33}:
                    raise ConfigChangedError(
                        "config became locked or changed during atomic replacement"
                    )
                raise ConfigError(f"unable to move config in Windows transaction (error {error})")

            if not create_hard_link_transacted(
                str(target_path),
                str(temporary_path),
                None,
                transaction_handle,
            ):
                error = ctypes.get_last_error()
                if error in {5, 32, 33}:
                    raise ConfigChangedError(
                        "config became locked or changed during atomic replacement"
                    )
                raise ConfigError(
                    f"unable to create config link in Windows transaction (error {error})"
                )

            if not delete_file_transacted(str(temporary_path), transaction_handle):
                error = ctypes.get_last_error()
                if error in {5, 32, 33}:
                    raise ConfigChangedError(
                        "temporary config changed during atomic replacement"
                    )
                raise ConfigError(
                    f"unable to remove temporary config in Windows transaction (error {error})"
                )
            if not delete_file_transacted(str(shadow_path), transaction_handle):
                error = ctypes.get_last_error()
                if error in {5, 32, 33}:
                    raise ConfigChangedError(
                        "config became locked during atomic replacement"
                    )
                raise ConfigError(
                    f"unable to remove old config in Windows transaction (error {error})"
                )

            new_handle_value = create_file_transacted(
                str(target_path),
                0x80000000,  # GENERIC_READ
                0x00000001 | 0x00000002,  # share read/write; deny DELETE/RENAME
                None,
                3,  # OPEN_EXISTING
                0x00000080,  # FILE_ATTRIBUTE_NORMAL
                None,
                transaction_handle,
                None,
                0,
                None,
            )
            if new_handle_value in (None, invalid_handle):
                error = ctypes.get_last_error()
                if error in {5, 32, 33}:
                    raise ConfigChangedError(
                        "config became locked during atomic replacement"
                    )
                raise ConfigError(
                    f"unable to pre-open new config handle in Windows transaction (error {error})"
                )
            new_handle = int(new_handle_value)

            if not _commit_windows_transaction(commit_transaction, transaction_handle):
                error = ctypes.get_last_error()
                raise ConfigError(f"unable to commit Windows config transaction (error {error})")
            committed = True

            # The share-denying handle was opened before commit.  Install it
            # on the lease before releasing any pre-commit handles so every
            # post-commit failure still leaves the target namespace guarded.
            self._handle = new_handle
            self._overlapped = None
            self._locked = False
            new_handle = None

            source_close_ok = close_handle(source_handle_int)
            source_close_code = ctypes.get_last_error() if not source_close_ok else None
            source_handle = None
            old_error = self._release_windows_handle(kernel32, old_handle, old_overlapped)
            if source_close_code is not None:
                raise ConfigError(
                    f"unable to release temporary Windows config handle (error {source_close_code})"
                )
            if old_error is not None:
                raise ConfigError(
                    f"unable to release previous Windows config lock (error {old_error})"
                )
        finally:
            if transaction is not None:
                if not committed:
                    try:
                        rollback_transaction(int(transaction))
                    except OSError:
                        pass
                close_handle(transaction)
            if new_handle is not None:
                close_handle(new_handle)
            if source_handle is not None:
                close_handle(source_handle)
            if not committed:
                shadow_path.unlink(missing_ok=True)

    def _release_windows(self, raise_on_error: bool) -> None:
        if self._handle is None or self._kernel32 is None:
            return
        kernel32 = self._kernel32
        unlock_error = self._release_windows_handle(
            kernel32,
            self._handle,
            self._overlapped,
        )
        self._handle = None
        self._locked = False
        if unlock_error is not None and raise_on_error:
            raise ConfigError(f"unable to release Windows config lock (error {unlock_error})")

    def _release_windows_handle(
        self,
        kernel32: object,
        handle: int,
        overlapped: _WindowsOverlapped | None,
    ) -> int | None:
        unlock_error: int | None = None
        if overlapped is not None:
            unlock_file = kernel32.UnlockFileEx
            unlock_file.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(_WindowsOverlapped),
            ]
            unlock_file.restype = ctypes.c_int
            if not unlock_file(
                handle,
                0,
                0xFFFFFFFF,
                0xFFFFFFFF,
                ctypes.byref(overlapped),
            ):
                unlock_error = ctypes.get_last_error()
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        if not close_handle(handle) and unlock_error is None:
            unlock_error = ctypes.get_last_error()
        return unlock_error

    def _acquire_cooperative(self) -> None:
        try:
            import fcntl
        except ImportError as error:
            raise ConfigError(
                "cooperative config locking is unavailable; refusing an unlocked write"
            ) from error
        lock_path = self.path.with_name(f".{self.path.name}.codex-model-switcher.lock")
        stream = None
        try:
            stream = lock_path.open("a+b")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            if stream is not None:
                stream.close()
            raise ConfigError("unable to obtain cooperative config lock") from error
        self._cooperative_stream = stream
        self._fcntl = fcntl

    def _release_cooperative(self) -> None:
        if self._cooperative_stream is None:
            return
        if self._fcntl is not None:
            self._fcntl.flock(self._cooperative_stream.fileno(), self._fcntl.LOCK_UN)
        self._cooperative_stream.close()
        self._cooperative_stream = None


def _exclusive_path_lock(path: Path, *, create: bool) -> _PathLease:
    return _PathLease(path, create=create)


def render_managed_config(
    catalog_path: Path,
    *,
    verification: PickerVerificationReceipt | None = None,
) -> str:
    """Render only an externally-attested candidate; never endpoint or credentials."""

    try:
        catalog = load_catalog(Path(catalog_path))
    except CatalogValidationError as error:
        raise ConfigError(str(error)) from error
    try:
        validate_picker_verification(catalog, verification)
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


def apply_managed_config(
    config_path: Path,
    catalog_path: Path,
    *,
    verification: PickerVerificationReceipt | None = None,
) -> ConfigReceipt:
    catalog_path = Path(catalog_path)
    managed_block = render_managed_config(catalog_path, verification=verification)
    config_path = Path(config_path).resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with _config_lock(config_path):
        with _exclusive_path_lock(config_path, create=True) as lease:
            return _apply_managed_config_locked(
                config_path,
                managed_block=managed_block,
                lease=lease,
            )


def _apply_managed_config_locked(
    config_path: Path,
    *,
    managed_block: str,
    lease: _PathLease,
) -> ConfigReceipt:
    try:
        original = lease.read_bytes()
        original_text = original.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError("config must be readable UTF-8 bytes") from error

    rendered = _replace_or_append_managed_block(original_text, managed_block)
    written = rendered.encode("utf-8")
    _assert_current_bytes(config_path, original, lease=lease)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = config_path.with_name(f"{config_path.name}.bak.{timestamp}")
    backup_descriptor = os.open(
        backup_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
    )
    os.close(backup_descriptor)
    try:
        with _exclusive_path_lock(backup_path, create=False) as backup_lease:
            _atomic_write(backup_path, original, expected=b"", lease=backup_lease)
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    try:
        _atomic_write(config_path, written, expected=original, lease=lease)
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
    with _config_lock(config_path):
        with _exclusive_path_lock(config_path, create=False) as lease:
            try:
                current = lease.read_bytes()
                backup = receipt.backup_path.read_bytes()
            except OSError as error:
                raise ConfigError("config or its backup is unavailable") from error
            if _sha256(current) != receipt.written_hash:
                raise ConfigChangedError(
                    "config changed after this project wrote it; refusing restore"
                )
            if _sha256(backup) != receipt.original_hash:
                raise ConfigError("backup hash does not match the apply receipt")
            _atomic_write(config_path, backup, expected=current, lease=lease)


def _replace_or_append_managed_block(original: str, block: str) -> str:
    lines = original.splitlines(keepends=True)
    start_lines: list[int] = []
    end_lines: list[int] = []
    multiline_quote: str | None = None
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        for marker, matches in ((MANAGED_START, start_lines), (MANAGED_END, end_lines)):
            if marker in content:
                if multiline_quote is not None:
                    raise ConfigError("managed config marker is inside a TOML multiline string")
                if content != marker:
                    raise ConfigError("managed config marker must occupy a complete line")
                matches.append(index)
        multiline_quote = _advance_toml_multiline_state(line, multiline_quote)
    if len(start_lines) > 1 or len(end_lines) > 1:
        raise ConfigError("managed config must contain exactly one marker pair")
    if len(start_lines) != len(end_lines):
        raise ConfigError("managed config marker pair is incomplete")
    if start_lines:
        start_line = start_lines[0]
        end_line = end_lines[0]
        if end_line < start_line:
            raise ConfigError("managed config marker pair is out of order")
        existing_end = lines[end_line]
        if existing_end.endswith("\r\n"):
            newline = "\r\n"
        elif existing_end.endswith("\n"):
            newline = "\n"
        else:
            newline = ""
        return "".join(lines[:start_line] + [block + newline] + lines[end_line + 1 :])

    separator = "" if not original or original.endswith(("\n", "\r")) else "\n"
    return original + separator + block + "\n"


def _advance_toml_multiline_state(line: str, quote: str | None) -> str | None:
    index = 0
    while index < len(line):
        if quote is not None:
            closing = _find_toml_triple_quote(line, quote, index)
            if closing == -1:
                return quote
            index = closing + 3
            quote = None
            continue
        if line[index] == "#":
            return None
        if line.startswith('"""', index):
            quote = '"""'
            index += 3
            continue
        if line.startswith("'''", index):
            quote = "'''"
            index += 3
            continue
        if line[index] in ('"', "'"):
            index = _skip_toml_single_line_string(line, index)
            continue
        index += 1
    return quote


def _find_toml_triple_quote(line: str, quote: str, start: int) -> int:
    index = start
    while True:
        index = line.find(quote, index)
        if index == -1:
            return -1
        if quote == '"""':
            backslashes = 0
            previous = index - 1
            while previous >= 0 and line[previous] == "\\":
                backslashes += 1
                previous -= 1
            if backslashes % 2:
                index += 1
                continue
        return index


def _skip_toml_single_line_string(line: str, start: int) -> int:
    quote = line[start]
    index = start + 1
    while index < len(line):
        if quote == '"' and line[index] == "\\":
            index += 2
            continue
        if line[index] == quote:
            return index + 1
        index += 1
    return len(line)


def _config_lock(path: Path) -> object:
    with _CONFIG_LOCKS_GUARD:
        lock = _CONFIG_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _CONFIG_LOCKS[path] = lock
        return lock


def _assert_current_bytes(
    path: Path,
    expected: bytes,
    *,
    lease: _PathLease | None = None,
) -> None:
    try:
        current = lease.read_bytes() if lease is not None else path.read_bytes()
    except OSError as error:
        raise ConfigError("config became unavailable during apply") from error
    if current != expected:
        raise ConfigChangedError("config changed during apply; refusing overwrite")


def _atomic_write(
    path: Path,
    data: bytes,
    *,
    expected: bytes | None = None,
    lease: _PathLease | None = None,
) -> None:
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
        if expected is not None:
            _assert_current_bytes(path, expected, lease=lease)
        _replace_temp_file(temporary_path, path, lease=lease, expected=data)
    finally:
        temporary_path.unlink(missing_ok=True)


def _commit_windows_transaction(commit_transaction, transaction_handle) -> bool:
    """Keep the transaction linearization point independently testable."""

    return bool(commit_transaction(transaction_handle))


def _replace_temp_file(
    temporary_path: Path,
    target_path: Path,
    *,
    lease: _PathLease | None = None,
    expected: bytes | None = None,
) -> None:
    if os.name != "nt":
        os.replace(temporary_path, target_path)
        return

    if lease is None:
        raise ConfigError("Windows atomic replacement requires an active path lease")

    if expected is None:
        raise ConfigError("atomic replacement needs expected bytes for a Windows lock")
    lease.replace_with_windows_transaction(temporary_path, target_path, expected)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
