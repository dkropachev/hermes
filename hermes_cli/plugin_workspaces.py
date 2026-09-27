"""Durable, profile-scoped workspace leases for trusted plugins.

The handle is a bearer capability, not a path.  Only a SHA-256 digest is
persisted; the plugin must keep the serializable handle if it wants to renew,
reconnect, inspect, or release a lease after a host restart.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import socket
import stat
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home, hermes_home_key, mkdir_under_hermes_home
from hermes_cli.plugins_manifest import _portable_skill_namespace
from hermes_cli.process_identity import _pid_alive_matches, _process_create_time
from hermes_cli.sqlite_util import open_db, transaction


HOST_FEATURE = "workspace_leases.v1"
HANDLE_VERSION = 1
DEFAULT_TTL_SECONDS = 300.0
MIN_TTL_SECONDS = 1.0
MAX_TTL_SECONDS = 24 * 60 * 60.0

_WORKSPACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PLUGIN_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_WORKSPACE_LOCKS: dict[str, threading.RLock] = {}
_WORKSPACE_LOCKS_GUARD = threading.Lock()


class WorkspaceLeaseError(RuntimeError):
    """Base class for workspace lifecycle failures."""


class InvalidWorkspaceHandleError(WorkspaceLeaseError):
    """The handle is malformed, stale, released, or belongs to another scope."""


class WorkspaceInUseError(WorkspaceLeaseError):
    """A live or not-yet-expired owner already holds the workspace."""


class WorkspaceLeaseExpiredError(InvalidWorkspaceHandleError):
    """The lease heartbeat expired; reconnect is required before further use."""


class WorkspaceOwnershipError(WorkspaceLeaseError):
    """A different live process owns the lease."""


class WorkspacePathError(WorkspaceLeaseError):
    """The workspace layout cannot be proven safe and canonical."""


class WorkspaceDurabilityError(WorkspacePathError):
    """A filesystem mutation completed, but its metadata could not be durably flushed."""

    def __init__(self, message: str, *, mutation_completed: bool) -> None:
        super().__init__(message)
        self.mutation_completed = mutation_completed


@dataclass(frozen=True)
class _Layout:
    home: Path
    profile_key: str
    plugin_namespace: str
    plugin_identity: str
    data_dir: Path
    workspaces_dir: Path
    quarantine_dir: Path
    db_path: Path


def _native_hashed_namespace(plugin_id: str) -> str:
    slug = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "_-") else "-"
        for ch in plugin_id.casefold()
    ).strip("-_") or "plugin"
    digest = hashlib.sha256(plugin_id.encode("utf-8")).hexdigest()[:12]
    return f"hermes-native-{slug[:96]}-{digest}"


def _plugin_namespace(plugin_id: str, skill_namespace: str) -> str:
    if skill_namespace:
        # Portable namespaces are host-generated, collision-resistant, and intentionally use the
        # reserved agent-plugin-* family. Malformed callers get a freshly generated portable name.
        return (
            skill_namespace
            if skill_namespace.startswith("agent-plugin-")
            and _PLUGIN_NAMESPACE_RE.fullmatch(skill_namespace)
            and ".." not in skill_namespace
            and not skill_namespace.endswith(".")
            else _portable_skill_namespace(plugin_id)
        )
    candidate = plugin_id
    folded = candidate.casefold()
    if (
        candidate == folded
        and _PLUGIN_NAMESPACE_RE.fullmatch(candidate)
        and ".." not in candidate
        and not candidate.endswith(".")
        and candidate.split(".", 1)[0] not in _WINDOWS_RESERVED
        and not candidate.startswith(("agent-plugin-", "hermes-native-"))
    ):
        return candidate
    return _native_hashed_namespace(candidate)


def _plugin_identity(plugin_id: str, skill_namespace: str) -> str:
    kind = "portable" if skill_namespace else "native"
    material = json.dumps(
        [kind, plugin_id, skill_namespace], ensure_ascii=True, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _validated_workspace_id(workspace_id: str) -> str:
    if (
        not isinstance(workspace_id, str)
        or not _WORKSPACE_ID_RE.fullmatch(workspace_id)
        or ".." in workspace_id
        or workspace_id.endswith(".")
        or workspace_id.split(".", 1)[0] in _WINDOWS_RESERVED
    ):
        raise ValueError(
            "workspace_id must be 1-128 lowercase ASCII letters, numbers, '.', '_', or '-' "
            "(without '..', a trailing '.', or a reserved device name)"
        )
    return workspace_id


def _ttl(value: Any) -> float:
    try:
        ttl = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ttl_seconds must be a finite number from 1 through 86400") from exc
    if not math.isfinite(ttl) or not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
        raise ValueError("ttl_seconds must be a finite number from 1 through 86400")
    return ttl


def _intent() -> dict[str, Any]:
    return {
        "contract_version": HANDLE_VERSION,
        "operation_id": str(uuid.uuid4()),
        "capability": secrets.token_urlsafe(32),
    }


def _parse_intent(intent: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(intent, Mapping) or set(intent) != {
        "contract_version", "operation_id", "capability",
    }:
        raise InvalidWorkspaceHandleError("malformed workspace operation intent")
    if intent.get("contract_version") != HANDLE_VERSION:
        raise InvalidWorkspaceHandleError("unsupported workspace operation intent version")
    operation_id, capability = intent.get("operation_id"), intent.get("capability")
    try:
        parsed = uuid.UUID(str(operation_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidWorkspaceHandleError("malformed workspace operation intent") from exc
    if str(parsed) != str(operation_id) or not isinstance(capability, str) or not 32 <= len(
        capability
    ) <= 256:
        raise InvalidWorkspaceHandleError("malformed workspace operation intent")
    return str(operation_id), capability


@contextmanager
def _workspace_lock(layout: _Layout, workspace_id: str):
    key = os.path.normcase(str(layout.workspaces_dir / workspace_id))
    with _WORKSPACE_LOCKS_GUARD:
        lock = _WORKSPACE_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def _strict_sync_directory(path: Path) -> None:
    """Durably flush one directory or raise; unlike ``utils.fsync_directory``, never best-effort."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        flush = kernel32.FlushFileBuffers
        flush.argtypes = (wintypes.HANDLE,)
        flush.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        handle = create_file(
            str(path), 0x40000000, 0x00000001 | 0x00000002 | 0x00000004,
            None, 3, 0x02000000 | 0x80000000, None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        error = None
        try:
            if not flush(handle):
                error = ctypes.WinError(ctypes.get_last_error())
        finally:
            if not close(handle) and error is None:
                error = ctypes.WinError(ctypes.get_last_error())
        if error is not None:
            raise error
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_dirs(*paths: Path) -> None:
    for path in dict.fromkeys(paths):
        _strict_sync_directory(path)


def _strict_replace(source: Path, target: Path, *sync_dirs: Path) -> None:
    """Rename with write-through semantics, then strictly flush affected directories."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel32.MoveFileExW
        move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move.restype = ctypes.c_int
        if not move(str(source), str(target), 0x1 | 0x8):  # REPLACE_EXISTING | WRITE_THROUGH
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)
    try:
        _sync_dirs(*sync_dirs)
    except OSError as exc:
        raise WorkspaceDurabilityError(
            f"filesystem rename completed but metadata flush failed: {exc}",
            mutation_completed=True,
        ) from exc


def _safe_child(parent: Path, name: str) -> Path:
    """Create one host-owned directory component and reject aliases/junctions/symlinks."""
    candidate = parent / name
    try:
        candidate.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise WorkspacePathError(f"cannot create workspace directory {candidate}: {exc}") from exc
    try:
        info = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkspacePathError(f"cannot validate workspace directory {candidate}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspacePathError(f"workspace directory is not a regular directory: {candidate}")
    if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate.absolute())):
        raise WorkspacePathError(f"workspace directory resolves through an alias: {candidate}")
    if os.name != "nt":
        try:
            os.chmod(candidate, 0o700)
        except OSError as exc:
            raise WorkspacePathError(f"cannot secure workspace directory {candidate}: {exc}") from exc
    return candidate


def _layout(plugin_id: str, skill_namespace: str, home_path: Path | None = None) -> _Layout:
    raw_home = Path(home_path if home_path is not None else get_hermes_home()).expanduser()
    mkdir_under_hermes_home(raw_home)
    try:
        home = raw_home.resolve(strict=True)
    except OSError as exc:
        raise WorkspacePathError(f"cannot resolve HERMES_HOME {raw_home}: {exc}") from exc
    namespace = _plugin_namespace(plugin_id, skill_namespace)
    identity = _plugin_identity(plugin_id, skill_namespace)
    plugin_data = _safe_child(home, "plugin-data")
    data_dir = _safe_child(plugin_data, namespace)
    workspaces = _safe_child(data_dir, "workspaces")
    quarantine = _safe_child(data_dir, "workspace-quarantine")
    return _Layout(
        home=home,
        profile_key=hermes_home_key(home),
        plugin_namespace=namespace,
        plugin_identity=identity,
        data_dir=data_dir,
        workspaces_dir=workspaces,
        quarantine_dir=quarantine,
        db_path=data_dir / "workspace-leases.db",
    )


def _initialize(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS workspace_leases (
            workspace_id TEXT PRIMARY KEY,
            lease_id TEXT NOT NULL UNIQUE,
            capability_hash TEXT NOT NULL,
            contract_version INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('preparing', 'active', 'releasing', 'released')),
            plugin_namespace TEXT NOT NULL,
            plugin_identity TEXT NOT NULL,
            profile_key TEXT NOT NULL,
            workspace_path TEXT NOT NULL,
            owner_pid INTEGER NOT NULL,
            owner_create_time REAL,
            owner_host TEXT NOT NULL,
            owner_instance TEXT NOT NULL,
            ttl_seconds REAL NOT NULL,
            acquired_at REAL NOT NULL,
            heartbeat_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            heartbeat_monotonic REAL,
            expires_monotonic REAL,
            expiry_observer TEXT,
            expiry_observed_monotonic REAL,
            released_at REAL,
            generation INTEGER NOT NULL,
            acquire_intent_id TEXT,
            cleanup_json TEXT,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace_lease_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL,
            lease_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at REAL NOT NULL,
            actor_pid INTEGER NOT NULL,
            actor_create_time REAL,
            details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS workspace_lease_events_lookup
            ON workspace_lease_events(workspace_id, event_id);
        CREATE TABLE IF NOT EXISTS workspace_cleanup_receipts (
            receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL,
            lease_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            operation TEXT NOT NULL CHECK (operation IN ('acquire', 'release', 'recovery')),
            phase TEXT NOT NULL,
            recorded_at REAL NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS workspace_cleanup_receipts_lookup
            ON workspace_cleanup_receipts(workspace_id, receipt_id);
        CREATE TABLE IF NOT EXISTS workspace_lease_operations (
            operation_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('acquire', 'reconnect')),
            workspace_id TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            input_lease_id TEXT,
            input_capability_hash TEXT,
            output_lease_id TEXT NOT NULL,
            output_capability_hash TEXT NOT NULL,
            generation INTEGER NOT NULL,
            ttl_seconds REAL NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('preparing', 'committed', 'failed', 'superseded')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(workspace_leases)")}
    if "plugin_identity" not in columns:
        conn.execute("ALTER TABLE workspace_leases ADD COLUMN plugin_identity TEXT")
    if "acquire_intent_id" not in columns:
        conn.execute("ALTER TABLE workspace_leases ADD COLUMN acquire_intent_id TEXT")
    for column in (
        "heartbeat_monotonic", "expires_monotonic", "expiry_observed_monotonic",
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE workspace_leases ADD COLUMN {column} REAL")
    if "expiry_observer" not in columns:
        conn.execute("ALTER TABLE workspace_leases ADD COLUMN expiry_observer TEXT")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS workspace_lease_acquire_intent
           ON workspace_leases(acquire_intent_id)"""
    )


