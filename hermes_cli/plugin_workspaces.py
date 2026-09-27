"""Durable, profile-scoped workspace leases for trusted plugins.

The handle is a bearer capability, not a path.  Only a SHA-256 digest is
persisted; the plugin must keep the serializable handle if it wants to renew,
reconnect, inspect, or release a lease after a host restart.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import time
import uuid
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


@dataclass(frozen=True)
class _Layout:
    home: Path
    profile_key: str
    plugin_namespace: str
    data_dir: Path
    workspaces_dir: Path
    quarantine_dir: Path
    db_path: Path


def _plugin_namespace(plugin_id: str, skill_namespace: str) -> str:
    candidate = skill_namespace or plugin_id
    folded = candidate.casefold()
    if (
        candidate == folded
        and _PLUGIN_NAMESPACE_RE.fullmatch(candidate)
        and ".." not in candidate
        and candidate.split(".", 1)[0] not in _WINDOWS_RESERVED
    ):
        return candidate
    return _portable_skill_namespace(candidate)


def _validated_workspace_id(workspace_id: str) -> str:
    if (
        not isinstance(workspace_id, str)
        or not _WORKSPACE_ID_RE.fullmatch(workspace_id)
        or ".." in workspace_id
        or workspace_id.split(".", 1)[0] in _WINDOWS_RESERVED
    ):
        raise ValueError(
            "workspace_id must be 1-128 lowercase ASCII letters, numbers, '.', '_', or '-' "
            "(without '..' or a reserved device name)"
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
    plugin_data = _safe_child(home, "plugin-data")
    data_dir = _safe_child(plugin_data, namespace)
    workspaces = _safe_child(data_dir, "workspaces")
    quarantine = _safe_child(data_dir, "workspace-quarantine")
    return _Layout(
        home=home,
        profile_key=hermes_home_key(home),
        plugin_namespace=namespace,
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
            released_at REAL,
            generation INTEGER NOT NULL,
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
        """
    )


def _connect(layout: _Layout):
    if layout.db_path.is_symlink():
        raise WorkspacePathError(f"workspace lease database cannot be a symlink: {layout.db_path}")
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
            os.chmod(layout.db_path, 0o600)
    except BaseException:
        conn.close()
        raise
    return conn


def _host_instance() -> str:
    """Boot/container witness; paired with pid/create-time to survive persisted rows across reboot."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if boot_id:
            return f"boot-id:{boot_id}"
    except OSError:
        pass
    try:
        import psutil
        return f"boot-time:{float(psutil.boot_time()):.6f}"
    except Exception:
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
    if str(_row_value(row, "owner_host") or "") != socket.gethostname():
        return "unknown"
    current_instance = _host_instance()
    recorded_instance = str(_row_value(row, "owner_instance") or "unverified")
    if recorded_instance != "unverified" and current_instance != recorded_instance:
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


def _handle(lease_id: str, capability: str) -> dict[str, Any]:
    return {"contract_version": HANDLE_VERSION, "lease_id": lease_id, "capability": capability}


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


def _validated_row(
    conn, layout: _Layout, handle: Mapping[str, Any], *, validate_path: bool = True,
):
    lease_id, capability = _parse_handle(handle)
    row = conn.execute("SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)).fetchone()
    if row is None or not hmac.compare_digest(str(row["capability_hash"]), _capability_hash(capability)):
        raise InvalidWorkspaceHandleError("workspace lease handle is invalid or stale")
    if row["plugin_namespace"] != layout.plugin_namespace or row["profile_key"] != layout.profile_key:
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
            return {"classification": "clean_git", "safe_to_delete": True, "checked_at": checked_at}
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


def _planned_detached_path(layout: _Layout, workspace_id: str, lease_id: str) -> Path:
    return layout.quarantine_dir / f".detached-{workspace_id}-{lease_id[:8]}-{secrets.token_hex(4)}"


def _detach_workspace(
    layout: _Layout, workspace_id: str, lease_id: str, *, planned: Path | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Atomically remove the leased name before DB unlock so a successor can never be cleaned."""
    path = layout.workspaces_dir / workspace_id
    detached = planned or _planned_detached_path(layout, workspace_id, lease_id)
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
        os.replace(path, detached)
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
            if detached.is_dir() and not detached.is_symlink():
                shutil.rmtree(detached)
            else:
                detached.unlink()
            finished["disposition"] = "removed"
            finished.pop("detached_path", None)
        else:
            finished.update({"disposition": "quarantined", "quarantine_path": str(detached)})
            finished.pop("detached_path", None)
    except OSError as exc:
        finished.update({
            "disposition": "preserved", "quarantine_path": str(detached),
            "cleanup_error": f"{type(exc).__name__}: {exc}",
        })
    return finished


