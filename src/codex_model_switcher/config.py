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
from urllib.parse import urlparse

from .catalog import (
    CatalogValidationError,
    PickerVerificationReceipt,
    load_catalog,
    validate_picker_verification,
    write_native_catalog,
)

MANAGED_START = "# >>> codex-model-switcher managed start"
MANAGED_END = "# <<< codex-model-switcher managed end"
_CONFIG_LOCKS_GUARD = threading.Lock()
_CONFIG_LOCKS: dict[Path, object] = {}
DEFAULT_ROUTER_BASE_URL = "http://127.0.0.1:4317/v1"


class ConfigError(RuntimeError):
    """Raised when a managed config cannot be safely applied or restored."""


class ConfigChangedError(ConfigError):
    """Raised when the target changed after this project wrote it."""


@dataclass(frozen=True)
class ConfigOperationFailure:
    """Safe metadata for one failed transaction operation."""

    operation: str
    path: Path | None
    error_code: int | str | None


@dataclass(frozen=True)
class _WindowsHandleReleaseResult:
    """Result that distinguishes a closed handle from an uncertain close."""

    close_succeeded: bool
    unlock_error: int | str | None = None
    close_error: int | str | None = None


class _WindowsHandleOwner:
    """Private retry capability handed to a structured transaction error."""

    def __init__(self, operation: str, path: Path, handle: int, close_callback) -> None:
        self.operation = operation
        self.path = Path(path)
        self.handle = int(handle)
        self._close_callback = close_callback
        self._released = False
        self._last_error_code: int | str | None = None
        self._lock = threading.Lock()

    @property
    def released(self) -> bool:
        return self._released

    def retry(self) -> bool:
        with self._lock:
            if self._released:
                return True
            try:
                close_succeeded = bool(self._close_callback(self.handle))
            except Exception as error:
                self._last_error_code = _safe_error_code(error)
                return False
            self._last_error_code = (
                None if close_succeeded else ctypes.get_last_error()
            )
            if close_succeeded:
                self._released = True
            return close_succeeded

    def mark_released(self) -> None:
        with self._lock:
            self._released = True

    def set_retry_callback(self, close_callback) -> None:
        with self._lock:
            self._close_callback = close_callback

    def failure(self) -> ConfigOperationFailure:
        return ConfigOperationFailure(
            self.operation,
            self.path,
            self._last_error_code,
        )


class _HandleOwnershipErrorMixin:
    def _set_unreleased_handle_owners(
        self,
        unreleased_handles: tuple[int, ...],
        owners: tuple[_WindowsHandleOwner, ...],
    ) -> None:
        self._unreleased_handle_owners = tuple(
            owner for owner in owners if not owner.released
        )
        self.unreleased_handles = tuple(
            dict.fromkeys(
                [
                    *unreleased_handles,
                    *(owner.handle for owner in self._unreleased_handle_owners),
                ]
            )
        )

    @property
    def unreleased_handle_owners(self) -> tuple[_WindowsHandleOwner, ...]:
        return self._unreleased_handle_owners

    def retry_unreleased_handles(self) -> bool:
        owners = self._unreleased_handle_owners
        remaining = tuple(owner for owner in owners if not owner.retry())
        self._unreleased_handle_owners = remaining
        owner_handles = {owner.handle for owner in owners}
        remaining_handles = {owner.handle for owner in remaining}
        self.unreleased_handles = tuple(
            handle
            for handle in self.unreleased_handles
            if handle not in owner_handles or handle in remaining_handles
        )
        return not self._unreleased_handle_owners and not self.unreleased_handles


class ConfigPostCommitError(_HandleOwnershipErrorMixin, ConfigError):
    """Raised when replacement committed but cleanup made completion uncertain."""

    def __init__(
        self,
        path: Path,
        detail: str,
        *,
        backup_path: Path | None = None,
        temporary_path: Path | None = None,
        cleanup_error: BaseException | None = None,
        original_error: BaseException | None = None,
        failures: tuple[ConfigOperationFailure, ...] = (),
        unreleased_handles: tuple[int, ...] = (),
        unreleased_handle_owners: tuple[_WindowsHandleOwner, ...] = (),
    ) -> None:
        self.path = Path(path)
        self.committed = True
        self.backup_path = Path(backup_path) if backup_path is not None else None
        self.temporary_path = (
            Path(temporary_path) if temporary_path is not None else None
        )
        self.cleanup_error = cleanup_error
        self.original_error = original_error
        self.failures = tuple(failures)
        self._set_unreleased_handle_owners(
            unreleased_handles,
            unreleased_handle_owners,
        )
        super().__init__(
            f"atomic replacement for {self.path} committed, but cleanup failed: {detail}"
        )


