"""Bounded binary tree transfer without executing workspace content.

Directory descriptors and no-follow opens keep repository symlinks from
redirecting host reads or writes. The lease facade holds the dispatch pin for
the complete operation; callers cannot snapshot a running native executor.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import PurePosixPath
import stat
import unicodedata

HOST_FEATURE = "workspace_trees.v1"
MAX_ENTRIES = 100_000
MAX_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024


def supported():
    return os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")


def _require_support():
    if not supported():
        raise NotImplementedError("Workspace tree transfer requires directory-relative no-follow operations")


def _path(value):
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ValueError("Tree entry requires a normalized repository path")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or str(path) != value or any(p in {".", ".."} or ":" in p for p in path.parts):
        raise ValueError("Tree path must stay inside the leased workspace")
    value.encode("utf-8", errors="strict")
    return path.parts


def _content(entry):
    if entry.get("mode") not in {"100644", "100755", "120000"}:
        raise ValueError("Unsupported tree entry mode")
    try:
        raw = base64.b64decode(entry["data"], validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Tree entry requires base64 content") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Workspace tree entry exceeds transfer limit")
    if entry["mode"] == "120000":
        if not raw or b"\0" in raw:
            raise ValueError("Invalid repository symlink target")
        raw.decode("utf-8", errors="strict")
    expected = entry.get("sha256")
    if expected is not None and expected != hashlib.sha256(raw).hexdigest():
        raise ValueError("Tree entry content digest does not match")
    return raw


def _validated(entries):
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise ValueError("Workspace tree entry limit exceeded")
    aliases, files, total = {}, [], 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid tree entry")
        parts = _path(entry.get("path"))
        raw = _content(entry)
        total += len(raw)
        if total > MAX_BYTES:
            raise ValueError("Workspace tree transfer limit exceeded")
        # Reject aliases in every component, including directory spellings.
        for index in range(1, len(parts) + 1):
            spelling = "/".join(parts[:index])
            alias = unicodedata.normalize("NFC", spelling).casefold()
            if alias in aliases and aliases[alias] != spelling:
                raise ValueError("Ambiguous repository path aliases")
            aliases[alias] = spelling
        files.append((parts, entry["mode"], raw))
    paths = [p for p, _m, _r in files]
    if len(set(paths)) != len(paths):
        raise ValueError("Duplicate repository path")
    leaves = set(paths)
    if any(parts[:i] in leaves for parts in paths for i in range(1, len(parts))):
        raise ValueError("A repository file cannot contain other tree entries")
    return sorted(files)


def _open_root(root):
    _require_support()
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _parent(root_fd, parts):
    current = os.dup(root_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o755, dir_fd=current)
            except FileExistsError:
                pass  # Existing directories are verified by the no-follow open.
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def materialize(root, entries):
    files = _validated(entries)
    root_fd = _open_root(root)
    try:
        if os.listdir(root_fd):
            raise ValueError("Materialization requires an empty leased workspace")
        for parts, mode, raw in files:
            parent = _parent(root_fd, parts[:-1])
            try:
                if mode == "120000":
                    os.symlink(raw.decode("utf-8"), parts[-1], dir_fd=parent)
                else:
                    fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o755 if mode == "100755" else 0o644, dir_fd=parent)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                os.fsync(parent)
            finally:
                os.close(parent)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return snapshot(root)


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode


def snapshot(root):
    root_fd = _open_root(root)
    entries, aliases, total = [], {}, [0]

    def walk(directory, prefix):
        before = os.fstat(directory)
        for name in sorted(os.listdir(directory)):
            path = "/".join((*prefix, name))
            _path(path)
            alias = unicodedata.normalize("NFC", path).casefold()
            if alias in aliases and aliases[alias] != path:
                raise ValueError("Ambiguous workspace path aliases")
            aliases[alias] = path
            if len(aliases) > MAX_ENTRIES:
                raise ValueError("Workspace tree entry limit exceeded")
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    walk(child, (*prefix, name))
                finally:
                    os.close(child)
                continue
            if stat.S_ISLNK(info.st_mode):
                raw, mode = os.readlink(name, dir_fd=directory).encode("utf-8"), "120000"
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > MAX_FILE_BYTES or total[0] + info.st_size > MAX_BYTES:
                    raise ValueError("Workspace tree transfer limit exceeded")
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or _stamp(opened) != _stamp(info):
                        raise RuntimeError("Workspace entry changed during snapshot")
                    raw = stream.read(MAX_FILE_BYTES + 1)
                    if _stamp(os.fstat(stream.fileno())) != _stamp(info):
                        raise RuntimeError("Workspace entry changed during snapshot")
                mode = "100755" if info.st_mode & 0o111 else "100644"
            else:
                raise ValueError("Workspace contains an unsupported special file")
            if _stamp(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _stamp(info):
                raise RuntimeError("Workspace entry changed during snapshot")
            total[0] += len(raw)
            if len(raw) > MAX_FILE_BYTES or total[0] > MAX_BYTES:
                raise ValueError("Workspace tree transfer limit exceeded")
            entries.append({"path": path, "mode": mode, "data": base64.b64encode(raw).decode("ascii"),
                            "sha256": hashlib.sha256(raw).hexdigest()})
        if _stamp(os.fstat(directory)) != _stamp(before):
            raise RuntimeError("Workspace directory changed during snapshot")

    try:
        walk(root_fd, ())
    finally:
        os.close(root_fd)
    return {"entries": sorted(entries, key=lambda i: i["path"]), "totalBytes": total[0], "complete": True}
