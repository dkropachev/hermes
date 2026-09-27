"""Handle-relative, no-follow filesystem boundary for plugin workspace leases."""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hermes_cli.plugin_workspace_errors import WorkspaceDurabilityError, WorkspacePathError


_WINDOWS_FILE_READ_ATTRIBUTES = 0x00000080
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_GENERIC_WRITE = 0x40000000
_WINDOWS_READ_CONTROL = 0x00020000
_WINDOWS_WRITE_DAC = 0x00040000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_CREATE_NEW = 1
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_OPEN_ALWAYS = 4
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_FLAG_WRITE_THROUGH = 0x80000000


class HeldDirectory:
    """Verified parent identity held across relative mutations and strict metadata flushes."""

    def __init__(
        self, path: Path, *, parent: "HeldDirectory | None" = None, name: str | None = None,
    ) -> None:
        self.path = path
        self.parent = parent
        self.name = name
        self.fd: int | None = None
        self.handle = None
        self.identity: tuple[int, ...] | None = None

    def __enter__(self) -> "HeldDirectory":
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                self.handle, self.identity = windows_hold_directory(self.path)
            else:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                if self.parent is not None and self.parent.fd is not None:
                    self.fd = os.open(self.name or "", flags, dir_fd=self.parent.fd)
                else:
                    self.fd = os.open(self.path, flags)
                info = os.fstat(self.fd)
                if not stat.S_ISDIR(info.st_mode):
                    raise WorkspacePathError(f"workspace parent is not a directory: {self.path}")
                self.identity = (info.st_dev, info.st_ino)
            self.verify()
            return self
        except BaseException:
            self._close(strict=False)
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        verify_error = None
        if exc_type is None:
            try:
                self.verify()
            except BaseException as raised:
                verify_error = raised
        self._close(strict=exc_type is None and verify_error is None)
        if verify_error is not None:
            raise verify_error

    def _close(self, *, strict: bool) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.close(fd)
            except OSError:
                if strict:
                    raise
        if self.handle is not None:  # pragma: no cover - exercised on Windows CI
            handle, self.handle = self.handle, None
            try:
                windows_close_handle(handle)
            except OSError:
                if strict:
                    raise

    def verify(self) -> None:
        try:
            if self.parent is not None:
                self.parent.verify()
                info = self.parent.stat(self.name or "")
            else:
                info = self.path.lstat()
        except OSError as exc:
            raise WorkspacePathError(f"workspace parent disappeared: {self.path}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkspacePathError(f"workspace parent was replaced or aliased: {self.path}")
        current = windows_path_identity(self.path) if os.name == "nt" else (info.st_dev, info.st_ino)
        if current != self.identity:
            raise WorkspacePathError(f"workspace parent identity changed: {self.path}")

    def child_directory(self, name: str) -> "HeldDirectory":
        return HeldDirectory(self.path / name, parent=self, name=name)

    def harden_security(self) -> None:
        """Apply the private Windows DACL after the caller authenticates this directory.

        Holding a directory is intentionally read-only: the outer HERMES_HOME is held by the
        same class but is not workspace-owned.  Registry code calls this method only for the
        authenticated plugin-owned descendants.
        """
        if os.name != "nt":
            return
        self.verify()
        windows_harden_directory(self.path, self.identity)
        self.verify()

    def secure_regular_file(self, name: str, *, writable: bool) -> None:
        """Verify, or verify then harden, an existing private Windows regular file."""
        if os.name != "nt":
            return
        self.verify()
        handle, _identity = windows_hold_regular_file(
            self.path / name, create=False, writable=writable,
        )
        try:
            self.verify()
        finally:
            windows_close_handle(handle)

    def exists(self, name: str) -> bool:
        try:
            self.stat(name)
            return True
        except FileNotFoundError:
            return False

    def stat(self, name: str):
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        self.verify()
        return (self.path / name).lstat()

    def list_names(self) -> list[str]:
        return os.listdir(self.fd if self.fd is not None else self.path)

    def child_path(self, name: str) -> Path:
        if self.fd is not None:
            for root in (Path(f"/proc/self/fd/{self.fd}"), Path(f"/dev/fd/{self.fd}")):
                if root.exists():
                    return root / name
        self.verify()
        return self.path / name

    def identity_json(self) -> list[int]:
        if self.identity is None:
            raise WorkspacePathError(f"workspace parent identity unavailable: {self.path}")
        return [int(part) for part in self.identity]

    def chmod(self, name: str, mode: int) -> None:
        if self.fd is not None:
            os.chmod(name, mode, dir_fd=self.fd, follow_symlinks=False)
        else:  # pragma: no cover - exercised on Windows CI
            self.verify()
            os.chmod(self.path / name, mode)

    def is_regular_file(self, name: str) -> bool:
        try:
            info = self.stat(name)
        except FileNotFoundError:
            return False
        return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)

    def sync(self) -> None:
        if self.fd is not None:
            os.fsync(self.fd)
        else:  # pragma: no cover - Windows namespace durability comes from write-through moves
            self.verify()

    def mkdir(self, name: str, mode: int = 0o700) -> None:
        if self.fd is not None:
            os.mkdir(name, mode, dir_fd=self.fd)
            child_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self.fd,
            )
            try:
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
            self.sync()
        else:  # pragma: no cover - exercised on Windows CI
            staging = f".{name}.creating-{secrets.token_hex(8)}"
            windows_create_private_directory(self.path / staging)
            try:
                with HeldDirectory(self.path / staging):
                    pass
                windows_move_write_through(
                    self.path / staging, self.path / name, replace=False,
                )
            except BaseException:
                try:
                    os.rmdir(self.path / staging)
                except OSError:
                    pass
                raise
        self.verify()

    def rmdir(self, name: str) -> None:
        if self.fd is not None:
            os.rmdir(name, dir_fd=self.fd)
        else:  # pragma: no cover - exercised on Windows CI
            tombstone = f".{name}.removing-{secrets.token_hex(8)}"
            windows_move_write_through(
                self.path / name, self.path / tombstone, replace=False,
            )
            try:
                os.rmdir(self.path / tombstone)
            except OSError:
                if not (self.path / name).exists():
                    try:
                        windows_move_write_through(
                            self.path / tombstone, self.path / name, replace=False,
                        )
                    except OSError:
                        pass
                raise
        try:
            self.sync()
        except OSError as exc:
            raise WorkspaceDurabilityError(
                f"workspace removal completed but metadata flush failed: {exc}",
                mutation_completed=True,
            ) from exc
        self.verify()

    def rename_to(self, name: str, target: "HeldDirectory", target_name: str) -> None:
        if self.fd is not None and target.fd is not None:
            os.rename(name, target_name, src_dir_fd=self.fd, dst_dir_fd=target.fd)
        else:  # pragma: no cover - exercised on Windows CI
            # MoveFileExW renames a junction itself, but later cleanup classification could follow
            # it.  Refuse any source that cannot first be held as a non-reparse directory.
            with self.child_directory(name):
                pass
            windows_move_write_through(
                self.path / name, target.path / target_name, replace=False,
            )
        try:
            self.sync()
            target.sync()
        except OSError as exc:
            raise WorkspaceDurabilityError(
                f"filesystem rename completed but metadata flush failed: {exc}",
                mutation_completed=True,
            ) from exc
        self.verify()
        target.verify()