class ConfigTransactionStateError(_HandleOwnershipErrorMixin, ConfigError):
    """Raised when pre-commit rollback or handle cleanup leaves state uncertain."""

    def __init__(
        self,
        path: Path,
        detail: str,
        *,
        backup_path: Path | None = None,
        temporary_path: Path | None = None,
        original_error: BaseException | None = None,
        cleanup_error: BaseException | None = None,
        failures: tuple[ConfigOperationFailure, ...] = (),
        unreleased_handles: tuple[int, ...] = (),
        unreleased_handle_owners: tuple[_WindowsHandleOwner, ...] = (),
        state_uncertain: bool = True,
    ) -> None:
        self.path = Path(path)
        self.committed = False
        self.state_uncertain = state_uncertain
        self.backup_path = Path(backup_path) if backup_path is not None else None
        self.temporary_path = (
            Path(temporary_path) if temporary_path is not None else None
        )
        self.original_error = original_error
        self.cleanup_error = cleanup_error
        self.failures = tuple(failures)
        self._set_unreleased_handle_owners(
            unreleased_handles,
            unreleased_handle_owners,
        )
        state = "uncertain" if state_uncertain else "not committed"
        super().__init__(
            f"Windows transaction is {state}; cleanup failed and backup is retained: {detail}"
        )


@dataclass(frozen=True)
class ConfigReceipt:
    config_path: Path
    backup_path: Path
    original_hash: str
    written_hash: str
    timestamp: str


def _safe_error_code(error: BaseException | int | str | None) -> int | str | None:
    if error is None:
        return None
    if isinstance(error, (int, str)):
        return error
    for attribute in ("winerror", "errno"):
        value = getattr(error, attribute, None)
        if isinstance(value, (int, str)):
            return value
    return type(error).__name__


def _normalize_windows_release(
    result: _WindowsHandleReleaseResult | int | str | None,
) -> _WindowsHandleReleaseResult:
    if isinstance(result, _WindowsHandleReleaseResult):
        return result
    if result is None:
        return _WindowsHandleReleaseResult(close_succeeded=True)
    return _WindowsHandleReleaseResult(
        close_succeeded=False,
        close_error=_safe_error_code(result),
    )


def _windows_release_failures(
    result: _WindowsHandleReleaseResult,
    path: Path,
    scope: str,
) -> list[ConfigOperationFailure]:
    failures: list[ConfigOperationFailure] = []
    if result.unlock_error is not None:
        failures.append(
            ConfigOperationFailure(
                f"{scope}:UnlockFileEx",
                Path(path),
                result.unlock_error,
            )
        )
    if not result.close_succeeded:
        failures.append(
            ConfigOperationFailure(
                f"{scope}:CloseHandle",
                Path(path),
                result.close_error,
            )
        )
    return failures


def _aggregate_config_errors(
    errors: list[BaseException],
    path: Path,
    *,
    committed: bool,
    backup_path: Path | None,
) -> ConfigError:
    failures: list[ConfigOperationFailure] = []
    unreleased_handles: list[int] = []
    unreleased_handle_owners: list[_WindowsHandleOwner] = []
    original_errors: list[BaseException] = []
    retained_backup = backup_path
    temporary_path: Path | None = None
    cleanup_error: BaseException | None = None
    for error in errors:
        failures.extend(getattr(error, "failures", ()))
        for handle in getattr(error, "unreleased_handles", ()):
            if handle not in unreleased_handles:
                unreleased_handles.append(handle)
        for owner in getattr(error, "unreleased_handle_owners", ()):
            if owner not in unreleased_handle_owners:
                unreleased_handle_owners.append(owner)
        if retained_backup is None:
            retained_backup = getattr(error, "backup_path", None)
        if temporary_path is None:
            temporary_path = getattr(error, "temporary_path", None)
        if cleanup_error is None:
            cleanup_error = getattr(error, "cleanup_error", None)
        original_error = getattr(error, "original_error", None)
        if original_error is not None:
            original_errors.append(original_error)
        elif not getattr(error, "failures", ()):
            original_errors.append(error)
    if not failures:
        failures.append(
            ConfigOperationFailure(
                "transaction cleanup",
                Path(path),
                _safe_error_code(errors[-1] if errors else None),
            )
        )
    original_error = original_errors[0] if original_errors else None
    if committed:
        return ConfigPostCommitError(
            path,
            "multiple post-commit failures",
            backup_path=retained_backup,
            temporary_path=temporary_path,
            cleanup_error=cleanup_error,
            original_error=original_error,
            failures=tuple(failures),
            unreleased_handles=tuple(unreleased_handles),
            unreleased_handle_owners=tuple(unreleased_handle_owners),
        )
    return ConfigTransactionStateError(
        path,
        "multiple transaction cleanup failures",
        backup_path=retained_backup,
        temporary_path=temporary_path,
        cleanup_error=cleanup_error,
        original_error=original_error,
        failures=tuple(failures),
        unreleased_handles=tuple(unreleased_handles),
        unreleased_handle_owners=tuple(unreleased_handle_owners),
    )


