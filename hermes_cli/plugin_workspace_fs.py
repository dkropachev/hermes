"""Handle-relative, no-follow filesystem boundary for plugin workspace leases."""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hermes_cli.plugin_workspace_errors import WorkspaceDurabilityError, WorkspacePathError


class HeldDirectory:
    """Verified parent identity held across relative mutations and strict metadata flushes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None
        self.handle = None
        self.identity: tuple[int, ...] | None = None

    def __enter__(self) -> "HeldDirectory":
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                self.handle, self.identity = windows_hold_directory(self.path)
            else:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
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
            import ctypes

            handle, self.handle = self.handle, None
            if not windows_kernel32().CloseHandle(handle) and strict:
                raise ctypes.WinError(ctypes.get_last_error())

    def verify(self) -> None:
        try:
            info = self.path.lstat()
        except OSError as exc:
            raise WorkspacePathError(f"workspace parent disappeared: {self.path}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkspacePathError(f"workspace parent was replaced or aliased: {self.path}")
        current = windows_path_identity(self.path) if os.name == "nt" else (info.st_dev, info.st_ino)
        if current != self.identity:
            raise WorkspacePathError(f"workspace parent identity changed: {self.path}")

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
            os.mkdir(self.path / staging, mode)
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

    def __init__(self, parent: HeldDirectory, name: str, mode: int = 0o600) -> None:
        self.parent, self.name, self.mode = parent, name, mode
        self.fd: int | None = None
        self.handle = None
        self.identity: tuple[int, ...] | None = None

    def __enter__(self) -> "HeldRegularFile":
        try:
            if self.parent.fd is not None:
                self.fd = os.open(
                    self.name,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    self.mode, dir_fd=self.parent.fd,
                )
                info = os.fstat(self.fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkspacePathError("workspace database leaf is not a regular file")
                os.fchmod(self.fd, self.mode)
                self.identity = (info.st_dev, info.st_ino)
            else:  # pragma: no cover - exercised on Windows CI
                self.handle, self.identity = windows_hold_regular_file(
                    self.parent.path / self.name,
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
            or (self.fd is not None and info.st_nlink != 1)
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

    def close(self, *, strict: bool = True) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.close(fd)
            except OSError:
                if strict:
                    raise
        if self.handle is not None:  # pragma: no cover - exercised on Windows CI
            import ctypes

            handle, self.handle = self.handle, None
            if not windows_kernel32().CloseHandle(handle) and strict:
                raise ctypes.WinError(ctypes.get_last_error())


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
    return kernel32


def windows_hold_directory(path: Path):  # pragma: no cover - exercised on Windows CI
    import ctypes

    kernel32 = windows_kernel32()
    handle = kernel32.CreateFileW(
        str(path), 0x80, 0x00000001 | 0x00000002,
        None, 3, 0x02000000 | 0x00200000, None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        identity = windows_directory_identity(handle)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    return handle, identity


def windows_hold_regular_file(path: Path):  # pragma: no cover - exercised on Windows CI
    import ctypes

    kernel32 = windows_kernel32()
    handle = kernel32.CreateFileW(
        str(path), 0x80000000 | 0x40000000, 0x00000001 | 0x00000002,
        None, 4, 0x00200000 | 0x80000000, None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        identity = windows_regular_file_identity(handle)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    return handle, identity


def windows_directory_identity(handle) -> tuple[int, ...]:  # pragma: no cover
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
    if info.attributes & 0x400:
        raise WorkspacePathError("workspace parent is a Windows reparse point")
    return (info.volume_serial, info.file_index_high, info.file_index_low)


def windows_regular_file_identity(handle) -> tuple[int, ...]:  # pragma: no cover
    identity = windows_directory_identity(handle)
    return identity


def windows_path_identity(path: Path) -> tuple[int, ...]:  # pragma: no cover
    import ctypes

    handle, identity = windows_hold_directory(path)
    try:
        return identity
    finally:
        if not windows_kernel32().CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def windows_path_file_identity(path: Path) -> tuple[int, ...]:  # pragma: no cover
    import ctypes

    handle, identity = windows_hold_regular_file(path)
    try:
        return identity
    finally:
        if not windows_kernel32().CloseHandle(handle):
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