class HeldRegularFile:
    """No-follow regular-file identity guard held while another API reopens the leaf."""

    def __init__(
        self, parent: HeldDirectory, name: str, mode: int = 0o600, *,
        create: bool = True, exclusive: bool = False, writable: bool = True,
    ) -> None:
        self.parent, self.name, self.mode = parent, name, mode
        self.create, self.exclusive, self.writable = create, exclusive, writable
        self.fd: int | None = None
        self.handle = None
        self.identity: tuple[int, ...] | None = None

    def __enter__(self) -> "HeldRegularFile":
        try:
            if self.parent.fd is not None:
                access = os.O_RDWR if self.writable else os.O_RDONLY
                creation = (os.O_CREAT if self.create else 0) | (
                    os.O_EXCL if self.exclusive else 0
                )
                self.fd = os.open(
                    self.name,
                    access | creation | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    self.mode, dir_fd=self.parent.fd,
                )
                info = os.fstat(self.fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkspacePathError("workspace database leaf is not a regular file")
                if self.create or self.writable:
                    os.fchmod(self.fd, self.mode)
                self.identity = (info.st_dev, info.st_ino)
            else:  # pragma: no cover - exercised on Windows CI
                self.parent.verify()
                self.handle, self.identity = windows_hold_regular_file(
                    self.parent.path / self.name, create=self.create,
                    exclusive=self.exclusive, writable=self.writable,
                )
            self.verify()
            return self
        except BaseException:
            self.close(strict=False)
            raise

    def verify(self) -> None:
        info = self.parent.stat(self.name)
        if (
            stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise WorkspacePathError("workspace database leaf was replaced or aliased")
        current = (
            windows_path_file_identity(self.parent.path / self.name)
            if self.parent.fd is None else (info.st_dev, info.st_ino)
        )
        if current != self.identity:
            raise WorkspacePathError("workspace database leaf identity changed")

    def open_path(self) -> tuple[Path, bool]:
        """Path SQLite may reopen plus whether SQLITE_OPEN_NOFOLLOW must be requested."""
        if self.fd is not None:
            for root in (Path(f"/proc/self/fd/{self.fd}"), Path(f"/dev/fd/{self.fd}")):
                if root.exists():
                    return root, False
            raise WorkspacePathError("no descriptor filesystem is available for SQLite")
        return self.parent.path / self.name, True  # pragma: no cover - Windows CI

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(strict=exc_type is None)

    def read(self, limit: int) -> bytes:
        if self.fd is not None:
            os.lseek(self.fd, 0, os.SEEK_SET)
            return os.read(self.fd, limit)
        return windows_read_handle(self.handle, limit)  # pragma: no cover - Windows CI

    def write(self, data: bytes) -> None:
        if not self.writable:
            raise WorkspacePathError("workspace private file is read-only")
        if self.fd is not None:
            os.lseek(self.fd, 0, os.SEEK_SET)
            written = 0
            while written < len(data):
                written += os.write(self.fd, data[written:])
            os.ftruncate(self.fd, len(data))
            return
        windows_write_handle(self.handle, data)  # pragma: no cover - Windows CI

    def sync(self) -> None:
        if self.fd is not None:
            os.fsync(self.fd)
        else:  # pragma: no cover - exercised on Windows CI
            windows_flush_handle(self.handle)

    def lock_exclusive(self) -> None:
        if self.fd is not None:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - exercised on Windows CI
            windows_lock_handle(self.handle)

    def unlock(self) -> None:
        if self.fd is not None:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_UN)
        else:  # pragma: no cover - exercised on Windows CI
            windows_unlock_handle(self.handle)

    def close(self, *, strict: bool = True) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.close(fd)
            except OSError:
                if strict:
                    raise
        if self.handle is not None:  # pragma: no cover - exercised on Windows CI
            handle, self.handle = self.handle, None
            try:
                windows_close_handle(handle)
            except OSError:
                if strict:
                    raise


def publish_private_file(parent: HeldDirectory, source: str, target: str) -> bool:
    """Publish *source* atomically under the caller's held cross-process lock."""
    if parent.exists(target):
        return False
    try:
        if parent.fd is not None:
            os.rename(source, target, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
        else:  # pragma: no cover - exercised on Windows CI
            windows_move_write_through(
                parent.path / source, parent.path / target, replace=False,
            )
    except OSError as exc:
        if isinstance(exc, FileExistsError) or getattr(exc, "winerror", None) in (80, 183):
            return False
        raise
    parent.sync()
    return True


def unlink_private_file(parent: HeldDirectory, name: str) -> None:
    if parent.fd is not None:
        os.unlink(name, dir_fd=parent.fd)
    else:  # pragma: no cover - exercised on Windows CI
        os.unlink(parent.path / name)
    parent.sync()


@contextmanager
def workspace_roots(layout: Any):
    with HeldDirectory(layout.workspaces_dir) as workspaces:
        with HeldDirectory(layout.quarantine_dir) as quarantine:
            yield workspaces, quarantine


def windows_kernel32():  # pragma: no cover - exercised on Windows CI
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.MoveFileExW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    kernel32.MoveFileExW.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = (
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    )
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = (
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    )
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.SetFilePointerEx.argtypes = (
        wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    )
    kernel32.SetFilePointerEx.restype = wintypes.BOOL
    kernel32.SetEndOfFile.argtypes = (wintypes.HANDLE,)
    kernel32.SetEndOfFile.restype = wintypes.BOOL
    return kernel32


def windows_close_handle(handle) -> None:  # pragma: no cover - exercised on Windows CI
    import ctypes

    if not windows_kernel32().CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_security_runtime():  # pragma: no cover - exercised on Windows CI
    # The SSH runtime already owns Hermes' owner+SYSTEM protected-DACL policy.  Importing it
    # lazily keeps this module importable off Windows and does not form a cycle.
    from hermes_cli import windows_ssh_runtime

    return windows_ssh_runtime


def _windows_private_security_attributes(
    *, directory: bool,
):  # pragma: no cover - exercised on Windows CI
    runtime = _windows_security_runtime()
    if not directory:
        return runtime._security_attributes()

    w = runtime._win32()
    ntsecuritycon, win32security = w.ntsecuritycon, w.win32security
    owner = runtime._current_sid()
    inherit = w.win32con.OBJECT_INHERIT_ACE | w.win32con.CONTAINER_INHERIT_ACE
    acl = win32security.ACL()
    for sid in (owner, runtime._system_sid()):
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION, inherit, ntsecuritycon.FILE_ALL_ACCESS, sid,
        )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(owner, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED,
    )
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def _windows_security_descriptor(handle):  # pragma: no cover - exercised on Windows CI
    w = _windows_security_runtime()._win32()
    information = (
        w.win32security.OWNER_SECURITY_INFORMATION
        | w.win32security.DACL_SECURITY_INFORMATION
    )
    return w.win32security.GetSecurityInfo(
        handle, w.win32security.SE_FILE_OBJECT, information,
    )


def _windows_verify_private_security(
    handle, *, directory: bool,
) -> None:  # pragma: no cover - exercised on Windows CI
    runtime = _windows_security_runtime()
    w = runtime._win32()
    ntsecuritycon, win32security = w.ntsecuritycon, w.win32security
    descriptor = _windows_security_descriptor(handle)
    allowed = runtime._allowed_sids()
    owner = descriptor.GetSecurityDescriptorOwner()
    if owner is None or runtime._sid_str(owner) != runtime._sid_str(runtime._current_sid()):
        raise WorkspacePathError("Windows workspace object has the wrong owner")
    control, _revision = descriptor.GetSecurityDescriptorControl()
    if not control & win32security.SE_DACL_PROTECTED:
        raise WorkspacePathError("Windows workspace object DACL is not protected")
    dacl = descriptor.GetSecurityDescriptorDacl()
    if dacl is None:
        raise WorkspacePathError("Windows workspace object has a null DACL")
    allow_types = {
        win32security.ACCESS_ALLOWED_ACE_TYPE,
        win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
        getattr(win32security, "ACCESS_ALLOWED_CALLBACK_ACE_TYPE", 9),
        getattr(win32security, "ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE", 11),
    }
    grants = {sid: 0 for sid in allowed}
    inherited = w.win32con.OBJECT_INHERIT_ACE | w.win32con.CONTAINER_INHERIT_ACE
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        ace_type, ace_flags = ace[0]
        if ace_type not in allow_types or not ace[1]:
            continue
        sid = runtime._sid_str(ace[-1])
        if sid not in allowed:
            raise WorkspacePathError("Windows workspace object DACL grants another principal")
        grants[sid] |= int(ace[1])
        if directory and ace_flags & inherited != inherited:
            raise WorkspacePathError("Windows workspace directory DACL is not inheritable")
    if any(
        mask & ntsecuritycon.FILE_ALL_ACCESS != ntsecuritycon.FILE_ALL_ACCESS
        for mask in grants.values()
    ):
        raise WorkspacePathError("Windows workspace object DACL is incomplete")


def _windows_harden_private_security(
    handle, *, directory: bool,
) -> None:  # pragma: no cover - exercised on Windows CI
    runtime = _windows_security_runtime()
    w = runtime._win32()
    descriptor = _windows_security_descriptor(handle)
    owner = descriptor.GetSecurityDescriptorOwner()
    if owner is None or runtime._sid_str(owner) != runtime._sid_str(runtime._current_sid()):
        raise WorkspacePathError("refusing to secure a Windows workspace object with a foreign owner")
    private = _windows_private_security_attributes(directory=directory).SECURITY_DESCRIPTOR
    information = (
        w.win32security.DACL_SECURITY_INFORMATION
        | getattr(w.win32security, "PROTECTED_DACL_SECURITY_INFORMATION", 0x80000000)
    )
    w.win32security.SetSecurityInfo(
        handle, w.win32security.SE_FILE_OBJECT, information,
        None, None, private.GetSecurityDescriptorDacl(), None,
    )
    _windows_verify_private_security(handle, directory=directory)


def _windows_open_handle(
    path: Path, access: int, creation: int, flags: int, *, directory: bool,
):  # pragma: no cover - exercised on Windows CI
    w = _windows_security_runtime()._win32()
    attributes = (
        _windows_private_security_attributes(directory=directory)
        if creation in (_WINDOWS_CREATE_NEW, _WINDOWS_OPEN_ALWAYS) else None
    )
    handle = w.win32file.CreateFile(
        str(path), access,
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
        attributes, creation, flags, None,
    )
    raw_handle = int(handle)
    handle.Detach()
    return raw_handle


def windows_hold_directory(path: Path):  # pragma: no cover - exercised on Windows CI
    handle = _windows_open_handle(
        path, _WINDOWS_FILE_READ_ATTRIBUTES | _WINDOWS_READ_CONTROL,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        directory=True,
    )
    try:
        identity = windows_directory_identity(handle)
    except BaseException:
        windows_close_handle(handle)
        raise
    return handle, identity


def windows_create_private_directory(path: Path) -> None:  # pragma: no cover - Windows CI
    w = _windows_security_runtime()._win32()
    w.win32file.CreateDirectory(
        str(path), _windows_private_security_attributes(directory=True),
    )
    handle, _identity = windows_hold_directory(path)
    try:
        _windows_verify_private_security(handle, directory=True)
    finally:
        windows_close_handle(handle)


def windows_harden_directory(
    path: Path, expected_identity: tuple[int, ...] | None,
) -> None:  # pragma: no cover - Windows CI
    handle = _windows_open_handle(
        path,
        _WINDOWS_FILE_READ_ATTRIBUTES | _WINDOWS_READ_CONTROL | _WINDOWS_WRITE_DAC,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        directory=True,
    )
    try:
        identity = windows_directory_identity(handle)
        if expected_identity is not None and identity != expected_identity:
            raise WorkspacePathError(f"workspace parent identity changed: {path}")
        _windows_harden_private_security(handle, directory=True)
    finally:
        windows_close_handle(handle)


def windows_hold_regular_file(
    path: Path, *, create: bool = True, exclusive: bool = False, writable: bool = True,
):  # pragma: no cover - exercised on Windows CI
    access = _WINDOWS_GENERIC_READ | _WINDOWS_READ_CONTROL
    if writable:
        access |= _WINDOWS_GENERIC_WRITE | _WINDOWS_WRITE_DAC
    disposition = (
        _WINDOWS_CREATE_NEW if exclusive
        else _WINDOWS_OPEN_ALWAYS if create
        else _WINDOWS_OPEN_EXISTING
    )
    flags = _WINDOWS_FILE_ATTRIBUTE_NORMAL | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
    if writable:
        flags |= _WINDOWS_FILE_FLAG_WRITE_THROUGH
    handle = _windows_open_handle(
        path, access, disposition, flags, directory=False,
    )
    try:
        identity = windows_regular_file_identity(handle)
        if writable:
            _windows_harden_private_security(handle, directory=False)
        else:
            _windows_verify_private_security(handle, directory=False)
    except BaseException:
        windows_close_handle(handle)
        raise
    return handle, identity


def _windows_handle_info(handle):  # pragma: no cover - Windows CI
    import ctypes
    from ctypes import wintypes

    class FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD), ("creation_low", wintypes.DWORD),
            ("creation_high", wintypes.DWORD), ("access_low", wintypes.DWORD),
            ("access_high", wintypes.DWORD), ("write_low", wintypes.DWORD),
            ("write_high", wintypes.DWORD), ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD), ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = windows_kernel32()
    kernel32.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(FileInfo))
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    info = FileInfo()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info