def _public_snapshot(conn, row: Mapping[str, Any]) -> dict[str, Any]:
    cleanup = json.loads(row["cleanup_json"]) if row["cleanup_json"] else None
    events = conn.execute(
        """SELECT lease_id, generation, event_type, occurred_at, actor_pid,
                  actor_create_time, details_json
           FROM workspace_lease_events WHERE workspace_id=? ORDER BY event_id DESC LIMIT 50""",
        (row["workspace_id"],),
    ).fetchall()
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
        "releasedAt": row["released_at"],
        "ttlSeconds": float(row["ttl_seconds"]),
        "cleanup": cleanup,
        "events": [
            {
                "type": event["event_type"], "at": float(event["occurred_at"]),
                "leaseId": event["lease_id"], "generation": int(event["generation"]),
                "actorPid": int(event["actor_pid"]), "actorCreateTime": event["actor_create_time"],
                "details": json.loads(event["details_json"]),
            }
            for event in reversed(events)
        ],
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

    def acquire(
        self, workspace_id: str, *, ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        workspace_id, ttl = _validated_workspace_id(workspace_id), _ttl(ttl_seconds)
        layout, now = self._layout(), time.time()
        lease_id, capability = str(uuid.uuid4()), secrets.token_urlsafe(32)
        pid, created, host, instance = _owner_stamp()
        planned_detached = _planned_detached_path(layout, workspace_id, lease_id)
        detached: Path | None = None
        preparation_error: WorkspacePathError | None = None
        with transaction(_connect(layout), immediate=True) as conn:
            old = conn.execute(
                "SELECT * FROM workspace_leases WHERE workspace_id=?", (workspace_id,)
            ).fetchone()
            generation, reclaimed = 1, None
            if old is not None:
                generation = int(old["generation"]) + 1
                if old["plugin_namespace"] != layout.plugin_namespace or old["profile_key"] != layout.profile_key:
                    raise WorkspacePathError("workspace lease database belongs to another plugin or profile")
                if old["state"] in {"active", "preparing", "releasing"}:
                    owner_status = _owner_status(old)
                    expired = float(old["expires_at"]) <= now
                    # A live preparation is never reclaimed on its short TTL boundary: the process may
                    # be between the rename and mkdir below. Active generations use normal heartbeat TTL.
                    live_transition = old["state"] in {"preparing", "releasing"}
                    if owner_status in {"self", "live"} and live_transition:
                        raise WorkspaceInUseError(
                            f"workspace {workspace_id!r} is being transitioned by a live owner"
                        )
                    if owner_status != "dead" and not expired:
                        raise WorkspaceInUseError(
                            f"workspace {workspace_id!r} is already leased until {old['expires_at']}"
                        )
                    reclaimed = "owner_dead" if owner_status == "dead" else "ttl_expired"
            cleanup = {
                "classification": "pending", "disposition": "preparing",
                "original_path": str(layout.workspaces_dir / workspace_id),
                "planned_detached_path": str(planned_detached),
            }
            row_values = (
                workspace_id, lease_id, _capability_hash(capability), HANDLE_VERSION, "preparing",
                layout.plugin_namespace, layout.profile_key, str(layout.workspaces_dir / workspace_id),
                pid, created, host, instance, ttl, now, now, now + ttl, None, generation,
                json.dumps(cleanup, sort_keys=True), now,
            )
            conn.execute(
                """INSERT OR REPLACE INTO workspace_leases
                   (workspace_id, lease_id, capability_hash, contract_version, state,
                    plugin_namespace, profile_key, workspace_path, owner_pid, owner_create_time,
                    owner_host, owner_instance, ttl_seconds, acquired_at, heartbeat_at,
                    expires_at, released_at,
                    generation, cleanup_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row_values,
            )
            row = conn.execute("SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)).fetchone()
            _event(conn, row, "preparing", {"reclaimed": reclaimed, "cleanup": cleanup})

        # The preparing row is now durable.  Hold another IMMEDIATE transaction across the
        # canonical-name handoff; a crash/commit failure leaves a reclaimable preparing generation,
        # never the predecessor row pointing at a replacement directory.
        with transaction(_connect(layout), immediate=True) as conn:
            current = _validated_row(
                conn, layout, _handle(lease_id, capability), validate_path=False,
            )
            if current["state"] != "preparing" or int(current["generation"]) != generation:
                raise InvalidWorkspaceHandleError(
                    "workspace lease changed while it was being prepared"
                )
            detached, cleanup = _detach_workspace(
                layout, workspace_id, lease_id, planned=planned_detached,
            )
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
                    if path.is_symlink() or os.path.normcase(
                        str(path.resolve(strict=True))
                    ) != os.path.normcase(str(path.absolute())):
                        raise WorkspacePathError(f"workspace path is not canonical: {path}")
                except (OSError, WorkspacePathError) as exc:
                    preparation_error = (
                        exc if isinstance(exc, WorkspacePathError) else
                        WorkspacePathError(f"cannot create canonical workspace {path}: {exc}")
                    )
            if preparation_error is not None:
                cleanup.update({
                    "disposition": "preserved",
                    "preparation_error": (
                        f"{type(preparation_error).__name__}: {preparation_error}"
                    ),
                })
                conn.execute(
                    """UPDATE workspace_leases SET state='released', released_at=?, cleanup_json=?,
                       updated_at=? WHERE lease_id=? AND state='preparing' AND generation=?""",
                    (
                        time.time(), json.dumps(cleanup, sort_keys=True), time.time(),
                        lease_id, generation,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise InvalidWorkspaceHandleError(
                        "workspace lease changed while preparation failed"
                    )
                failed = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
                _event(conn, failed, "preparation_failed", {"cleanup": cleanup})
            else:
                conn.execute(
                    """UPDATE workspace_leases SET state='active', cleanup_json=?, updated_at=?
                       WHERE lease_id=? AND state='preparing' AND generation=?""",
                    (json.dumps(cleanup, sort_keys=True), time.time(), lease_id, generation),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise InvalidWorkspaceHandleError(
                        "workspace lease changed while it was being prepared"
                    )
                active = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
                _event(conn, active, "acquired", {"reclaimed": reclaimed, "cleanup": cleanup})

        if preparation_error is not None:
            raise preparation_error

        if detached is not None:
            finished_cleanup = _finish_detached_cleanup(detached, cleanup)
            with transaction(_connect(layout), immediate=True) as conn:
                changed = conn.execute(
                    """UPDATE workspace_leases SET cleanup_json=?, updated_at=?
                       WHERE lease_id=? AND state='active' AND generation=?""",
                    (json.dumps(finished_cleanup, sort_keys=True), time.time(), lease_id, generation),
                ).rowcount
                if changed:
                    active = conn.execute(
                        "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)
                    ).fetchone()
                    _event(conn, active, "predecessor_cleanup_completed", {"cleanup": finished_cleanup})
        return _handle(lease_id, capability)

    def renew(
        self, handle: Mapping[str, Any], *, ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        layout, now = self._layout(), time.time()
        with transaction(_connect(layout), immediate=True) as conn:
            row = _validated_row(conn, layout, handle, validate_path=False)
            if row["state"] != "active":
                raise InvalidWorkspaceHandleError("workspace lease has been released")
            _validate_workspace_path(Path(row["workspace_path"]))
            if float(row["expires_at"]) <= now:
                raise WorkspaceLeaseExpiredError("workspace lease expired; reconnect it before use")
            if _owner_state(row) is not True:
                raise WorkspaceOwnershipError("workspace lease belongs to another live process")
            ttl = _ttl(row["ttl_seconds"] if ttl_seconds is None else ttl_seconds)
            conn.execute(
                "UPDATE workspace_leases SET ttl_seconds=?, heartbeat_at=?, expires_at=?, updated_at=? "
                "WHERE lease_id=? AND state='active' AND generation=?",
                (ttl, now, now + ttl, now, row["lease_id"], row["generation"]),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise InvalidWorkspaceHandleError("workspace lease changed during renewal")
            updated = conn.execute("SELECT * FROM workspace_leases WHERE lease_id=?", (row["lease_id"],)).fetchone()
            _event(conn, updated, "renewed", {"ttl_seconds": ttl})
            return _public_snapshot(conn, updated)

    def reconnect(
        self, handle: Mapping[str, Any], *, ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        layout, now = self._layout(), time.time()
        with transaction(_connect(layout), immediate=True) as conn:
            row = _validated_row(conn, layout, handle, validate_path=False)
            if row["state"] != "active":
                raise InvalidWorkspaceHandleError("workspace lease has been released")
            _validate_workspace_path(Path(row["workspace_path"]))
            owner, expired = _owner_state(row), float(row["expires_at"]) <= now
            if owner is None:
                raise WorkspaceOwnershipError("workspace lease still belongs to another live process")
            ttl = _ttl(row["ttl_seconds"] if ttl_seconds is None else ttl_seconds)
            previous_lease_id = row["lease_id"]
            _event(conn, row, "reconnect_started", {"expired": expired})
            successor_lease_id, successor_capability = str(uuid.uuid4()), secrets.token_urlsafe(32)
            pid, created, host, instance = _owner_stamp()
            conn.execute(
                """UPDATE workspace_leases SET owner_pid=?, owner_create_time=?, owner_host=?,
                   owner_instance=?, ttl_seconds=?, heartbeat_at=?, expires_at=?, updated_at=?,
                   lease_id=?, capability_hash=?, generation=generation+1
                   WHERE lease_id=? AND state='active' AND generation=?""",
                (
                    pid, created, host, instance, ttl, now, now + ttl, now, successor_lease_id,
                    _capability_hash(successor_capability), previous_lease_id, row["generation"],
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise InvalidWorkspaceHandleError("workspace lease changed during reconnect")
            updated = conn.execute(
                "SELECT * FROM workspace_leases WHERE lease_id=?", (successor_lease_id,)
            ).fetchone()
            _event(conn, updated, "reconnected", {
                "predecessor_lease_id": previous_lease_id,
                "previous_owner": "dead" if owner is False else "expired" if expired else "self",
            })
            return _handle(successor_lease_id, successor_capability)

    def inspect(self, handle: Mapping[str, Any]) -> dict[str, Any]:
        layout, now = self._layout(), time.time()
        with transaction(_connect(layout)) as conn:
            row = _validated_row(conn, layout, handle, validate_path=False)
            if row["state"] != "active":
                raise InvalidWorkspaceHandleError("workspace lease has been released")
            _validate_workspace_path(Path(row["workspace_path"]))
            if float(row["expires_at"]) <= now:
                raise WorkspaceLeaseExpiredError("workspace lease expired; reconnect it before use")
            return _public_snapshot(conn, row)

    def release(self, handle: Mapping[str, Any]) -> dict[str, Any]:
        layout, now = self._layout(), time.time()
        with transaction(_connect(layout), immediate=True) as conn:
            row = _validated_row(conn, layout, handle, validate_path=False)
            if row["state"] == "released":
                return _public_snapshot(conn, row)
            if row["state"] not in {"active", "releasing"}:
                raise InvalidWorkspaceHandleError("workspace lease is not active")
            owner_status = _owner_status(row)
            expired = float(row["expires_at"]) <= now
            if owner_status in {"live", "unknown"} and not expired:
                raise WorkspaceOwnershipError("workspace lease still belongs to another live process")
            if row["state"] == "active":
                planned = _planned_detached_path(layout, row["workspace_id"], row["lease_id"])
                cleanup = {
                    "classification": "pending", "disposition": "releasing",
                    "original_path": row["workspace_path"],
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
                row = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (row["lease_id"],)
                ).fetchone()
                _event(conn, row, "release_started", {"cleanup": cleanup})
            else:
                cleanup = json.loads(row["cleanup_json"] or "{}")
                try:
                    planned = Path(cleanup["planned_detached_path"])
                except (KeyError, TypeError) as exc:
                    raise InvalidWorkspaceHandleError("workspace release receipt is malformed") from exc
                if planned.parent != layout.quarantine_dir:
                    raise InvalidWorkspaceHandleError("workspace release receipt escaped quarantine")

        # The releasing state and planned rename are durable before filesystem mutation.  A retry after
        # process death resumes the same target; a commit failure cannot resurrect an active predecessor.
        with transaction(_connect(layout), immediate=True) as conn:
            current = _validated_row(conn, layout, handle, validate_path=False)
            if current["state"] == "released":
                return _public_snapshot(conn, current)
            if current["state"] != "releasing" or current["generation"] != row["generation"]:
                raise InvalidWorkspaceHandleError("workspace lease changed during release")
            detached, cleanup = _detach_workspace(
                layout, current["workspace_id"], current["lease_id"], planned=planned,
            )
            finished_at = time.time()
            changed = conn.execute(
                """UPDATE workspace_leases SET state='released', released_at=?, cleanup_json=?,
                   updated_at=? WHERE lease_id=? AND state='releasing' AND generation=?""",
                (
                    finished_at, json.dumps(cleanup, sort_keys=True), finished_at,
                    current["lease_id"], current["generation"],
                ),
            ).rowcount
            if changed != 1:
                raise InvalidWorkspaceHandleError("workspace lease changed during release")
            updated = conn.execute(
                "SELECT * FROM workspace_leases WHERE lease_id=?", (current["lease_id"],)
            ).fetchone()
            _event(conn, updated, "released", {"cleanup": cleanup})
            released_snapshot = _public_snapshot(conn, updated)

        if detached is None:
            return released_snapshot
        cleanup = _finish_detached_cleanup(detached, cleanup)
        finished = time.time()
        with transaction(_connect(layout), immediate=True) as conn:
            changed = conn.execute(
                """UPDATE workspace_leases SET cleanup_json=?, updated_at=?
                   WHERE lease_id=? AND state='released' AND generation=?""",
                (
                    json.dumps(cleanup, sort_keys=True), finished, current["lease_id"],
                    current["generation"],
                ),
            ).rowcount
            if not changed:
                return released_snapshot
            updated = conn.execute(
                "SELECT * FROM workspace_leases WHERE lease_id=?", (current["lease_id"],)
            ).fetchone()
            _event(conn, updated, "cleanup_completed", {"cleanup": cleanup})
            return _public_snapshot(conn, updated)


__all__ = [
    "DEFAULT_TTL_SECONDS", "HANDLE_VERSION", "HOST_FEATURE", "PluginWorkspaces",
    "InvalidWorkspaceHandleError", "WorkspaceInUseError", "WorkspaceLeaseError",
    "WorkspaceLeaseExpiredError", "WorkspaceOwnershipError", "WorkspacePathError",
]