def _connect(layout: _Layout):
    sidecars = (
        layout.db_path,
        Path(str(layout.db_path) + "-wal"),
        Path(str(layout.db_path) + "-shm"),
    )
    if any(path.is_symlink() for path in sidecars):
        raise WorkspacePathError(f"workspace lease database files cannot be symlinks: {layout.db_path}")
    conn = open_db(
        layout.db_path,
        db_label=f"plugin-data/{layout.plugin_namespace}/workspace-leases.db",
        foreign_keys=True,
        synchronous_full=True,
        wal_lock_retries=5,
        initialize=_initialize,
    )
    try:
        if layout.db_path.is_symlink() or layout.db_path.resolve(strict=True).parent != layout.data_dir:
            raise WorkspacePathError("workspace lease database resolved outside its plugin namespace")
        if os.name != "nt":
            for path in sidecars:
                if path.exists():
                    if path.is_symlink() or not path.is_file():
                        raise WorkspacePathError(
                            f"workspace lease database sidecar is unsafe: {path}"
                        )
                    os.chmod(path, 0o600)
    except BaseException:
        conn.close()
        raise
    return conn


def _host_instance() -> str:
    """Stable OS boot witness where available; never synthesize one from wall-clock time."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if boot_id:
            return f"boot-id:{boot_id}"
    except OSError:
        pass
    return "unverified"


def _owner_stamp() -> tuple[int, float | None, str, str]:
    return os.getpid(), _process_create_time(), socket.gethostname(), _host_instance()


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _owner_status(row: Mapping[str, Any]) -> str:
    """Return ``self``, ``live``, ``dead``, or ``unknown`` for a persisted owner."""
    try:
        pid = int(row["owner_pid"])
    except (KeyError, TypeError, ValueError):
        return "dead"
    if pid <= 0:
        return "dead"
    if str(_row_value(row, "owner_host") or "") != socket.gethostname():
        return "unknown"
    current_instance = _host_instance()
    recorded_instance = str(_row_value(row, "owner_instance") or "unverified")
    if (
        recorded_instance != "unverified"
        and current_instance != "unverified"
        and current_instance != recorded_instance
    ):
        return "dead"
    alive = _pid_alive_matches(pid, _row_value(row, "owner_create_time"))
    if alive is False:
        return "dead"
    if alive is None:
        return "unknown"
    current_pid, current_create, _host, _instance = _owner_stamp()
    if pid != current_pid:
        return "live"
    if current_create is None or _row_value(row, "owner_create_time") is None:
        return "self"
    return (
        "self"
        if abs(float(current_create) - float(row["owner_create_time"])) < 2.0
        else "dead"
    )


def _owner_state(row: Mapping[str, Any]) -> bool | None:
    """Compatibility-shaped owner result used by lifecycle decisions."""
    status = _owner_status(row)
    return True if status == "self" else False if status == "dead" else None


def _verified_instance(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value != "unverified"


def _expiry_observer_token() -> str:
    host, instance = socket.gethostname(), _host_instance()
    if _verified_instance(instance):
        return f"boot:{host}:{instance}"
    started = _process_create_time()
    return f"process:{host}:{os.getpid()}:{started if started is not None else 'unknown'}"


def _fresh_expiry(ttl: float) -> tuple[float, float, float, float, str, float]:
    wall, monotonic = time.time(), time.monotonic()
    return (
        wall, wall + ttl, monotonic, monotonic + ttl,
        _expiry_observer_token(), monotonic,
    )


def _lease_expired(conn, row: Mapping[str, Any]) -> bool:
    """Canonical expiry decision: same-boot monotonic, otherwise one full local TTL observation."""
    current_host, current_instance = socket.gethostname(), _host_instance()
    owner_host = str(_row_value(row, "owner_host") or "")
    owner_instance = str(_row_value(row, "owner_instance") or "unverified")
    now = time.monotonic()
    if (
        owner_host == current_host
        and _verified_instance(owner_instance)
        and _verified_instance(current_instance)
    ):
        if owner_instance != current_instance:
            return True
        deadline = _row_value(row, "expires_monotonic")
        if isinstance(deadline, (int, float)) and math.isfinite(float(deadline)):
            return now >= float(deadline)

    # A wall-clock-expired read never grants authority. Foreign hosts, unverified boot identity,
    # and legacy rows must remain continuously observed by this local boot/process for a full TTL.
    observer = _expiry_observer_token()
    recorded_observer = _row_value(row, "expiry_observer")
    observed_at = _row_value(row, "expiry_observed_monotonic")
    if (
        recorded_observer != observer
        or not isinstance(observed_at, (int, float))
        or not math.isfinite(float(observed_at))
        or now < float(observed_at)
    ):
        conn.execute(
            """UPDATE workspace_leases SET expiry_observer=?, expiry_observed_monotonic=?
               WHERE lease_id=? AND generation=?""",
            (observer, now, row["lease_id"], row["generation"]),
        )
        return False
    return now - float(observed_at) >= float(row["ttl_seconds"])


def _handle(lease_id: str, capability: str) -> dict[str, Any]:
    return {"contract_version": HANDLE_VERSION, "lease_id": lease_id, "capability": capability}


def _response_handle(lease_id: str, capability: str) -> dict[str, Any]:
    """Response boundary kept separate so crash-after-commit behavior is fault-testable."""
    return _handle(lease_id, capability)


def _parse_handle(handle: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(handle, Mapping) or set(handle) != {"contract_version", "lease_id", "capability"}:
        raise InvalidWorkspaceHandleError("malformed workspace lease handle")
    if handle.get("contract_version") != HANDLE_VERSION:
        raise InvalidWorkspaceHandleError("unsupported workspace lease handle version")
    lease_id, capability = handle.get("lease_id"), handle.get("capability")
    try:
        uuid.UUID(str(lease_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidWorkspaceHandleError("malformed workspace lease handle") from exc
    if not isinstance(capability, str) or not 32 <= len(capability) <= 256:
        raise InvalidWorkspaceHandleError("malformed workspace lease handle")
    return str(lease_id), capability


def _capability_hash(capability: str) -> str:
    return hashlib.sha256(capability.encode("utf-8")).hexdigest()


def _operation_fingerprint(
    layout: _Layout, kind: str, workspace_id: str, ttl: float,
    input_lease_id: str | None = None,
) -> str:
    encoded = json.dumps(
        [
            kind, layout.plugin_identity, layout.profile_key, workspace_id,
            float(ttl), input_lease_id,
        ],
        ensure_ascii=True, separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_operation(
    row: Mapping[str, Any], *, kind: str, workspace_id: str, fingerprint: str,
    output_capability_hash: str, input_lease_id: str | None = None,
    input_capability_hash: str | None = None,
) -> None:
    valid = (
        row["kind"] == kind
        and row["workspace_id"] == workspace_id
        and hmac.compare_digest(row["request_fingerprint"], fingerprint)
        and hmac.compare_digest(row["output_capability_hash"], output_capability_hash)
        and row["input_lease_id"] == input_lease_id
    )
    if input_capability_hash is not None:
        valid = valid and row["input_capability_hash"] is not None and hmac.compare_digest(
            row["input_capability_hash"], input_capability_hash,
        )
    if not valid:
        raise InvalidWorkspaceHandleError("workspace operation intent does not match its request")


def _supersede_lease_operations(conn, lease_id: str, now: float) -> None:
    conn.execute(
        """UPDATE workspace_lease_operations SET state='superseded', updated_at=?
           WHERE output_lease_id=? AND state IN ('preparing', 'committed', 'failed')""",
        (now, lease_id),
    )


def _validated_row(
    conn, layout: _Layout, handle: Mapping[str, Any], *, validate_path: bool = True,
):
    lease_id, capability = _parse_handle(handle)
    row = conn.execute("SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)).fetchone()
    if row is None or not hmac.compare_digest(str(row["capability_hash"]), _capability_hash(capability)):
        raise InvalidWorkspaceHandleError("workspace lease handle is invalid or stale")
    if (
        row["plugin_namespace"] != layout.plugin_namespace
        or row["plugin_identity"] != layout.plugin_identity
        or row["profile_key"] != layout.profile_key
    ):
        raise InvalidWorkspaceHandleError("workspace lease handle belongs to another plugin or profile")
    expected = layout.workspaces_dir / str(row["workspace_id"])
    if os.path.normcase(str(expected)) != os.path.normcase(str(row["workspace_path"])):
        raise InvalidWorkspaceHandleError("workspace lease has an invalid persisted path")
    if validate_path:
        _validate_workspace_path(expected)
    return row


def _validate_workspace_path(expected: Path) -> None:
    try:
        info = expected.lstat()
        resolved = expected.resolve(strict=True)
    except OSError as exc:
        raise WorkspacePathError(f"workspace path is unavailable: {expected}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspacePathError(f"workspace path is not a regular directory: {expected}")
    if os.path.normcase(str(resolved)) != os.path.normcase(str(expected.absolute())):
        raise WorkspacePathError(f"workspace path resolves through an alias: {expected}")


def _event(conn, row: Mapping[str, Any], event_type: str, details: Mapping[str, Any] | None = None) -> None:
    pid, created, _host, _instance = _owner_stamp()
    conn.execute(
        """INSERT INTO workspace_lease_events
           (workspace_id, lease_id, generation, event_type, occurred_at, actor_pid,
            actor_create_time, details_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["workspace_id"], row["lease_id"], row["generation"], event_type,
            time.time(), pid, created,
            json.dumps(dict(details or {}), sort_keys=True, separators=(",", ":")),
        ),
    )