def _windows_file_id(handle) -> tuple[int, ...]:  # pragma: no cover - Windows CI
    import ctypes
    from ctypes import wintypes

    class FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("volume_serial", ctypes.c_ulonglong),
            ("file_id", ctypes.c_ubyte * 16),
        ]

    kernel32 = windows_kernel32()
    kernel32.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    info = FileIdInfo()
    if not kernel32.GetFileInformationByHandleEx(
        handle, 18, ctypes.byref(info), ctypes.sizeof(info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    volume = int(info.volume_serial)
    identifier = tuple(int(part) for part in info.file_id)
    if not any(identifier):
        raise WorkspacePathError("Windows file identity is unavailable")
    return (volume, *identifier)


def windows_directory_identity(handle) -> tuple[int, ...]:  # pragma: no cover
    info = _windows_handle_info(handle)
    if info.attributes & 0x400:
        raise WorkspacePathError("workspace parent is a Windows reparse point")
    if not info.attributes & 0x10:
        raise WorkspacePathError("workspace parent is not a directory")
    return _windows_file_id(handle)


def windows_regular_file_identity(handle) -> tuple[int, ...]:  # pragma: no cover
    info = _windows_handle_info(handle)
    if info.attributes & (0x400 | 0x10) or info.links != 1:
        raise WorkspacePathError("workspace database leaf is unsafe")
    return _windows_file_id(handle)


def windows_path_identity(path: Path) -> tuple[int, ...]:  # pragma: no cover
    handle, identity = windows_hold_directory(path)
    try:
        return identity
    finally:
        windows_close_handle(handle)


def windows_path_file_identity(path: Path) -> tuple[int, ...]:  # pragma: no cover
    handle, identity = windows_hold_regular_file(
        path, create=False, writable=False,
    )
    try:
        return identity
    finally:
        windows_close_handle(handle)


def windows_read_handle(handle, limit: int) -> bytes:  # pragma: no cover - Windows CI
    import ctypes
    from ctypes import wintypes

    kernel32 = windows_kernel32()
    if not kernel32.SetFilePointerEx(handle, 0, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(limit)
    read = wintypes.DWORD()
    if not kernel32.ReadFile(handle, buffer, limit, ctypes.byref(read), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.raw[:read.value]


def windows_write_handle(handle, data: bytes) -> None:  # pragma: no cover - Windows CI
    import ctypes
    from ctypes import wintypes

    kernel32 = windows_kernel32()
    if not kernel32.SetFilePointerEx(handle, 0, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    written = wintypes.DWORD()
    if not kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None):
        raise ctypes.WinError(ctypes.get_last_error())
    if written.value != len(data) or not kernel32.SetEndOfFile(handle):
        error = ctypes.get_last_error()
        if error:
            raise ctypes.WinError(error)
        raise OSError("short write to workspace private file")


def _windows_overlapped_type():  # pragma: no cover - Windows CI
    import ctypes
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    return Overlapped


def windows_lock_handle(handle) -> None:  # pragma: no cover - Windows CI
    import ctypes
    from ctypes import wintypes

    overlapped_type = _windows_overlapped_type()
    kernel32 = windows_kernel32()
    kernel32.LockFileEx.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(overlapped_type),
    )
    kernel32.LockFileEx.restype = wintypes.BOOL
    overlapped = overlapped_type()
    if not kernel32.LockFileEx(handle, 0x2, 0, 1, 0, ctypes.byref(overlapped)):
        raise ctypes.WinError(ctypes.get_last_error())


def windows_unlock_handle(handle) -> None:  # pragma: no cover - Windows CI
    import ctypes
    from ctypes import wintypes

    overlapped_type = _windows_overlapped_type()
    kernel32 = windows_kernel32()
    kernel32.UnlockFileEx.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(overlapped_type),
    )
    kernel32.UnlockFileEx.restype = wintypes.BOOL
    overlapped = overlapped_type()
    if not kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
        raise ctypes.WinError(ctypes.get_last_error())


def windows_flush_handle(handle) -> None:  # pragma: no cover
    import ctypes

    if not windows_kernel32().FlushFileBuffers(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def windows_move_write_through(
    source: Path, target: Path, *, replace: bool = False,
) -> None:  # pragma: no cover
    import ctypes

    flags = 0x8 | (0x1 if replace else 0)
    if not windows_kernel32().MoveFileExW(str(source), str(target), flags):
        raise ctypes.WinError(ctypes.get_last_error())


def strict_sync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        with HeldDirectory(path):
            return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_dirs(*paths: Path) -> None:
    for path in dict.fromkeys(paths):
        strict_sync_directory(path)


def strict_replace(source: Path, target: Path, *sync_paths: Path) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        windows_move_write_through(source, target, replace=True)
    else:
        os.replace(source, target)
    try:
        sync_dirs(*sync_paths)
    except OSError as exc:
        raise WorkspaceDurabilityError(
            f"filesystem rename completed but metadata flush failed: {exc}",
            mutation_completed=True,
        ) from exc


def safe_child(parent: Path, name: str) -> Path:
    candidate = parent / name
    with HeldDirectory(parent) as held:
        if not held.exists(name):
            try:
                held.mkdir(name, 0o700)
            except FileExistsError:
                pass  # another process published the same host-owned directory
            except OSError as exc:
                raise WorkspacePathError(
                    f"cannot create workspace directory {candidate}: {exc}"
                ) from exc
        info = held.stat(name)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkspacePathError(f"workspace directory is not a regular directory: {candidate}")
        if held.fd is not None:
            child_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=held.fd,
            )
            try:
                child_info = os.fstat(child_fd)
                if not stat.S_ISDIR(child_info.st_mode):
                    raise WorkspacePathError(
                        f"workspace directory is not a regular directory: {candidate}"
                    )
            finally:
                os.close(child_fd)
        else:  # pragma: no cover - exercised on Windows CI
            with HeldDirectory(candidate):
                pass
        if os.name != "nt":
            try:
                os.chmod(name, 0o700, dir_fd=held.fd, follow_symlinks=False)
            except OSError as exc:
                raise WorkspacePathError(
                    f"cannot secure workspace directory {candidate}: {exc}"
                ) from exc
        held.verify()
    return candidate