def _remove_unreleased_handle(error: BaseException, handle: int) -> None:
    handles = getattr(error, "unreleased_handles", None)
    if handles is not None:
        error.unreleased_handles = tuple(value for value in handles if value != handle)
    owners = getattr(error, "_unreleased_handle_owners", None)
    if owners is not None:
        for owner in owners:
            if owner.handle == handle:
                owner.mark_released()
        error._unreleased_handle_owners = tuple(
            owner for owner in owners if owner.handle != handle
        )


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
        self._created_new_path = False
        self._replacement_committed = False
        self._backup_path: Path | None = None
        self._pending_backup_cleanup: Path | None = None
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
            cleanup_errors: list[BaseException] = []
            if self._created_new_path and not self._replacement_committed:
                try:
                    self._rollback_new_path()
                except ConfigError as error:
                    cleanup_errors.append(error)
            release_handle = self._handle
            try:
                self._release_windows()
            except Exception as release_error:
                if (
                    release_handle is not None
                    and self._handle is None
                    and isinstance(exc_value, BaseException)
                ):
                    _remove_unreleased_handle(exc_value, release_handle)
                cleanup_errors.append(release_error)
            else:
                if (
                    release_handle is not None
                    and self._handle is None
                    and isinstance(exc_value, BaseException)
                ):
                    _remove_unreleased_handle(exc_value, release_handle)
            if cleanup_errors:
                errors = list(cleanup_errors)
                if isinstance(exc_value, BaseException):
                    errors.insert(0, exc_value)
                committed = self._replacement_committed or any(
                    isinstance(error, ConfigPostCommitError) for error in errors
                )
                aggregate = _aggregate_config_errors(
                    errors,
                    self.path,
                    committed=committed,
                    backup_path=self._backup_path or self._pending_backup_cleanup,
                )
                original_error = getattr(aggregate, "original_error", None)
                if original_error is not None:
                    raise aggregate from original_error
                raise aggregate
        else:
            self._release_cooperative()
        if (
            exc_value is not None
            and self._pending_backup_cleanup is not None
            and not self._replacement_committed
            and not isinstance(exc_value, (ConfigPostCommitError, ConfigTransactionStateError))
        ):
            pending_backup = self._pending_backup_cleanup
            try:
                pending_backup.unlink(missing_ok=True)
                self._pending_backup_cleanup = None
            except OSError as error:
                state_error = ConfigTransactionStateError(
                    self.path,
                    "unable to remove the pre-commit backup after a safe rollback",
                    backup_path=pending_backup,
                    state_uncertain=False,
                    original_error=exc_value,
                    cleanup_error=error,
                    failures=(
                        ConfigOperationFailure(
                            "unlink(backup)",
                            pending_backup,
                            _safe_error_code(error),
                        ),
                    ),
                )
                if exc_value is not None:
                    raise state_error from exc_value
                raise state_error from error
        return False

    def _rollback_new_path(self) -> None:
        if not self._created_new_path or self._replacement_committed:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError as error:
            raise ConfigTransactionStateError(
                self.path,
                "unable to roll back the newly created config file",
                backup_path=self._backup_path or self._pending_backup_cleanup,
                cleanup_error=error,
                failures=(
                    ConfigOperationFailure(
                        "unlink(config)",
                        self.path,
                        _safe_error_code(error),
                    ),
                ),
            ) from error
        self._created_new_path = False

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
        created_new_path = self.create and ctypes.get_last_error() == 0
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
        lock_failure_cause: BaseException | None = None
        try:
            lock_succeeded = _lock_windows_file(lock_file, handle, overlapped)
            lock_error_code = None if lock_succeeded else ctypes.get_last_error()
        except Exception as error:
            lock_succeeded = False
            lock_error_code = _safe_error_code(error)
            lock_failure_cause = error
        if not lock_succeeded:
            lock_failure = ConfigError("unable to lock config on Windows")
            failures = [
                ConfigOperationFailure(
                    "LockFileEx",
                    self.path,
                    lock_error_code,
                )
            ]
            unreleased_handles: list[int] = []
            unreleased_handle_owners: list[_WindowsHandleOwner] = []
            handle_owner = _WindowsHandleOwner(
                "CloseHandle(lease)",
                self.path,
                int(handle),
                lambda value: _close_windows_handle(close_handle, value),
            )
            if not handle_owner.retry():
                failures.append(handle_owner.failure())
                unreleased_handles.append(handle_owner.handle)
                unreleased_handle_owners.append(handle_owner)
            if created_new_path:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError as error:
                    failures.append(
                        ConfigOperationFailure(
                            "unlink(config)",
                            self.path,
                            _safe_error_code(error),
                        )
                    )
            if len(failures) > 1:
                state_error = ConfigTransactionStateError(
                    self.path,
                    "Windows lock acquisition cleanup is uncertain",
                    backup_path=self._backup_path or self._pending_backup_cleanup,
                    original_error=lock_failure,
                    failures=tuple(failures),
                    unreleased_handles=tuple(unreleased_handles),
                    unreleased_handle_owners=tuple(unreleased_handle_owners),
                )
                if lock_failure_cause is not None:
                    raise state_error from lock_failure_cause
                raise state_error from lock_failure
            if lock_failure_cause is not None:
                raise lock_failure from lock_failure_cause
            raise lock_failure

        self._kernel32 = kernel32
        self._handle = int(handle)
        self._overlapped = overlapped
        self._locked = True
        self._created_new_path = created_new_path
        try:
            self._verify_windows_handle_path_identity(self._handle, create_file, close_handle)
        except Exception as error:
            cleanup_errors: list[BaseException] = []
            if self._created_new_path:
                try:
                    self._rollback_new_path()
                except ConfigError as cleanup_exception:
                    cleanup_errors.append(cleanup_exception)
            try:
                self._release_windows()
            except Exception as cleanup_exception:
                cleanup_errors.append(cleanup_exception)
            if cleanup_errors:
                aggregate = _aggregate_config_errors(
                    [error, *cleanup_errors],
                    self.path,
                    committed=self._replacement_committed,
                    backup_path=self._backup_path or self._pending_backup_cleanup,
                )
                original_error = getattr(aggregate, "original_error", None)
                if original_error is not None:
                    raise aggregate from original_error
                raise aggregate from error
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
        body_error: BaseException | None = None
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
        except BaseException as error:
            body_error = error
            raise
        finally:
            identity_owner = _WindowsHandleOwner(
                "CloseHandle(identity)",
                self.path,
                int(current_handle),
                lambda value: _close_windows_handle(close_handle, value),
            )
            close_succeeded = identity_owner.retry()
            if not close_succeeded:
                original_error = getattr(body_error, "original_error", None)
                if original_error is None:
                    original_error = body_error
                state_error = ConfigTransactionStateError(
                    self.path,
                    "unable to close the Windows identity handle",
                    original_error=original_error,
                    failures=tuple(
                        [*getattr(body_error, "failures", ()), identity_owner.failure()]
                    ),
                    unreleased_handles=tuple(
                        [
                            *getattr(body_error, "unreleased_handles", ()),
                            identity_owner.handle,
                        ]
                    ),
                    unreleased_handle_owners=tuple(
                        [
                            *getattr(body_error, "unreleased_handle_owners", ()),
                            identity_owner,
                        ]
                    ),
                )
                if original_error is not None:
                    raise state_error from original_error
                raise state_error

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
        source_handle = source_handle_int
        transaction = None
        new_handle: int | None = None
        committed = False
        body_error: Exception | None = None
        body_failures: list[ConfigOperationFailure] = []
        unreleased_handles: list[int] = []
        unreleased_handle_owners: list[_WindowsHandleOwner] = []
        transaction_owner: _WindowsHandleOwner | None = None
        replacement_owner: _WindowsHandleOwner | None = None
        source_owner = _WindowsHandleOwner(
            "CloseHandle(source)",
            temporary_path,
            source_handle_int,
            lambda value: _close_source_handle(close_handle, value),
        )

        def remember_owner(owner: _WindowsHandleOwner) -> None:
            if owner not in unreleased_handle_owners:
                unreleased_handle_owners.append(owner)
            if owner.handle not in unreleased_handles:
                unreleased_handles.append(owner.handle)

        def forget_owner(owner: _WindowsHandleOwner) -> None:
            if owner in unreleased_handle_owners:
                unreleased_handle_owners.remove(owner)
            if not any(
                remaining.handle == owner.handle
                for remaining in unreleased_handle_owners
            ):
                while owner.handle in unreleased_handles:
                    unreleased_handles.remove(owner.handle)
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
                create_file_transacted = _configure_create_file_transacted(
                    kernel32.CreateFileTransactedW
                )
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
            transaction_owner = _WindowsHandleOwner(
                "CloseHandle(transaction)",
                target_path,
                transaction_handle,
                lambda value: _close_windows_transaction(close_handle, value),
            )
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

            new_handle_value = _call_create_file_transacted(
                create_file_transacted,
                target_path,
                transaction_handle,
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
            replacement_owner = _WindowsHandleOwner(
                "CloseHandle(replacement)",
                target_path,
                new_handle,
                lambda value: _close_precommit_handle(close_handle, value),
            )

            def commit_and_mark(handle: int) -> bool:
                nonlocal committed
                succeeded = bool(commit_transaction(handle))
                if succeeded:
                    committed = True
                    self._replacement_committed = True
                return succeeded

            commit_succeeded = _commit_windows_transaction(
                commit_and_mark,
                transaction_handle,
            )

            if not commit_succeeded:
                error = ctypes.get_last_error()
                raise ConfigError(f"unable to commit Windows config transaction (error {error})")
            if not committed:
                raise ConfigError("Windows transaction commit was not recorded")

            # The share-denying handle was opened before commit.  Release the
            # old lease while it is still owned by self, then install the new
            # handle only after CloseHandle has succeeded.
            source_close_ok = source_owner.retry()
            if source_close_ok:
                source_handle = None
            else:
                body_failures.append(source_owner.failure())
                remember_owner(source_owner)

            try:
                old_release = _normalize_windows_release(
                    self._release_windows_handle(
                        kernel32,
                        old_handle,
                        old_overlapped,
                    )
                )
            except Exception as error:
                body_failures.append(
                    ConfigOperationFailure(
                        "lease release",
                        target_path,
                        _safe_error_code(error),
                    )
                )
                if old_handle not in unreleased_handles:
                    unreleased_handles.append(old_handle)
            else:
                old_failures = _windows_release_failures(
                    old_release,
                    target_path,
                    "lease",
                )
                body_failures.extend(old_failures)
                if old_release.close_succeeded:
                    self._handle = new_handle
                    self._overlapped = None
                    self._locked = False
                    new_handle = None
                    while old_handle in unreleased_handles:
                        unreleased_handles.remove(old_handle)
                elif old_handle not in unreleased_handles:
                    unreleased_handles.append(old_handle)

            if body_failures:
                raise ConfigError("post-commit handle cleanup failed")
        except Exception as error:
            body_error = error
            raise
        finally:
            cleanup_failures: list[ConfigOperationFailure] = []
            if transaction is not None:
                transaction_value = int(transaction)
                if not committed:
                    try:
                        rollback_ok = _rollback_windows_transaction(
                            rollback_transaction,
                            transaction_value,
                        )
                        if not rollback_ok:
                            cleanup_failures.append(
                                ConfigOperationFailure(
                                    "RollbackTransaction",
                                    target_path,
                                    ctypes.get_last_error(),
                                )
                            )
                    except Exception as error:
                        cleanup_failures.append(
                            ConfigOperationFailure(
                                "RollbackTransaction",
                                target_path,
                                _safe_error_code(error),
                            )
                        )
                try:
                    transaction_close_ok = (
                        transaction_owner.retry()
                        if transaction_owner is not None
                        else _close_windows_transaction(close_handle, transaction_value)
                    )
                    if transaction_close_ok:
                        transaction = None
                        if transaction_owner is not None:
                            forget_owner(transaction_owner)
                        else:
                            while transaction_value in unreleased_handles:
                                unreleased_handles.remove(transaction_value)
                    else:
                        if transaction_owner is not None:
                            cleanup_failures.append(transaction_owner.failure())
                            remember_owner(transaction_owner)
                        else:
                            cleanup_failures.append(
                                ConfigOperationFailure(
                                    "CloseHandle(transaction)",
                                    target_path,
                                    ctypes.get_last_error(),
                                )
                            )
                            if transaction_value not in unreleased_handles:
                                unreleased_handles.append(transaction_value)
                except Exception as error:
                    cleanup_failures.append(
                        ConfigOperationFailure(
                            "CloseHandle(transaction)",
                            target_path,
                            _safe_error_code(error),
                        )
                    )
                    if transaction_value not in unreleased_handles:
                        unreleased_handles.append(transaction_value)
            if new_handle is not None:
                replacement_handle = new_handle
                try:
                    replacement_close_ok = (
                        replacement_owner.retry()
                        if replacement_owner is not None
                        else _close_precommit_handle(close_handle, replacement_handle)
                    )
                    if replacement_close_ok:
                        new_handle = None
                        if replacement_owner is not None:
                            forget_owner(replacement_owner)
                        else:
                            while replacement_handle in unreleased_handles:
                                unreleased_handles.remove(replacement_handle)
                    else:
                        if replacement_owner is not None:
                            cleanup_failures.append(replacement_owner.failure())
                            remember_owner(replacement_owner)
                        else:
                            cleanup_failures.append(
                                ConfigOperationFailure(
                                    "CloseHandle(replacement)",
                                    target_path,
                                    ctypes.get_last_error(),
                                )
                            )
                            if replacement_handle not in unreleased_handles:
                                unreleased_handles.append(replacement_handle)
                except Exception as error:
                    cleanup_failures.append(
                        ConfigOperationFailure(
                            "CloseHandle(replacement)",
                            target_path,
                            _safe_error_code(error),
                        )
                    )
                    if replacement_handle not in unreleased_handles:
                        unreleased_handles.append(replacement_handle)
            if source_handle is not None:
                source_handle_value = int(source_handle)
                try:
                    source_owner.set_retry_callback(
                        lambda value: _close_precommit_handle(close_handle, value)
                    )
                    source_cleanup_ok = source_owner.retry()
                    if source_cleanup_ok:
                        source_handle = None
                        forget_owner(source_owner)
                    else:
                        cleanup_failures.append(source_owner.failure())
                        remember_owner(source_owner)
                except Exception as error:
                    cleanup_failures.append(
                        ConfigOperationFailure(
                            "CloseHandle(source)",
                            temporary_path,
                            _safe_error_code(error),
                        )
                    )
                    if source_handle_value not in unreleased_handles:
                        unreleased_handles.append(source_handle_value)
            if not committed:
                try:
                    shadow_path.unlink(missing_ok=True)
                except Exception as error:
                    cleanup_failures.append(
                        ConfigOperationFailure(
                            "unlink(shadow)",
                            shadow_path,
                            _safe_error_code(error),
                        )
                    )

            body_metadata_failures = list(getattr(body_error, "failures", ()))
            all_failures = body_metadata_failures + body_failures + cleanup_failures
            body_handles = list(getattr(body_error, "unreleased_handles", ()))
            all_handles = body_handles + unreleased_handles
            if committed and (body_error is not None or all_failures):
                if body_error is not None and not all_failures:
                    all_failures.append(
                        ConfigOperationFailure(
                            "post-commit",
                            target_path,
                            _safe_error_code(body_error),
                        )
                    )
                original_error = getattr(body_error, "original_error", None)
                if original_error is None:
                    original_error = body_error
                all_owners = list(
                    getattr(body_error, "unreleased_handle_owners", ())
                )
                for owner in unreleased_handle_owners:
                    if owner not in all_owners:
                        all_owners.append(owner)
                post_error = ConfigPostCommitError(
                    target_path,
                    "post-commit operation cleanup is uncertain",
                    backup_path=self._backup_path,
                    original_error=original_error,
                    failures=tuple(all_failures),
                    unreleased_handles=tuple(dict.fromkeys(all_handles)),
                    unreleased_handle_owners=tuple(all_owners),
                )
                if original_error is not None:
                    raise post_error from original_error
                raise post_error
            if not committed and all_failures:
                original_error = getattr(body_error, "original_error", None)
                all_owners = list(
                    getattr(body_error, "unreleased_handle_owners", ())
                )
                for owner in unreleased_handle_owners:
                    if owner not in all_owners:
                        all_owners.append(owner)
                state_error = ConfigTransactionStateError(
                    target_path,
                    "pre-commit operation cleanup is uncertain",
                    backup_path=self._backup_path,
                    original_error=original_error or body_error,
                    failures=tuple(all_failures),
                    unreleased_handles=tuple(dict.fromkeys(all_handles)),
                    unreleased_handle_owners=tuple(all_owners),
                )
                if state_error.original_error is not None:
                    raise state_error from state_error.original_error
                raise state_error

    def _close_owned_lease_handle(self, kernel32: object, handle: int) -> bool:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        close_succeeded = _close_windows_handle(close_handle, handle)
        if close_succeeded and self._handle == handle:
            self._handle = None
            self._overlapped = None
            self._locked = False
        return close_succeeded

    def _release_windows(self) -> None:
        if self._handle is None or self._kernel32 is None:
            return
        handle = self._handle
        overlapped = self._overlapped
        kernel32 = self._kernel32
        try:
            release_result = self._release_windows_handle(
                kernel32,
                handle,
                overlapped,
            )
        except Exception as error:
            handle_owner = _WindowsHandleOwner(
                "lease:CloseHandle",
                self.path,
                handle,
                lambda value: self._close_owned_lease_handle(kernel32, value),
            )
            failures = (
                ConfigOperationFailure(
                    "lease release",
                    self.path,
                    _safe_error_code(error),
                ),
            )
            error_type = (
                ConfigPostCommitError
                if self._replacement_committed
                else ConfigTransactionStateError
            )
            raise error_type(
                self.path,
                "unable to release Windows config lock",
                backup_path=self._backup_path
                if self._replacement_committed
                else self._backup_path or self._pending_backup_cleanup,
                original_error=error,
                failures=failures,
                unreleased_handles=(handle,),
                unreleased_handle_owners=(handle_owner,),
            ) from error
        normalized = _normalize_windows_release(release_result)
        failures = _windows_release_failures(normalized, self.path, "lease")
        if normalized.close_succeeded:
            self._handle = None
            self._overlapped = None
            self._locked = False
        if failures:
            error_type = (
                ConfigPostCommitError
                if self._replacement_committed
                else ConfigTransactionStateError
            )
            handle_owners: tuple[_WindowsHandleOwner, ...] = ()
            if not normalized.close_succeeded:
                handle_owners = (
                    _WindowsHandleOwner(
                        "lease:CloseHandle",
                        self.path,
                        handle,
                        lambda value: self._close_owned_lease_handle(
                            kernel32,
                            value,
                        ),
                    ),
                )
            raise error_type(
                self.path,
                "unable to release Windows config lock",
                backup_path=self._backup_path
                if self._replacement_committed
                else self._backup_path or self._pending_backup_cleanup,
                failures=tuple(failures),
                unreleased_handles=()
                if normalized.close_succeeded
                else (handle,),
                unreleased_handle_owners=handle_owners,
            )

    def _release_windows_handle(
        self,
        kernel32: object,
        handle: int,
        overlapped: _WindowsOverlapped | None,
    ) -> _WindowsHandleReleaseResult:
        unlock_error: int | str | None = None
        if overlapped is not None:
            try:
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
            except Exception as error:
                unlock_error = _safe_error_code(error)
        close_succeeded = False
        close_error: int | str | None = None
        try:
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            close_succeeded = _close_windows_handle(close_handle, handle)
            if not close_succeeded:
                close_error = ctypes.get_last_error()
        except Exception as error:
            close_error = _safe_error_code(error)
        return _WindowsHandleReleaseResult(
            close_succeeded=close_succeeded,
            unlock_error=unlock_error,
            close_error=close_error,
        )

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
    native_catalog_path: Path | None = None,
    bundled_catalog_path: Path | None = None,
    router_base_url: str | None = None,
    verification: PickerVerificationReceipt | None = None,
) -> str:
    """Render an externally-attested native catalog path and local Router provider."""

    try:
        catalog = load_catalog(Path(catalog_path))
    except CatalogValidationError as error:
        raise ConfigError(str(error)) from error
    try:
        validate_picker_verification(catalog, verification)
    except CatalogValidationError as error:
        raise ConfigError(str(error)) from error
    router_base_url = router_base_url or DEFAULT_ROUTER_BASE_URL
    parsed = urlparse(router_base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ConfigError("router_base_url must be a loopback URL")
    if native_catalog_path is None:
        native_catalog_path = Path(catalog_path).with_suffix(".native.json")
    native_path = Path(native_catalog_path).resolve()
    try:
        write_native_catalog(
            Path(catalog_path),
            native_path,
            bundled_catalog_path=bundled_catalog_path,
        )
    except CatalogValidationError as error:
        raise ConfigError(str(error)) from error
    return "\n".join(
        (
            MANAGED_START,
            f"model_provider = {json.dumps(catalog.provider_id, ensure_ascii=False)}",
            f"model_catalog_json = {json.dumps(str(native_path), ensure_ascii=False)}",
            "",
            f"[model_providers.{json.dumps(catalog.provider_id, ensure_ascii=False)}]",
            f"name = {json.dumps(catalog.provider_id, ensure_ascii=False)}",
            f"base_url = {json.dumps(router_base_url, ensure_ascii=False)}",
            'wire_api = "responses"',
            "requires_openai_auth = false",
            MANAGED_END,
        )
    )


def apply_managed_config(
    config_path: Path,
    catalog_path: Path,
    *,
    native_catalog_path: Path | None = None,
    bundled_catalog_path: Path | None = None,
    router_base_url: str | None = None,
    verification: PickerVerificationReceipt | None = None,
) -> ConfigReceipt:
    catalog_path = Path(catalog_path)
    render_kwargs: dict[str, object] = {"verification": verification}
    if native_catalog_path is not None:
        render_kwargs["native_catalog_path"] = native_catalog_path
    if bundled_catalog_path is not None:
        render_kwargs["bundled_catalog_path"] = bundled_catalog_path
    if router_base_url is not None:
        render_kwargs["router_base_url"] = router_base_url
    managed_block = render_managed_config(catalog_path, **render_kwargs)
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
    lease._backup_path = backup_path
    lease._pending_backup_cleanup = backup_path
    os.close(backup_descriptor)
    try:
        with _exclusive_path_lock(backup_path, create=False) as backup_lease:
            _atomic_write(backup_path, original, expected=b"", lease=backup_lease)
    except (ConfigPostCommitError, ConfigTransactionStateError) as error:
        error.backup_path = backup_path
        raise
    except Exception:
        raise
    try:
        _atomic_write(config_path, written, expected=original, lease=lease)
    except (ConfigPostCommitError, ConfigTransactionStateError) as error:
        error.backup_path = backup_path
        raise
    except Exception:
        raise
    lease._pending_backup_cleanup = None
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
            lease._backup_path = receipt.backup_path
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
    replacement_committed = False
    body_error: Exception | None = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not None:
            _assert_current_bytes(path, expected, lease=lease)
        try:
            _replace_temp_file(temporary_path, path, lease=lease, expected=data)
        except ConfigPostCommitError:
            raise
        except Exception as error:
            if lease is not None and getattr(lease, "_replacement_committed", False):
                raise ConfigPostCommitError(
                    path,
                    "replacement raised after the transaction committed",
                    backup_path=lease._backup_path,
                    original_error=error,
                    failures=(
                        ConfigOperationFailure(
                            "replacement",
                            path,
                            _safe_error_code(error),
                        ),
                    ),
                ) from error
            raise
        replacement_committed = True
    except Exception as error:
        body_error = error
        raise
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except Exception as cleanup_error:
            if replacement_committed or (
                lease is not None and getattr(lease, "_replacement_committed", False)
            ):
                body_failures = list(getattr(body_error, "failures", ()))
                body_failures.append(
                    ConfigOperationFailure(
                        "unlink(temporary)",
                        temporary_path,
                        _safe_error_code(cleanup_error),
                    )
                )
                original_error = getattr(body_error, "original_error", None)
                if original_error is None:
                    original_error = body_error
                post_error = ConfigPostCommitError(
                    path,
                    "unable to remove the temporary replacement file",
                    backup_path=lease._backup_path if lease is not None else None,
                    temporary_path=temporary_path,
                    cleanup_error=cleanup_error,
                    original_error=original_error,
                    failures=tuple(body_failures),
                    unreleased_handles=tuple(
                        getattr(body_error, "unreleased_handles", ())
                    ),
                    unreleased_handle_owners=tuple(
                        getattr(body_error, "unreleased_handle_owners", ())
                    ),
                )
                if original_error is not None:
                    raise post_error from original_error
                raise post_error from cleanup_error
            backup_path = None
            if lease is not None:
                backup_path = lease._backup_path or lease._pending_backup_cleanup
            state_error = ConfigTransactionStateError(
                path,
                "temporary replacement cleanup failed before commit; "
                "temporary evidence is retained",
                backup_path=backup_path,
                temporary_path=temporary_path,
                original_error=getattr(body_error, "original_error", None)
                or body_error,
                cleanup_error=cleanup_error,
                failures=tuple(
                    [
                        *getattr(body_error, "failures", ()),
                        ConfigOperationFailure(
                            "unlink(temporary)",
                            temporary_path,
                            _safe_error_code(cleanup_error),
                        ),
                    ]
                ),
                unreleased_handles=tuple(
                    getattr(body_error, "unreleased_handles", ())
                ),
                unreleased_handle_owners=tuple(
                    getattr(body_error, "unreleased_handle_owners", ())
                ),
            )
            original_error = state_error.original_error
            if original_error is not None:
                raise state_error from original_error
            raise state_error from cleanup_error


def _configure_create_file_transacted(create_file_transacted):
    """Configure the ten-parameter Win32 CreateFileTransactedW ABI."""

    create_file_transacted.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.c_uint32,
    ]
    create_file_transacted.restype = ctypes.c_void_p
    return create_file_transacted