def _cleanup_receipt(
    conn, row: Mapping[str, Any], operation: str, phase: str,
    details: Mapping[str, Any],
) -> None:
    """Append one immutable cleanup fact; rows survive successor lease replacement."""
    conn.execute(
        """INSERT INTO workspace_cleanup_receipts
           (workspace_id, lease_id, generation, operation, phase, recorded_at, details_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            row["workspace_id"], row["lease_id"], row["generation"], operation, phase,
            time.time(), json.dumps(dict(details), sort_keys=True, separators=(",", ":")),
        ),
    )


def _cleanup_history(conn, workspace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT lease_id, generation, operation, phase, recorded_at, details_json
           FROM workspace_cleanup_receipts WHERE workspace_id=?
           ORDER BY receipt_id DESC LIMIT 100""",
        (workspace_id,),
    ).fetchall()
    return [
        {
            "leaseId": row["lease_id"], "generation": int(row["generation"]),
            "operation": row["operation"], "phase": row["phase"],
            "at": float(row["recorded_at"]), "details": json.loads(row["details_json"]),
        }
        for row in reversed(rows)
    ]


def _event_history(conn, workspace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT lease_id, generation, event_type, occurred_at, actor_pid,
                  actor_create_time, details_json
           FROM workspace_lease_events WHERE workspace_id=?
           ORDER BY event_id DESC LIMIT 50""",
        (workspace_id,),
    ).fetchall()
    return [
        {
            "type": row["event_type"], "at": float(row["occurred_at"]),
            "leaseId": row["lease_id"], "generation": int(row["generation"]),
            "actorPid": int(row["actor_pid"]), "actorCreateTime": row["actor_create_time"],
            "details": json.loads(row["details_json"]),
        }
        for row in reversed(rows)
    ]


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    allowed = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TMPDIR", "TEMP", "TMP")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"})
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(workspace), *args],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=5, env=env, check=False,
    )


def _classify_workspace(path: Path) -> dict[str, Any]:
    checked_at = time.time()
    if path.is_symlink():
        return {"classification": "symlink", "safe_to_delete": False, "checked_at": checked_at}
    if not path.exists():
        return {"classification": "missing", "safe_to_delete": True, "checked_at": checked_at}
    if not path.is_dir():
        return {"classification": "non_directory", "safe_to_delete": False, "checked_at": checked_at}
    try:
        if next(path.iterdir(), None) is None:
            return {"classification": "clean_empty", "safe_to_delete": True, "checked_at": checked_at}
        status = _git(
            path, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching",
        )
        if status.returncode != 0:
            return {
                "classification": "uncertain", "safe_to_delete": False,
                "checked_at": checked_at, "reason": "git_status_failed",
            }
        lines = [line for line in status.stdout.splitlines() if line]
        if lines:
            untracked = any(line.startswith("??") for line in lines)
            ignored = any(line.startswith("!!") for line in lines)
            tracked = any(not line.startswith(("??", "!!")) for line in lines)
            classification = "dirty" if tracked else "untracked" if untracked else "ignored"
            return {
                "classification": classification,
                "safe_to_delete": False, "checked_at": checked_at,
                "dirty": tracked, "untracked": untracked, "ignored": ignored,
                "changed_paths": len(lines),
            }
        contains = _git(path, "branch", "-r", "--contains", "HEAD", "--format=%(refname)")
        if contains.returncode == 0 and contains.stdout.strip():
            # A clean current tree says nothing about commits reachable only through another local
            # branch, tag, or reflog. Preserve every non-empty repository rather than attempting a
            # lossy whole-object-graph proof.
            return {"classification": "clean_git", "safe_to_delete": False, "checked_at": checked_at}
        head = _git(path, "rev-parse", "--verify", "HEAD")
        if head.returncode == 0:
            return {
                "classification": "unpushed", "safe_to_delete": False,
                "checked_at": checked_at, "head": head.stdout.strip()[:64],
            }
        return {
            "classification": "uncertain", "safe_to_delete": False,
            "checked_at": checked_at, "reason": "git_head_unverifiable",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "classification": "uncertain", "safe_to_delete": False,
            "checked_at": checked_at, "reason": type(exc).__name__,
        }


def _planned_detached_path(
    layout: _Layout, workspace_id: str, lease_id: str, generation: int, operation: str,
) -> Path:
    return layout.quarantine_dir / (
        f".{operation}-{workspace_id}-g{int(generation)}-{lease_id}"
    )


def _detach_workspace(
    layout: _Layout, workspace_id: str, lease_id: str, generation: int,
    operation: str, *, planned: Path | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Atomically remove the leased name before DB unlock so a successor can never be cleaned."""
    path = layout.workspaces_dir / workspace_id
    detached = planned or _planned_detached_path(
        layout, workspace_id, lease_id, generation, operation,
    )
    if not path.exists() and not path.is_symlink():
        if detached.exists() or detached.is_symlink():
            return detached, {
                "classification": "pending", "disposition": "detached",
                "original_path": str(path), "detached_path": str(detached),
            }
        return None, {"classification": "missing", "disposition": "absent", "original_path": str(path)}
    if detached.exists() or detached.is_symlink():
        return None, {
            "classification": "uncertain", "disposition": "preserved",
            "original_path": str(path), "detached_path": str(detached),
            "cleanup_error": "both canonical and planned detached paths exist",
        }
    try:
        _strict_replace(
            path, detached, layout.workspaces_dir, layout.quarantine_dir,
        )
    except OSError as exc:
        return None, {
            "classification": "uncertain", "disposition": "preserved",
            "original_path": str(path), "cleanup_error": f"{type(exc).__name__}: {exc}",
        }
    return detached, {
        "classification": "pending", "disposition": "detached",
        "original_path": str(path), "detached_path": str(detached),
    }


def _finish_detached_cleanup(detached: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    assessment = _classify_workspace(detached)
    finished = {**receipt, **assessment}
    try:
        if assessment["safe_to_delete"]:
            # The only existing safe-to-delete tree is empty. Atomic rmdir refuses a late file;
            # recursive deletion would race classification and destroy newly-created content.
            detached.rmdir()
            try:
                _sync_dirs(detached.parent)
            except OSError as exc:
                raise WorkspaceDurabilityError(
                    f"workspace removal completed but metadata flush failed: {exc}",
                    mutation_completed=True,
                ) from exc
            finished["disposition"] = "removed"
            finished.pop("detached_path", None)
        else:
            finished.update({"disposition": "quarantined", "quarantine_path": str(detached)})
            finished.pop("detached_path", None)
    except OSError as exc:
        if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            late = _classify_workspace(detached)
            finished.update({
                **late, "safe_to_delete": False, "disposition": "quarantined",
                "quarantine_path": str(detached), "cleanup_race": "late_content",
            })
            finished.pop("detached_path", None)
            return finished
        finished.update({
            "disposition": "preserved", "quarantine_path": str(detached),
            "cleanup_error": f"{type(exc).__name__}: {exc}",
        })
    return finished


def _pending_operation(row: Mapping[str, Any]) -> str | None:
    if row["state"] == "preparing":
        return "acquire"
    if row["state"] == "releasing":
        return "release"
    try:
        cleanup = json.loads(row["cleanup_json"] or "{}")
    except (TypeError, ValueError):
        return None
    operation = cleanup.get("operation")
    if row["state"] == "released":
        if operation == "release" and cleanup.get("disposition") in {"releasing", "detached"}:
            return "release"
        if operation == "acquire":
            return "acquire"
    return None


def _reconcile_pending_generation(conn, layout: _Layout, row: Mapping[str, Any]) -> None:
    """Preserve names left by a crashed transition before the current row is replaced."""
    operation = _pending_operation(row)
    if operation is None:
        return
    workspace_id = str(row["workspace_id"])
    lease_id = str(row["lease_id"])
    generation = int(row["generation"])
    canonical = layout.workspaces_dir / workspace_id
    planned = _planned_detached_path(
        layout, workspace_id, lease_id, generation, operation,
    )
    recovery_base = _planned_detached_path(
        layout, workspace_id, lease_id, generation, "recovery",
    )
    preserved: list[dict[str, Any]] = []

    def recovery_paths() -> list[Path]:
        candidates = [recovery_base]
        candidates.extend(sorted(recovery_base.parent.glob(f"{recovery_base.name}-*")))
        return [path for path in candidates if path.exists() or path.is_symlink()]

    def remember(source: str, path: Path) -> None:
        if not any(item.get("quarantine_path") == str(path) for item in preserved):
            preserved.append({
                "source": source, "disposition": "quarantined",
                "quarantine_path": str(path),
            })

    # A previous retry may have crashed after moving the canonical name to recovery but before its
    # receipt transaction committed. Inventory every deterministic recovery slot on every pass.
    for existing in recovery_paths():
        remember("recovery", existing)

    if canonical.exists() or canonical.is_symlink():
        target = planned
        if target.exists() or target.is_symlink():
            target = recovery_base
            suffix = 2
            while target.exists() or target.is_symlink():
                target = recovery_base.with_name(f"{recovery_base.name}-{suffix}")
                suffix += 1
        try:
            _strict_replace(
                canonical, target, layout.workspaces_dir, layout.quarantine_dir,
            )
            remember("canonical", target)
        except OSError as exc:
            raise WorkspacePathError(
                f"cannot preserve interrupted workspace generation at {canonical}: {exc}"
            ) from exc
    if planned.exists() or planned.is_symlink():
        remember("detached", planned)
    for existing in recovery_paths():
        remember("recovery", existing)
    if not preserved:
        preserved.append({"disposition": "absent"})
    _cleanup_receipt(
        conn, row, "recovery", "reconciled", {
            "interrupted_operation": operation, "preserved": preserved,
        },
    )


def _public_snapshot(conn, row: Mapping[str, Any]) -> dict[str, Any]:
    cleanup = json.loads(row["cleanup_json"]) if row["cleanup_json"] else None
    return {
        "contractVersion": int(row["contract_version"]),
        "leaseId": row["lease_id"],
        "workspaceId": row["workspace_id"],
        "path": row["workspace_path"],
        "state": row["state"],
        "generation": int(row["generation"]),
        "owner": {
            "pid": int(row["owner_pid"]), "createTime": row["owner_create_time"],
            "host": row["owner_host"], "instance": row["owner_instance"],
        },
        "acquiredAt": float(row["acquired_at"]),
        "heartbeatAt": float(row["heartbeat_at"]),
        "expiresAt": float(row["expires_at"]),
        "heartbeatMonotonic": row["heartbeat_monotonic"],
        "expiresMonotonic": row["expires_monotonic"],
        "releasedAt": row["released_at"],
        "ttlSeconds": float(row["ttl_seconds"]),
        "cleanup": cleanup,
        "cleanupReceipts": _cleanup_history(conn, row["workspace_id"]),
        "events": _event_history(conn, row["workspace_id"]),
    }


class PluginWorkspaces:
    """Workspace lifecycle facade bound to one plugin and the active profile per call."""

    def __init__(
        self, plugin_id: str, skill_namespace: str = "", *, home_path: Path | None = None,
    ) -> None:
        self._plugin_id = plugin_id
        self._skill_namespace = skill_namespace
        self._home_path = Path(home_path) if home_path is not None else Path(get_hermes_home())

    def _layout(self) -> _Layout:
        return _layout(self._plugin_id, self._skill_namespace, self._home_path)

    def new_intent(self) -> dict[str, Any]:
        """Return a caller-persisted intent for one retryable acquire or reconnect operation."""
        return _intent()

    def acquire(
        self, workspace_id: str, *, intent: Mapping[str, Any],
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        workspace_id, ttl = _validated_workspace_id(workspace_id), _ttl(ttl_seconds)
        operation_id, capability = _parse_intent(intent)
        layout = self._layout()
        with _workspace_lock(layout, workspace_id):
            return self._acquire_locked(
                layout, workspace_id, ttl, operation_id, capability,
            )

    def _acquire_locked(
        self, layout: _Layout, workspace_id: str, ttl: float,
        operation_id: str, capability: str,
    ) -> dict[str, Any]:
        capability_hash = _capability_hash(capability)
        request_fingerprint = _operation_fingerprint(
            layout, "acquire", workspace_id, ttl,
        )
        pid, created, host, instance = _owner_stamp()
        detached: Path | None = None
        preparation_error: WorkspacePathError | None = None
        reclaimed: str | None = None

        with transaction(_connect(layout), immediate=True) as conn:
            now, expires_wall, heartbeat_mono, expires_mono, observer, observed_mono = (
                _fresh_expiry(ttl)
            )
            operation = conn.execute(
                "SELECT * FROM workspace_lease_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if operation is not None:
                _validate_operation(
                    operation, kind="acquire", workspace_id=workspace_id,
                    fingerprint=request_fingerprint,
                    output_capability_hash=capability_hash,
                )
                if operation["state"] == "superseded":
                    raise InvalidWorkspaceHandleError("workspace acquire intent was superseded")
                intent_row = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?",
                    (operation["output_lease_id"],),
                ).fetchone()
                if intent_row is None or int(intent_row["generation"]) != int(
                    operation["generation"]
                ):
                    conn.execute(
                        """UPDATE workspace_lease_operations SET state='superseded', updated_at=?
                           WHERE operation_id=?""",
                        (now, operation_id),
                    )
                    raise InvalidWorkspaceHandleError("workspace acquire intent no longer owns a lease")
                if (
                    intent_row["workspace_id"] != workspace_id
                    or intent_row["plugin_namespace"] != layout.plugin_namespace
                    or intent_row["plugin_identity"] != layout.plugin_identity
                    or intent_row["profile_key"] != layout.profile_key
                    or not hmac.compare_digest(intent_row["capability_hash"], capability_hash)
                ):
                    raise InvalidWorkspaceHandleError("workspace acquire intent scope is invalid")
                state = intent_row["state"]
                owner_status = _owner_status(intent_row)
                expired = _lease_expired(conn, intent_row)
                if owner_status in {"live", "unknown"} and not expired:
                    raise WorkspaceInUseError(
                        f"workspace {workspace_id!r} intent is owned by another live process"
                    )
                if state == "active":
                    if operation["state"] != "committed":
                        raise InvalidWorkspaceHandleError(
                            "workspace acquire receipt is not committed"
                        )
                    _validate_workspace_path(Path(intent_row["workspace_path"]))
                    changed = conn.execute(
                        """UPDATE workspace_leases SET owner_pid=?, owner_create_time=?,
                           owner_host=?, owner_instance=?, heartbeat_at=?, expires_at=?,
                           heartbeat_monotonic=?, expires_monotonic=?, expiry_observer=?,
                           expiry_observed_monotonic=?, updated_at=?
                           WHERE lease_id=? AND generation=? AND state='active'
                           AND acquire_intent_id=?""",
                        (
                            pid, created, host, instance, now, expires_wall,
                            heartbeat_mono, expires_mono, observer, observed_mono, now,
                            intent_row["lease_id"], intent_row["generation"], operation_id,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise InvalidWorkspaceHandleError("workspace acquire replay lost its fence")
                    replayed = conn.execute(
                        "SELECT * FROM workspace_leases WHERE lease_id=?",
                        (intent_row["lease_id"],),
                    ).fetchone()
                    _event(conn, replayed, "acquire_replayed", {"operation_id": operation_id})
                    return _response_handle(replayed["lease_id"], capability)
                if state == "released":
                    cleanup = json.loads(intent_row["cleanup_json"] or "{}")
                    if cleanup.get("operation") != "acquire" or operation["state"] == "committed":
                        conn.execute(
                            """UPDATE workspace_lease_operations SET state='superseded', updated_at=?
                               WHERE operation_id=?""",
                            (now, operation_id),
                        )
                        raise InvalidWorkspaceHandleError("workspace acquire intent was already released")
                    _reconcile_pending_generation(conn, layout, intent_row)
                    generation = int(intent_row["generation"]) + 1
                    lease_id = str(uuid.uuid4())
                elif state == "preparing":
                    generation = int(intent_row["generation"])
                    lease_id = str(intent_row["lease_id"])
                    _reconcile_pending_generation(conn, layout, intent_row)
                else:
                    raise WorkspaceInUseError(
                        f"workspace {workspace_id!r} is in a release transition"
                    )
                reclaimed = "intent_retry"
                planned_detached = _planned_detached_path(
                    layout, workspace_id, lease_id, generation, "acquire",
                )
                cleanup = {
                    "operation": "acquire", "classification": "pending",
                    "disposition": "preparing",
                    "original_path": str(layout.workspaces_dir / workspace_id),
                    "planned_detached_path": str(planned_detached),
                }
                changed = conn.execute(
                    """UPDATE workspace_leases SET lease_id=?, state='preparing', owner_pid=?,
                       owner_create_time=?, owner_host=?, owner_instance=?, ttl_seconds=?,
                       heartbeat_at=?, expires_at=?, heartbeat_monotonic=?, expires_monotonic=?,
                       expiry_observer=?, expiry_observed_monotonic=?, released_at=NULL,
                       generation=?, cleanup_json=?, updated_at=?
                       WHERE workspace_id=? AND acquire_intent_id=?""",
                    (
                        lease_id, pid, created, host, instance, ttl, now, expires_wall,
                        heartbeat_mono, expires_mono, observer, observed_mono, generation,
                        json.dumps(cleanup, sort_keys=True), now, workspace_id, operation_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise InvalidWorkspaceHandleError("workspace acquire retry lost its fence")
                conn.execute(
                    """UPDATE workspace_lease_operations SET output_lease_id=?, generation=?,
                       state='preparing', updated_at=? WHERE operation_id=?""",
                    (lease_id, generation, now, operation_id),
                )
                row = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,),
                ).fetchone()
                _cleanup_receipt(conn, row, "acquire", "retry_planned", cleanup)
                _event(conn, row, "preparing", {"reclaimed": reclaimed, "cleanup": cleanup})
            else:
                old = conn.execute(
                    "SELECT * FROM workspace_leases WHERE workspace_id=?", (workspace_id,),
                ).fetchone()
                generation = 1
                if old is not None:
                    generation = int(old["generation"]) + 1
                    if (
                        old["plugin_namespace"] != layout.plugin_namespace
                        or old["plugin_identity"] != layout.plugin_identity
                        or old["profile_key"] != layout.profile_key
                    ):
                        raise WorkspacePathError(
                            "workspace lease database belongs to another plugin or profile"
                        )
                    if old["state"] in {"active", "preparing", "releasing"}:
                        owner_status = _owner_status(old)
                        expired = _lease_expired(conn, old)
                        live_transition = old["state"] in {"preparing", "releasing"}
                        if owner_status in {"self", "live"} and live_transition:
                            raise WorkspaceInUseError(
                                f"workspace {workspace_id!r} is being transitioned by a live owner"
                            )
                        if owner_status != "dead" and not expired:
                            raise WorkspaceInUseError(
                                f"workspace {workspace_id!r} is leased until {old['expires_at']}"
                            )
                        reclaimed = "owner_dead" if owner_status == "dead" else "ttl_expired"
                    _reconcile_pending_generation(conn, layout, old)
                    _supersede_lease_operations(conn, old["lease_id"], now)
                lease_id = str(uuid.uuid4())
                planned_detached = _planned_detached_path(
                    layout, workspace_id, lease_id, generation, "acquire",
                )
                cleanup = {
                    "operation": "acquire", "classification": "pending",
                    "disposition": "preparing",
                    "original_path": str(layout.workspaces_dir / workspace_id),
                    "planned_detached_path": str(planned_detached),
                }
                conn.execute(
                    """INSERT INTO workspace_lease_operations
                       (operation_id, kind, workspace_id, request_fingerprint, input_lease_id,
                        input_capability_hash, output_lease_id, output_capability_hash,
                        generation, ttl_seconds, state, created_at, updated_at)
                       VALUES (?, 'acquire', ?, ?, NULL, NULL, ?, ?, ?, ?, 'preparing', ?, ?)""",
                    (
                        operation_id, workspace_id, request_fingerprint, lease_id,
                        capability_hash, generation, ttl, now, now,
                    ),
                )
                conn.execute(
                    """INSERT OR REPLACE INTO workspace_leases
                       (workspace_id, lease_id, capability_hash, contract_version, state,
                        plugin_namespace, plugin_identity, profile_key, workspace_path, owner_pid,
                        owner_create_time, owner_host, owner_instance, ttl_seconds, acquired_at,
                        heartbeat_at, expires_at, heartbeat_monotonic, expires_monotonic,
                        expiry_observer, expiry_observed_monotonic, released_at, generation,
                        acquire_intent_id, cleanup_json, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        workspace_id, lease_id, capability_hash, HANDLE_VERSION, "preparing",
                        layout.plugin_namespace, layout.plugin_identity, layout.profile_key,
                        str(layout.workspaces_dir / workspace_id), pid, created, host, instance,
                        ttl, now, now, expires_wall, heartbeat_mono, expires_mono,
                        observer, observed_mono, None, generation, operation_id,
                        json.dumps(cleanup, sort_keys=True), now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,),
                ).fetchone()
                _cleanup_receipt(conn, row, "acquire", "planned", cleanup)
                _event(conn, row, "preparing", {"reclaimed": reclaimed, "cleanup": cleanup})

        with transaction(_connect(layout), immediate=True) as conn:
            current = _validated_row(
                conn, layout, _handle(lease_id, capability), validate_path=False,
            )
            if (
                current["state"] != "preparing"
                or int(current["generation"]) != generation
                or current["acquire_intent_id"] != operation_id
            ):
                raise InvalidWorkspaceHandleError("workspace preparation lost its fence")
            detached, cleanup = _detach_workspace(
                layout, workspace_id, lease_id, generation, "acquire", planned=planned_detached,
            )
            cleanup.update({
                "operation": "acquire", "planned_detached_path": str(planned_detached),
            })
            _cleanup_receipt(conn, current, "acquire", "detached", cleanup)
            path = layout.workspaces_dir / workspace_id
            if detached is None and cleanup["disposition"] == "preserved":
                preparation_error = WorkspacePathError(
                    f"cannot detach occupied workspace {path}; existing contents were preserved"
                )
            if preparation_error is None:
                try:
                    path.mkdir(mode=0o700)
                    if os.name != "nt":
                        os.chmod(path, 0o700)
                    _validate_workspace_path(path)
                    _sync_dirs(path, layout.workspaces_dir)
                except (OSError, WorkspacePathError) as exc:
                    preparation_error = (
                        exc if isinstance(exc, WorkspacePathError)
                        else WorkspacePathError(f"cannot create canonical workspace {path}: {exc}")
                    )
            if preparation_error is not None:
                cleanup.update({
                    "disposition": "preserved",
                    "preparation_error": f"{type(preparation_error).__name__}: {preparation_error}",
                })
                failed_at = time.time()
                changed = conn.execute(
                    """UPDATE workspace_leases SET state='released', released_at=?, cleanup_json=?,
                       updated_at=? WHERE lease_id=? AND state='preparing' AND generation=?
                       AND acquire_intent_id=?""",
                    (
                        failed_at, json.dumps(cleanup, sort_keys=True), failed_at,
                        lease_id, generation, operation_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise InvalidWorkspaceHandleError("workspace preparation failure lost its fence")
                conn.execute(
                    """UPDATE workspace_lease_operations SET state='failed', updated_at=?
                       WHERE operation_id=? AND output_lease_id=?""",
                    (failed_at, operation_id, lease_id),
                )
                failed = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,),
                ).fetchone()
                _cleanup_receipt(conn, failed, "acquire", "failed", cleanup)
                _event(conn, failed, "preparation_failed", {"cleanup": cleanup})
            else:
                changed = conn.execute(
                    """UPDATE workspace_leases SET cleanup_json=?, updated_at=? WHERE lease_id=?
                       AND state='preparing' AND generation=? AND acquire_intent_id=?""",
                    (
                        json.dumps(cleanup, sort_keys=True), time.time(), lease_id,
                        generation, operation_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise InvalidWorkspaceHandleError("workspace preparation lost its fence")

        if preparation_error is not None:
            raise preparation_error

        if detached is not None:
            finished_cleanup = _finish_detached_cleanup(detached, cleanup)
            finished_cleanup.update({
                "operation": "acquire", "planned_detached_path": str(planned_detached),
            })
            with transaction(_connect(layout), immediate=True) as conn:
                changed = conn.execute(
                    """UPDATE workspace_leases SET cleanup_json=?, updated_at=? WHERE lease_id=?
                       AND state='preparing' AND generation=? AND acquire_intent_id=?""",
                    (
                        json.dumps(finished_cleanup, sort_keys=True), time.time(), lease_id,
                        generation, operation_id,
                    ),
                ).rowcount
                if changed:
                    prepared = conn.execute(
                        "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,),
                    ).fetchone()
                    _cleanup_receipt(
                        conn, prepared, "acquire", "predecessor_cleanup_completed",
                        finished_cleanup,
                    )

        with transaction(_connect(layout), immediate=True) as conn:
            (
                ready_at, ready_expires_wall, ready_mono, ready_expires_mono,
                ready_observer, ready_observed_mono,
            ) = _fresh_expiry(ttl)
            current = _validated_row(
                conn, layout, _handle(lease_id, capability), validate_path=False,
            )
            if (
                current["state"] != "preparing"
                or int(current["generation"]) != generation
                or current["acquire_intent_id"] != operation_id
            ):
                raise InvalidWorkspaceHandleError("workspace activation lost its fence")
            _validate_workspace_path(Path(current["workspace_path"]))
            changed = conn.execute(
                """UPDATE workspace_leases SET state='active', heartbeat_at=?, expires_at=?,
                   heartbeat_monotonic=?, expires_monotonic=?, expiry_observer=?,
                   expiry_observed_monotonic=?, updated_at=? WHERE lease_id=?
                   AND state='preparing' AND generation=? AND acquire_intent_id=?""",
                (
                    ready_at, ready_expires_wall, ready_mono, ready_expires_mono,
                    ready_observer, ready_observed_mono, ready_at, lease_id,
                    generation, operation_id,
                ),
            ).rowcount
            if changed != 1:
                raise InvalidWorkspaceHandleError("workspace activation lost its fence")
            operation_changed = conn.execute(
                """UPDATE workspace_lease_operations SET state='committed', updated_at=?
                   WHERE operation_id=? AND kind='acquire' AND output_lease_id=?
                   AND generation=? AND state='preparing'""",
                (ready_at, operation_id, lease_id, generation),
            ).rowcount
            if operation_changed != 1:
                raise InvalidWorkspaceHandleError("workspace acquire receipt lost its fence")
            active = conn.execute(
                "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,),
            ).fetchone()
            _cleanup_receipt(conn, active, "acquire", "ready", json.loads(active["cleanup_json"]))
            _event(conn, active, "acquired", {"reclaimed": reclaimed, "operation_id": operation_id})
        return _response_handle(lease_id, capability)

    def renew(
        self, handle: Mapping[str, Any], *, ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        layout = self._layout()
        with transaction(_connect(layout), immediate=True) as conn:
            row = _validated_row(conn, layout, handle, validate_path=False)
            if row["state"] != "active":
                raise InvalidWorkspaceHandleError("workspace lease has been released")
            _validate_workspace_path(Path(row["workspace_path"]))
            if _lease_expired(conn, row):
                raise WorkspaceLeaseExpiredError("workspace lease expired; reconnect it before use")
            if _owner_state(row) is not True:
                raise WorkspaceOwnershipError("workspace lease belongs to another live process")
            ttl = _ttl(row["ttl_seconds"] if ttl_seconds is None else ttl_seconds)
            now, expires_wall, heartbeat_mono, expires_mono, observer, observed_mono = (
                _fresh_expiry(ttl)
            )
            conn.execute(
                """UPDATE workspace_leases SET ttl_seconds=?, heartbeat_at=?, expires_at=?,
                   heartbeat_monotonic=?, expires_monotonic=?, expiry_observer=?,
                   expiry_observed_monotonic=?, updated_at=? WHERE lease_id=?
                   AND state='active' AND generation=?""",
                (
                    ttl, now, expires_wall, heartbeat_mono, expires_mono, observer,
                    observed_mono, now, row["lease_id"], row["generation"],
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise InvalidWorkspaceHandleError("workspace lease changed during renewal")
            updated = conn.execute("SELECT * FROM workspace_leases WHERE lease_id=?", (row["lease_id"],)).fetchone()
            _event(conn, updated, "renewed", {"ttl_seconds": ttl})
            return _public_snapshot(conn, updated)

    def reconnect(
        self, handle: Mapping[str, Any], *, intent: Mapping[str, Any],
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        predecessor_lease_id, predecessor_capability = _parse_handle(handle)
        operation_id, successor_capability = _parse_intent(intent)
        predecessor_hash = _capability_hash(predecessor_capability)
        successor_hash = _capability_hash(successor_capability)
        if hmac.compare_digest(predecessor_hash, successor_hash):
            raise InvalidWorkspaceHandleError(
                "reconnect intent must use a fresh successor capability"
            )
        layout = self._layout()
        with transaction(_connect(layout), immediate=True) as conn:
            now = time.time()
            operation = conn.execute(
                "SELECT * FROM workspace_lease_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if operation is not None:
                ttl = _ttl(operation["ttl_seconds"] if ttl_seconds is None else ttl_seconds)
                fingerprint = _operation_fingerprint(
                    layout, "reconnect", operation["workspace_id"], ttl,
                    predecessor_lease_id,
                )
                _validate_operation(
                    operation, kind="reconnect", workspace_id=operation["workspace_id"],
                    fingerprint=fingerprint, output_capability_hash=successor_hash,
                    input_lease_id=predecessor_lease_id,
                    input_capability_hash=predecessor_hash,
                )
                if operation["state"] != "committed":
                    raise InvalidWorkspaceHandleError("workspace reconnect intent is not committed")
                row = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?",
                    (operation["output_lease_id"],),
                ).fetchone()
                if (
                    row is None
                    or row["state"] != "active"
                    or int(row["generation"]) != int(operation["generation"])
                    or row["plugin_identity"] != layout.plugin_identity
                    or row["profile_key"] != layout.profile_key
                    or not hmac.compare_digest(row["capability_hash"], successor_hash)
                ):
                    conn.execute(
                        """UPDATE workspace_lease_operations SET state='superseded', updated_at=?
                           WHERE operation_id=?""",
                        (now, operation_id),
                    )
                    raise InvalidWorkspaceHandleError("workspace reconnect intent was superseded")
                owner_status = _owner_status(row)
                expired = _lease_expired(conn, row)
                if owner_status in {"live", "unknown"} and not expired:
                    raise WorkspaceOwnershipError(
                        "workspace lease still belongs to another live process"
                    )
                _validate_workspace_path(Path(row["workspace_path"]))
                (
                    fresh_wall, fresh_expires_wall, fresh_mono, fresh_expires_mono,
                    fresh_observer, fresh_observed_mono,
                ) = _fresh_expiry(ttl)
                changed = conn.execute(
                    """UPDATE workspace_leases SET owner_pid=?, owner_create_time=?, owner_host=?,
                       owner_instance=?, ttl_seconds=?, heartbeat_at=?, expires_at=?,
                       heartbeat_monotonic=?, expires_monotonic=?, expiry_observer=?,
                       expiry_observed_monotonic=?, updated_at=?
                       WHERE lease_id=? AND generation=? AND state='active'""",
                    (
                        *_owner_stamp(), ttl, fresh_wall, fresh_expires_wall,
                        fresh_mono, fresh_expires_mono, fresh_observer,
                        fresh_observed_mono, fresh_wall,
                        row["lease_id"], row["generation"],
                    ),
                ).rowcount
                if changed != 1:
                    raise InvalidWorkspaceHandleError("workspace reconnect replay lost its fence")
                updated = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (row["lease_id"],),
                ).fetchone()
                _event(conn, updated, "reconnect_replayed", {"operation_id": operation_id})
                successor_lease_id = str(updated["lease_id"])
            else:
                row = _validated_row(conn, layout, handle, validate_path=False)
                ttl = _ttl(row["ttl_seconds"] if ttl_seconds is None else ttl_seconds)
                fingerprint = _operation_fingerprint(
                    layout, "reconnect", row["workspace_id"], ttl, predecessor_lease_id,
                )
                if row["state"] != "active":
                    raise InvalidWorkspaceHandleError("workspace lease has been released")
                _validate_workspace_path(Path(row["workspace_path"]))
                owner_status = _owner_status(row)
                expired = _lease_expired(conn, row)
                if owner_status in {"live", "unknown"} and not expired:
                    raise WorkspaceOwnershipError(
                        "workspace lease still belongs to another live process"
                    )
                successor_lease_id = str(uuid.uuid4())
                successor_generation = int(row["generation"]) + 1
                _supersede_lease_operations(conn, predecessor_lease_id, now)
                conn.execute(
                    """INSERT INTO workspace_lease_operations
                       (operation_id, kind, workspace_id, request_fingerprint, input_lease_id,
                        input_capability_hash, output_lease_id, output_capability_hash,
                        generation, ttl_seconds, state, created_at, updated_at)
                       VALUES (?, 'reconnect', ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?, ?)""",
                    (
                        operation_id, row["workspace_id"], fingerprint,
                        predecessor_lease_id, predecessor_hash, successor_lease_id,
                        successor_hash, successor_generation, ttl, now, now,
                    ),
                )
                pid, created, host, instance = _owner_stamp()
                (
                    fresh_wall, fresh_expires_wall, fresh_mono, fresh_expires_mono,
                    fresh_observer, fresh_observed_mono,
                ) = _fresh_expiry(ttl)
                changed = conn.execute(
                    """UPDATE workspace_leases SET owner_pid=?, owner_create_time=?, owner_host=?,
                       owner_instance=?, ttl_seconds=?, heartbeat_at=?, expires_at=?,
                       heartbeat_monotonic=?, expires_monotonic=?, expiry_observer=?,
                       expiry_observed_monotonic=?, updated_at=?, lease_id=?, capability_hash=?,
                       generation=?
                       WHERE lease_id=? AND state='active' AND generation=?""",
                    (
                        pid, created, host, instance, ttl, fresh_wall, fresh_expires_wall,
                        fresh_mono, fresh_expires_mono, fresh_observer,
                        fresh_observed_mono, fresh_wall,
                        successor_lease_id, successor_hash, successor_generation,
                        predecessor_lease_id, row["generation"],
                    ),
                ).rowcount
                if changed != 1:
                    raise InvalidWorkspaceHandleError("workspace lease changed during reconnect")
                updated = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (successor_lease_id,),
                ).fetchone()
                _event(conn, updated, "reconnected", {
                    "operation_id": operation_id,
                    "predecessor_lease_id": predecessor_lease_id,
                    "previous_owner": "expired" if expired else owner_status,
                })
        return _response_handle(successor_lease_id, successor_capability)

    def inspect(self, handle: Mapping[str, Any]) -> dict[str, Any]:
        layout = self._layout()
        with transaction(_connect(layout), immediate=True) as conn:
            row = _validated_row(conn, layout, handle, validate_path=False)
            if row["state"] != "active":
                raise InvalidWorkspaceHandleError("workspace lease has been released")
            _validate_workspace_path(Path(row["workspace_path"]))
            if _lease_expired(conn, row):
                raise WorkspaceLeaseExpiredError("workspace lease expired; reconnect it before use")
            return _public_snapshot(conn, row)

    def release(self, handle: Mapping[str, Any]) -> dict[str, Any]:
        layout = self._layout()
        detached: Path | None = None
        released_snapshot: dict[str, Any] | None = None
        release_error: WorkspacePathError | None = None
        with transaction(_connect(layout), immediate=True) as conn:
            now = time.time()
            row = _validated_row(conn, layout, handle, validate_path=False)
            if row["state"] == "released":
                cleanup = json.loads(row["cleanup_json"] or "{}")
                if cleanup.get("operation") != "release" or cleanup.get("disposition") not in {
                    "releasing", "detached",
                }:
                    return _public_snapshot(conn, row)
                planned = _planned_detached_path(
                    layout, row["workspace_id"], row["lease_id"], row["generation"], "release",
                )
                detached = planned if planned.exists() or planned.is_symlink() else None
                if detached is None:
                    cleanup = {
                        **cleanup, "classification": "missing", "safe_to_delete": True,
                        "disposition": "removed",
                        "reconciled_reason": "detached_target_absent",
                        "planned_detached_path": str(planned),
                    }
                    changed = conn.execute(
                        """UPDATE workspace_leases SET cleanup_json=?, updated_at=?
                           WHERE lease_id=? AND state='released' AND generation=?""",
                        (
                            json.dumps(cleanup, sort_keys=True), now, row["lease_id"],
                            row["generation"],
                        ),
                    ).rowcount
                    if changed != 1:
                        raise InvalidWorkspaceHandleError(
                            "workspace cleanup changed during reconciliation"
                        )
                    row = conn.execute(
                        "SELECT * FROM workspace_leases WHERE lease_id=?", (row["lease_id"],)
                    ).fetchone()
                    _cleanup_receipt(
                        conn, row, "release", "cleanup_reconciled", cleanup,
                    )
                    _event(conn, row, "cleanup_reconciled", {"cleanup": cleanup})
                    return _public_snapshot(conn, row)
                released_snapshot = _public_snapshot(conn, row)
            else:
                if row["state"] not in {"active", "releasing"}:
                    raise InvalidWorkspaceHandleError("workspace lease is not active")
                owner_status = _owner_status(row)
                expired = _lease_expired(conn, row)
                if owner_status in {"live", "unknown"} and not expired:
                    raise WorkspaceOwnershipError("workspace lease still belongs to another live process")
                planned = _planned_detached_path(
                    layout, row["workspace_id"], row["lease_id"], row["generation"], "release",
                )
                if row["state"] == "active":
                    cleanup = {
                        "operation": "release", "classification": "pending",
                        "disposition": "releasing", "original_path": row["workspace_path"],
                        "planned_detached_path": str(planned),
                    }
                    changed = conn.execute(
                        """UPDATE workspace_leases SET state='releasing', cleanup_json=?, updated_at=?
                           WHERE lease_id=? AND state='active' AND generation=?""",
                        (
                            json.dumps(cleanup, sort_keys=True), now, row["lease_id"],
                            row["generation"],
                        ),
                    ).rowcount
                    if changed != 1:
                        raise InvalidWorkspaceHandleError("workspace lease changed during release")
                    _supersede_lease_operations(conn, row["lease_id"], now)
                    row = conn.execute(
                        "SELECT * FROM workspace_leases WHERE lease_id=?", (row["lease_id"],)
                    ).fetchone()
                    _cleanup_receipt(conn, row, "release", "planned", cleanup)
                    _event(conn, row, "release_started", {"cleanup": cleanup})

        if row["state"] != "released":
            # Releasing + deterministic rename are durable before mutation. A retry after process
            # death resumes the same name; a DB rollback cannot resurrect an active predecessor.
            with transaction(_connect(layout), immediate=True) as conn:
                current = _validated_row(conn, layout, handle, validate_path=False)
                if current["state"] == "released":
                    released_snapshot = _public_snapshot(conn, current)
                else:
                    if (
                        current["state"] != "releasing"
                        or current["generation"] != row["generation"]
                    ):
                        raise InvalidWorkspaceHandleError("workspace lease changed during release")
                    detached, cleanup = _detach_workspace(
                        layout, current["workspace_id"], current["lease_id"],
                        current["generation"], "release", planned=planned,
                    )
                    cleanup["operation"] = "release"
                    cleanup["planned_detached_path"] = str(planned)
                    _cleanup_receipt(conn, current, "release", "detached", cleanup)
                    finished_at = time.time()
                    if detached is None and cleanup.get("disposition") == "preserved":
                        changed = conn.execute(
                            """UPDATE workspace_leases SET cleanup_json=?, updated_at=?
                               WHERE lease_id=? AND state='releasing' AND generation=?""",
                            (
                                json.dumps(cleanup, sort_keys=True), finished_at,
                                current["lease_id"], current["generation"],
                            ),
                        ).rowcount
                        if changed != 1:
                            raise InvalidWorkspaceHandleError(
                                "workspace release failure lost its fence"
                            )
                        failed = conn.execute(
                            "SELECT * FROM workspace_leases WHERE lease_id=?",
                            (current["lease_id"],),
                        ).fetchone()
                        _cleanup_receipt(conn, failed, "release", "detach_failed", cleanup)
                        _event(conn, failed, "release_detach_failed", {"cleanup": cleanup})
                        release_error = WorkspacePathError(
                            f"workspace release could not detach {current['workspace_path']}; "
                            "contents were preserved and the same handle can retry"
                        )
                    else:
                        changed = conn.execute(
                            """UPDATE workspace_leases SET state='released', released_at=?,
                               cleanup_json=?, updated_at=? WHERE lease_id=? AND state='releasing'
                               AND generation=?""",
                            (
                                finished_at, json.dumps(cleanup, sort_keys=True), finished_at,
                                current["lease_id"], current["generation"],
                            ),
                        ).rowcount
                        if changed != 1:
                            raise InvalidWorkspaceHandleError(
                                "workspace lease changed during release"
                            )
                        updated = conn.execute(
                            "SELECT * FROM workspace_leases WHERE lease_id=?",
                            (current["lease_id"],),
                        ).fetchone()
                        _cleanup_receipt(conn, updated, "release", "released", cleanup)
                        _event(conn, updated, "released", {"cleanup": cleanup})
                        released_snapshot = _public_snapshot(conn, updated)

        if release_error is not None:
            raise release_error

        if detached is None:
            assert released_snapshot is not None
            return released_snapshot
        cleanup = _finish_detached_cleanup(detached, cleanup)
        cleanup["operation"] = "release"
        cleanup["planned_detached_path"] = str(planned)
        finished = time.time()
        with transaction(_connect(layout), immediate=True) as conn:
            # Receipt/event ownership is immutable per generation and does not depend on the mutable
            # current-row CAS: a successor may already own the canonical workspace while this detached
            # predecessor finishes its slow cleanup.
            _cleanup_receipt(conn, row, "release", "cleanup_completed", cleanup)
            _event(conn, row, "cleanup_completed", {"cleanup": cleanup})
            changed = conn.execute(
                """UPDATE workspace_leases SET cleanup_json=?, updated_at=?
                   WHERE lease_id=? AND state='released' AND generation=?""",
                (
                    json.dumps(cleanup, sort_keys=True), finished, row["lease_id"],
                    row["generation"],
                ),
            ).rowcount
            if not changed:
                assert released_snapshot is not None
                actual = dict(released_snapshot)
                actual["cleanup"] = cleanup
                actual["cleanupReceipts"] = _cleanup_history(conn, row["workspace_id"])
                actual["events"] = _event_history(conn, row["workspace_id"])
                return actual
            updated = conn.execute(
                "SELECT * FROM workspace_leases WHERE lease_id=?", (row["lease_id"],)
            ).fetchone()
            return _public_snapshot(conn, updated)


__all__ = [
    "DEFAULT_TTL_SECONDS", "HANDLE_VERSION", "HOST_FEATURE", "PluginWorkspaces",
    "InvalidWorkspaceHandleError", "WorkspaceInUseError", "WorkspaceLeaseError",
    "WorkspaceDurabilityError", "WorkspaceLeaseExpiredError", "WorkspaceOwnershipError",
    "WorkspacePathError",
]
