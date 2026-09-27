"""Parent-anchored storage registry for durable plugin workspace leases."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Mapping

from hermes_cli.plugin_workspace_errors import WorkspacePathError
from hermes_cli.plugin_workspace_fs import (
    HeldDirectory, HeldRegularFile, publish_private_file, unlink_private_file,
)


REGISTRY_NAME = ".plugin-workspace-leases"
OUTER_MARKER_NAME = ".plugin-workspace-roots-v1.json"
LOCK_NAME = ".plugin-workspace-roots.lock"
LEGACY_DATABASE_NAMES = (
    "workspace-leases.db", "workspace-leases.db-wal",
    "workspace-leases.db-shm", "workspace-leases.db-journal",
)
_MARKER_LIMIT = 4096


def registry_component(plugin_identity: str) -> str:
    digest = hashlib.sha256(plugin_identity.encode("utf-8")).hexdigest()[:24]
    return f"plugin-{digest}"


def inner_marker_name(plugin_identity: str) -> str:
    digest = hashlib.sha256(plugin_identity.encode("utf-8")).hexdigest()[:24]
    return f".workspace-namespace-{digest}.json"


def registry_binding_name(plugin_identity: str) -> str:
    digest = hashlib.sha256(plugin_identity.encode("utf-8")).hexdigest()[:24]
    return f".plugin-binding-{digest}.json"


def registry_paths(home: Path, plugin_identity: str) -> tuple[Path, Path, Path]:
    root = home / REGISTRY_NAME
    namespace = root / registry_component(plugin_identity)
    return root, namespace, namespace / "workspace-leases.db"


def _ensure_child(parent: HeldDirectory, name: str) -> HeldDirectory:
    created = False
    if not parent.exists(name):
        try:
            parent.mkdir(name, 0o700)
            created = True
        except FileExistsError:
            pass
    info = parent.stat(name)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspacePathError(f"workspace registry directory is unsafe: {parent.path / name}")
    if created and os.name != "nt":
        parent.chmod(name, 0o700)
    return parent.child_directory(name)


def _require_child(parent: HeldDirectory, name: str) -> HeldDirectory:
    if not parent.exists(name):
        raise WorkspacePathError(
            f"workspace registry binding exists but directory is missing: {parent.path / name}"
        )
    info = parent.stat(name)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspacePathError(f"workspace registry directory is unsafe: {parent.path / name}")
    return parent.child_directory(name)


def _read_marker(parent: HeldDirectory, name: str) -> dict[str, Any] | None:
    if not parent.exists(name):
        return None
    info = parent.stat(name)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WorkspacePathError(f"workspace registry marker is unsafe: {parent.path / name}")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
        raise WorkspacePathError(f"workspace registry marker is not private: {parent.path / name}")
    with HeldRegularFile(
        parent, name, create=False, writable=False,
    ) as marker:
        raw = marker.read(_MARKER_LIMIT + 1)
        marker.verify()
    if len(raw) > _MARKER_LIMIT:
        raise WorkspacePathError(f"workspace registry marker is oversized: {parent.path / name}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspacePathError(f"workspace registry marker is malformed: {parent.path / name}") from exc
    if not isinstance(value, dict):
        raise WorkspacePathError(f"workspace registry marker is malformed: {parent.path / name}")
    return value


def _publish_marker(parent: HeldDirectory, name: str, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > _MARKER_LIMIT:
        raise WorkspacePathError("workspace registry marker payload is oversized")
    temporary = f".{name}.creating-{secrets.token_hex(12)}"
    try:
        with HeldRegularFile(
            parent, temporary, create=True, exclusive=True, writable=True,
        ) as marker:
            marker.write(encoded)
            marker.sync()
        published = publish_private_file(parent, temporary, name)
        if parent.exists(temporary):
            unlink_private_file(parent, temporary)
        if not published:
            existing = _read_marker(parent, name)
            if existing != dict(payload):
                raise WorkspacePathError(
                    f"workspace registry marker conflicts with this profile: {parent.path / name}"
                )
    except BaseException:
        try:
            if parent.exists(temporary):
                unlink_private_file(parent, temporary)
        except OSError:
            pass
        raise
    if _read_marker(parent, name) != dict(payload):
        raise WorkspacePathError(f"workspace registry marker did not persist: {parent.path / name}")


def _expect_exact(actual: Mapping[str, Any] | None, expected: Mapping[str, Any], path: Path) -> None:
    if actual is not None and dict(actual) != dict(expected):
        raise WorkspacePathError(
            f"workspace registry marker conflicts with the canonical storage identity: {path}"
        )


class StorageAnchors:
    """Held HERMES_HOME-to-workspace chain plus crash-safe bootstrap lock."""

    def __init__(self, layout: Any, *, include_roots: bool, publish: bool) -> None:
        self.layout = layout
        self.include_roots = include_roots
        self.publish_requested = publish
        self.stack = ExitStack()
        self.home: HeldDirectory
        self.plugin_data: HeldDirectory
        self.data: HeldDirectory
        self.registry: HeldDirectory
        self.registry_namespace: HeldDirectory
        self.workspaces: HeldDirectory | None = None
        self.quarantine: HeldDirectory | None = None
        self.lock: HeldRegularFile
        self.outer_payload: dict[str, Any]
        self.binding_payload: dict[str, Any] | None = None
        self.outer_missing = False
        self.inner_missing = False
        self.binding_missing = False
        self.unbound_registry_names: list[str] = []
        self.markers_published = False

    def __enter__(self) -> "StorageAnchors":
        try:
            self.home = self.stack.enter_context(HeldDirectory(self.layout.home))
            self.lock = self.stack.enter_context(HeldRegularFile(
                self.home, LOCK_NAME, create=True, writable=True,
            ))
            self.lock.lock_exclusive()
            self.stack.callback(self.lock.unlock)
            self._open_chain()
            if self.publish_requested:
                self.publish_outer()
                self.ensure_roots()
            elif self.include_roots:
                self.ensure_roots()
            return self
        except BaseException:
            self.stack.__exit__(*sys.exc_info())
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.lock.verify()
            outer = _read_marker(self.home, OUTER_MARKER_NAME)
            if not self.outer_missing:
                _expect_exact(outer, self.outer_payload, self.layout.outer_marker_path)
        self.stack.__exit__(exc_type, exc, tb)

    def _open_chain(self) -> None:
        outer = _read_marker(self.home, OUTER_MARKER_NAME)
        if outer is not None:
            self.plugin_data = self.stack.enter_context(
                _require_child(self.home, "plugin-data")
            )
            self.registry = self.stack.enter_context(
                _require_child(self.home, REGISTRY_NAME)
            )
            profile_id = outer.get("profile_id")
            try:
                parsed_profile_id = uuid.UUID(str(profile_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise WorkspacePathError("workspace registry profile identity is malformed") from exc
            if str(parsed_profile_id) != profile_id:
                raise WorkspacePathError("workspace registry profile identity is malformed")
            self.layout.profile_key = str(profile_id)
            self.outer_payload = self._outer_payload()
            _expect_exact(outer, self.outer_payload, self.layout.outer_marker_path)
            self.outer_missing = False
            binding = _read_marker(self.registry, self.layout.registry_binding_path.name)
        else:
            self.plugin_data = self.stack.enter_context(
                _ensure_child(self.home, "plugin-data")
            )
            marker_names = [
                name for name in self.plugin_data.list_names()
                if name.startswith(".workspace-namespace-")
                or name.startswith("..workspace-namespace-")
            ]
            if marker_names:
                raise WorkspacePathError(
                    "workspace namespace marker exists without its outer registry binding"
                )
            if self.plugin_data.exists(self.layout.plugin_namespace):
                self.data = self.stack.enter_context(
                    _require_child(self.plugin_data, self.layout.plugin_namespace)
                )
                self._reject_legacy_database()
            else:
                self.data = None  # type: ignore[assignment]
            registry_opener = _require_child if self.home.exists(REGISTRY_NAME) else _ensure_child
            self.registry = self.stack.enter_context(registry_opener(self.home, REGISTRY_NAME))
            if self.registry.list_names():
                raise WorkspacePathError(
                    "workspace plugin state exists without its outer registry binding"
                )
            if self.data is None:
                self.data = self.stack.enter_context(
                    _ensure_child(self.plugin_data, self.layout.plugin_namespace)
                )
            self.outer_missing = True
            self.layout.profile_key = str(uuid.uuid4())
            self.outer_payload = self._outer_payload()
            _publish_marker(self.home, OUTER_MARKER_NAME, self.outer_payload)
            self.outer_missing = False
            binding = _read_marker(self.registry, self.layout.registry_binding_path.name)

        inner = _read_marker(self.plugin_data, self.layout.inner_marker_path.name)
        if inner is not None and binding is None:
            raise WorkspacePathError(
                "workspace namespace marker has no durable registry binding"
            )
        if binding is not None:
            self._validate_binding_shape(binding)
            self.data = self.stack.enter_context(
                _require_child(self.plugin_data, self.layout.plugin_namespace)
            )
            self.registry_namespace = self.stack.enter_context(
                _require_child(self.registry, self.layout.registry_dir.name)
            )
        else:
            if outer is not None:
                self.data = self.stack.enter_context(
                    _ensure_child(self.plugin_data, self.layout.plugin_namespace)
                )
                self._reject_legacy_database()
            if self.registry.exists(self.layout.registry_dir.name):
                candidate = self.stack.enter_context(
                    _require_child(self.registry, self.layout.registry_dir.name)
                )
                self.unbound_registry_names = candidate.list_names()
                self.registry_namespace = candidate
            else:
                self.registry_namespace = self.stack.enter_context(
                    _ensure_child(self.registry, self.layout.registry_dir.name)
                )
        self._reject_legacy_database()
        if binding is not None and not self._binding_matches_static(binding):
            raise WorkspacePathError(
                "workspace registry plugin binding conflicts with canonical storage identities"
            )
        _expect_exact(inner, binding, self.layout.inner_marker_path)
        self.inner_missing = inner is None
        self.binding_payload = dict(binding) if binding is not None else None
        self.binding_missing = binding is None
        if not self.outer_missing and not self.binding_missing and not self.inner_missing:
            self._secure_directories()

    def _binding_matches_static(self, binding: Mapping[str, Any]) -> bool:
        static = {
            "kind": "plugin-workspace-binding", "version": 1,
            "profile_key": self.layout.profile_key,
            "plugin_namespace": self.layout.plugin_namespace,
            "plugin_identity": self.layout.plugin_identity,
            "data_identity": self.data.identity_json(),
            "registry_namespace_identity": self.registry_namespace.identity_json(),
        }
        return all(binding.get(key) == value for key, value in static.items())

    @staticmethod
    def _validate_binding_shape(binding: Mapping[str, Any]) -> None:
        expected_keys = {
            "kind", "version", "profile_key", "plugin_namespace", "plugin_identity",
            "data_identity", "registry_namespace_identity", "database_identity",
            "workspaces_identity", "quarantine_identity",
        }
        if set(binding) != expected_keys:
            raise WorkspacePathError("workspace registry plugin binding is malformed")
        for key in ("database_identity", "workspaces_identity", "quarantine_identity"):
            value = binding.get(key)
            if not isinstance(value, list) or not all(isinstance(part, int) for part in value):
                raise WorkspacePathError("workspace registry plugin binding is malformed")

    def _outer_payload(self) -> dict[str, Any]:
        return {
            "kind": "plugin-workspace-roots", "version": 1,
            "profile_id": self.layout.profile_key,
            "lock_identity": [int(part) for part in self.lock.identity],
            "plugin_data_identity": self.plugin_data.identity_json(),
            "registry_identity": self.registry.identity_json(),
        }

    def _secure_directories(self) -> None:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            for directory in (
                self.plugin_data, self.data, self.registry, self.registry_namespace,
                self.workspaces, self.quarantine,
            ):
                if directory is not None:
                    directory.harden_security()
        else:
            self.home.chmod("plugin-data", 0o700)
            self.home.chmod(REGISTRY_NAME, 0o700)
            self.plugin_data.chmod(self.layout.plugin_namespace, 0o700)
            self.registry.chmod(self.layout.registry_dir.name, 0o700)
            if self.workspaces is not None and self.quarantine is not None:
                self.data.chmod("workspaces", 0o700)
                self.data.chmod("workspace-quarantine", 0o700)

    def _reject_legacy_database(self) -> None:
        found = [name for name in LEGACY_DATABASE_NAMES if self.data.exists(name)]
        if found:
            raise WorkspacePathError(
                "legacy workspace lease database state requires offline operator migration; "
                f"refusing to open while present: {', '.join(found)}"
            )

    def publish_outer(self) -> None:
        if self.outer_missing:
            _publish_marker(self.home, OUTER_MARKER_NAME, self.outer_payload)
            self.outer_missing = False

    def ensure_roots(self) -> None:
        if self.outer_missing:
            raise WorkspacePathError("workspace registry outer marker is not initialized")
        if self.workspaces is None:
            opener = _ensure_child if self.binding_missing else _require_child
            self.workspaces = self.stack.enter_context(opener(self.data, "workspaces"))
            self.quarantine = self.stack.enter_context(opener(self.data, "workspace-quarantine"))
            if self.binding_payload is not None and (
                self.binding_payload["workspaces_identity"] != self.workspaces.identity_json()
                or self.binding_payload["quarantine_identity"] != self.quarantine.identity_json()
            ):
                raise WorkspacePathError("workspace registry bound root identity changed")
            if not self.binding_missing:
                self._secure_directories()

    def expected_database_identity(self) -> list[int] | None:
        if self.binding_payload is None:
            return None
        return list(self.binding_payload["database_identity"])

    def commit_binding(self, database_identity: list[int]) -> None:
        if not self.binding_missing:
            if self.expected_database_identity() != database_identity:
                raise WorkspacePathError("workspace registry database identity changed")
            if self.inner_missing:
                _publish_marker(
                    self.plugin_data, self.layout.inner_marker_path.name,
                    self.binding_payload or {},
                )
                self.inner_missing = False
            return
        if self.workspaces is None or self.quarantine is None:
            raise WorkspacePathError("workspace roots are not held")
        payload = {
            "kind": "plugin-workspace-binding", "version": 1,
            "profile_key": self.layout.profile_key,
            "plugin_namespace": self.layout.plugin_namespace,
            "plugin_identity": self.layout.plugin_identity,
            "data_identity": self.data.identity_json(),
            "registry_namespace_identity": self.registry_namespace.identity_json(),
            "database_identity": list(database_identity),
            "workspaces_identity": self.workspaces.identity_json(),
            "quarantine_identity": self.quarantine.identity_json(),
        }
        _publish_marker(self.registry, self.layout.registry_binding_path.name, payload)
        self.binding_payload = payload
        self.binding_missing = False
        _publish_marker(self.plugin_data, self.layout.inner_marker_path.name, payload)
        self.inner_missing = False
        self._secure_directories()
        self.markers_published = True

    def identities(self) -> dict[str, Any]:
        if self.workspaces is None or self.quarantine is None:
            raise WorkspacePathError("workspace roots are not held")
        return {
            "version": 2,
            "home": self.home.identity_json(),
            "plugin_data": self.plugin_data.identity_json(),
            "registry": self.registry.identity_json(),
            "data": self.data.identity_json(),
            "registry_namespace": self.registry_namespace.identity_json(),
            "workspaces": self.workspaces.identity_json(),
            "quarantine": self.quarantine.identity_json(),
        }


def open_storage_anchors(
    layout: Any, *, include_roots: bool = True, publish: bool = False,
) -> StorageAnchors:
    return StorageAnchors(layout, include_roots=include_roots, publish=publish)


_NONPORTABLE_ROOT_NAMES = frozenset({REGISTRY_NAME, OUTER_MARKER_NAME, LOCK_NAME})
_NONPORTABLE_WORKSPACE_DIRS = frozenset({"workspaces", "workspace-quarantine"})


def is_workspace_runtime_path(relative_path: str | Path) -> bool:
    """Whether a profile-relative path is non-portable workspace ownership state."""
    parts = Path(relative_path).parts
    if len(parts) >= 3 and parts[0] == "profiles":
        parts = parts[2:]
    if not parts:
        return False
    if parts[0] in _NONPORTABLE_ROOT_NAMES or (
        parts[0].startswith("..plugin-workspace-roots-v1.json.")
        or parts[0].startswith("..plugin-workspace-roots.lock.")
    ):
        return True
    if parts[0] != "plugin-data" or len(parts) < 2:
        return False
    if len(parts) == 2 and (
        parts[1].startswith(".workspace-namespace-")
        or parts[1].startswith("..workspace-namespace-")
    ):
        return ".json" in parts[1]
    if len(parts) < 3:
        return False
    return parts[2] in _NONPORTABLE_WORKSPACE_DIRS or parts[2] in LEGACY_DATABASE_NAMES


def strip_workspace_runtime_tree(profile_root: Path) -> list[str]:
    """Remove non-portable lease artifacts from a staged clone/import tree."""
    import shutil

    removed: list[str] = []
    for directory, dirnames, filenames in os.walk(profile_root, topdown=True, followlinks=False):
        parent = Path(directory)
        entries = [*dirnames, *filenames]
        for name in entries:
            candidate = parent / name
            relative = candidate.relative_to(profile_root)
            if not is_workspace_runtime_path(relative):
                continue
            if name in dirnames:
                dirnames.remove(name)
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                candidate.unlink(missing_ok=True)
            elif os.name == "nt":  # pragma: no cover - exercised on Windows CI
                handle = None
                try:
                    handle, _identity = __import__(
                        "hermes_cli.plugin_workspace_fs", fromlist=["windows_hold_directory"],
                    ).windows_hold_directory(candidate)
                except WorkspacePathError:
                    os.rmdir(candidate)  # removes a junction itself, never its target
                else:
                    __import__(
                        "hermes_cli.plugin_workspace_fs", fromlist=["windows_kernel32"],
                    ).windows_kernel32().CloseHandle(handle)
                    shutil.rmtree(candidate)
            else:
                shutil.rmtree(candidate)
            removed.append(str(relative))
    return removed