def _call_create_file_transacted(create_file_transacted, path: Path, transaction_handle):
    return create_file_transacted(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002,  # share read/write; deny DELETE/RENAME
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
        transaction_handle,
        None,
        0,
    )


def _commit_windows_transaction(commit_transaction, transaction_handle) -> bool:
    """Keep the transaction linearization point independently testable."""

    return bool(commit_transaction(transaction_handle))


def _rollback_windows_transaction(rollback_transaction, transaction_handle) -> bool:
    return bool(rollback_transaction(transaction_handle))


def _close_windows_transaction(close_handle, transaction_handle) -> bool:
    return bool(close_handle(transaction_handle))


def _close_windows_handle(close_handle, handle) -> bool:
    return bool(close_handle(handle))


def _lock_windows_file(lock_file, handle, overlapped: _WindowsOverlapped) -> bool:
    return bool(
        lock_file(
            handle,
            0x00000002 | 0x00000001,  # LOCKFILE_EXCLUSIVE_LOCK | FAIL_IMMEDIATELY
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(overlapped),
        )
    )


def _close_precommit_handle(close_handle, handle) -> bool:
    return _close_windows_handle(close_handle, handle)


def _close_source_handle(close_handle, handle) -> bool:
    return _close_windows_handle(close_handle, handle)


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
