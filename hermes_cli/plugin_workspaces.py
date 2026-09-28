"""Durable, profile-scoped workspace leases for cooperative trusted plugins.

The handle is a bearer capability, not a path.  Only a SHA-256 digest is
persisted; the plugin must keep the serializable handle if it wants to renew,
reconnect, inspect, or release a lease after a host restart.

This v1 service coordinates trusted plugin workers.  It deliberately does not
try to defend its directories from a hostile local process or plugin; stronger
filesystem and execution isolation is tracked separately in Hermes issue #9.
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
import threading
import time
import uuid
import weakref
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
INTENT_VERSION = 1
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


class InvalidWorkspaceIntentError(WorkspaceLeaseError):
    """The caller-persisted idempotency intent is malformed, stale, or reused."""


class WorkspaceInUseError(WorkspaceLeaseError):
    """A live or not-yet-expired owner already holds the workspace."""


class WorkspaceLeaseExpiredError(InvalidWorkspaceHandleError):
    """The lease heartbeat expired; reconnect is required before further use."""


class WorkspaceOwnershipError(WorkspaceLeaseError):
    """A different live process owns the lease."""


class WorkspacePathError(WorkspaceLeaseError):
    """The expected workspace directory is missing or unusable."""


@dataclass(frozen=True)
class _Layout:
    profile_key: str
    plugin_namespace: str
    plugin_identity_digest: str
    workspaces_dir: Path
    quarantine_dir: Path
    db_path: Path


@dataclass(frozen=True)
class _PinnedWorkspace:
    """Host-derived inputs held stable for one synchronous bound dispatch."""

    path: Path
    task_id: str
    generation: int


_generation_locks_guard = threading.Lock()
_generation_locks: weakref.WeakValueDictionary[tuple[str, str, str], threading.RLock] = (
    weakref.WeakValueDictionary()
)


def _generation_lock(layout: _Layout, workspace_id: str) -> threading.RLock:
    """One process-local mutex for a plugin/profile/workspace name."""
    key = (layout.profile_key, layout.plugin_identity_digest, workspace_id)
    with _generation_locks_guard:
        lock = _generation_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _generation_locks[key] = lock
        return lock


def _plugin_namespace(plugin_id: str, skill_namespace: str) -> str:
    candidate = skill_namespace or plugin_id
    folded = candidate.casefold()
    if (
        candidate == folded
        and _PLUGIN_NAMESPACE_RE.fullmatch(candidate)
        and ".." not in candidate
        and not candidate.endswith(".")
        and candidate.split(".", 1)[0] not in _WINDOWS_RESERVED
    ):
        return candidate
    return _portable_skill_namespace(candidate)


def _plugin_identity_digest(plugin_id: str, skill_namespace: str) -> str:
    """Bind storage to the exact native/portable plugin identity, not just its display name."""
    kind = "portable" if skill_namespace else "native"
    identity = json.dumps(
        {"kind": kind, "plugin_id": plugin_id, "skill_namespace": skill_namespace},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


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


def _layout(plugin_id: str, skill_namespace: str, home_path: Path | None = None) -> _Layout:
    home = Path(home_path if home_path is not None else get_hermes_home()).expanduser().absolute()
    namespace = _plugin_namespace(plugin_id, skill_namespace)
    try:
        mkdir_under_hermes_home(home)
        # Portable Agent Plugins and native plugins occupy structurally disjoint trees.  A native
        # plugin whose id happens to equal a generated portable namespace therefore cannot open the
        # portable plugin's lease database or workspace directory.
        data_dir = (
            home / "plugin-data" / ".portable-workspaces" / namespace
            if skill_namespace
            else home / "plugin-data" / namespace
        )
        workspaces = data_dir / "workspaces"
        quarantine = data_dir / "workspace-quarantine"
        workspaces.mkdir(parents=True, exist_ok=True)
        quarantine.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspacePathError(f"cannot create plugin workspace storage below {home}: {exc}") from exc
    return _Layout(
        profile_key=hermes_home_key(home),
        plugin_namespace=namespace,
        plugin_identity_digest=_plugin_identity_digest(plugin_id, skill_namespace),
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
            plugin_identity_digest TEXT NOT NULL,
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
        CREATE TABLE IF NOT EXISTS workspace_lease_operations (
            operation_id TEXT PRIMARY KEY,
            operation_kind TEXT NOT NULL CHECK (operation_kind IN ('acquire', 'reconnect')),
            workspace_id TEXT NOT NULL,
            input_lease_id TEXT,
            input_capability_hash TEXT,
            output_lease_id TEXT NOT NULL,
            output_capability_hash TEXT NOT NULL,
            plugin_identity_digest TEXT NOT NULL,
            ttl_seconds REAL NOT NULL,
            ttl_argument_mode INTEGER NOT NULL
                CHECK (ttl_argument_mode IN (0, 1)),
            result_generation INTEGER,
            state TEXT NOT NULL CHECK (state IN ('planned', 'committed', 'failed')),
            error_text TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace_lease_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )


def _connect(layout: _Layout):
    conn = open_db(
        layout.db_path,
        db_label=f"plugin-data/{layout.plugin_namespace}/workspace-leases.db",
        foreign_keys=True,
        synchronous_full=True,
        wal_lock_retries=5,
        initialize=_initialize,
    )
    try:
        # A fresh database has no identity row yet.  Serialize first-use binding so two
        # dashboard workers cannot both observe the gap and race a plain INSERT.
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            bound = conn.execute(
                "SELECT value FROM workspace_lease_metadata WHERE key='plugin_identity_digest'"
            ).fetchone()
            if bound is None:
                existing = conn.execute(
                    "SELECT DISTINCT plugin_identity_digest FROM workspace_leases"
                ).fetchall()
                # A pre-contract database containing rows has no trustworthy identity witness.
                # Refuse adoption instead of guessing which colliding plugin created it.
                if existing and any(not str(row[0] or "") for row in existing):
                    raise WorkspacePathError(
                        "workspace lease database predates plugin identity binding"
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO workspace_lease_metadata(key, value) VALUES "
                    "('plugin_identity_digest', ?)",
                    (layout.plugin_identity_digest,),
                )
                bound = conn.execute(
                    "SELECT value FROM workspace_lease_metadata "
                    "WHERE key='plugin_identity_digest'"
                ).fetchone()
            if bound is None:
                raise WorkspacePathError("workspace lease database identity binding failed")
            bound_value = str(bound[0])
            if not hmac.compare_digest(bound_value, layout.plugin_identity_digest):
                raise WorkspacePathError("workspace lease database belongs to another plugin identity")
        return conn
    except BaseException:
        conn.close()
        raise


def _host_instance() -> str:
    """Boot/container witness; paired with pid/create-time to survive persisted rows across reboot."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if boot_id:
            return f"boot-id:{boot_id}"
    except OSError:
        pass
    # Wall-clock boot timestamps can jump when the clock is corrected and are therefore not
    # identity evidence.  Hosts without Linux's boot UUID rely on PID/create-time matching.
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


def _handle(lease_id: str, capability: str) -> dict[str, Any]:
    return {"contract_version": HANDLE_VERSION, "lease_id": lease_id, "capability": capability}


def _intent(operation_id: str, output_capability: str) -> dict[str, Any]:
    return {
        "contract_version": INTENT_VERSION,
        "operation_id": operation_id,
        "output_capability": output_capability,
    }


def _parse_intent(intent: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(intent, Mapping) or set(intent) != {
        "contract_version", "operation_id", "output_capability",
    }:
        raise InvalidWorkspaceIntentError("malformed workspace operation intent")
    if intent.get("contract_version") != INTENT_VERSION:
        raise InvalidWorkspaceIntentError("unsupported workspace operation intent version")
    operation_id, output_capability = intent.get("operation_id"), intent.get("output_capability")
    try:
        uuid.UUID(str(operation_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidWorkspaceIntentError("malformed workspace operation intent") from exc
    if not isinstance(output_capability, str) or not 32 <= len(output_capability) <= 256:
        raise InvalidWorkspaceIntentError("malformed workspace operation intent")
    return str(operation_id), output_capability


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


def _validated_row(conn, layout: _Layout, handle: Mapping[str, Any]):
    lease_id, capability = _parse_handle(handle)
    row = conn.execute("SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)).fetchone()
    if row is None or not hmac.compare_digest(str(row["capability_hash"]), _capability_hash(capability)):
        raise InvalidWorkspaceHandleError("workspace lease handle is invalid or stale")
    if (
        row["plugin_namespace"] != layout.plugin_namespace
        or row["profile_key"] != layout.profile_key
        or not hmac.compare_digest(
            str(row["plugin_identity_digest"]), layout.plugin_identity_digest,
        )
    ):
        raise InvalidWorkspaceHandleError("workspace lease handle belongs to another plugin or profile")
    expected = layout.workspaces_dir / str(row["workspace_id"])
    if os.path.normcase(str(expected)) != os.path.normcase(str(row["workspace_path"])):
        raise InvalidWorkspaceHandleError("workspace lease has an invalid persisted path")
    return row


def _validate_operation(
    operation: Mapping[str, Any],
    layout: _Layout,
    *,
    operation_kind: str,
    workspace_id: str,
    output_capability: str,
    input_handle: Mapping[str, Any] | None = None,
    ttl_seconds: float | None = None,
    ttl_seconds_provided: bool | None = None,
) -> None:
    input_lease_id, input_capability_hash = None, None
    if input_handle is not None:
        input_lease_id, input_capability = _parse_handle(input_handle)
        input_capability_hash = _capability_hash(input_capability)
    expected = {
        "operation_kind": operation_kind,
        "workspace_id": workspace_id,
        "input_lease_id": input_lease_id,
        "input_capability_hash": input_capability_hash,
    }
    if any(operation[key] != value for key, value in expected.items()):
        raise InvalidWorkspaceIntentError("workspace operation intent was reused for another request")
    if not hmac.compare_digest(
        str(operation["output_capability_hash"]), _capability_hash(output_capability),
    ):
        raise InvalidWorkspaceIntentError("workspace operation intent capability does not match")
    if not hmac.compare_digest(
        str(operation["plugin_identity_digest"]), layout.plugin_identity_digest,
    ):
        raise InvalidWorkspaceIntentError("workspace operation intent belongs to another plugin")
    stored_ttl_mode = operation["ttl_argument_mode"]
    if (
        ttl_seconds_provided is not None
        and bool(stored_ttl_mode) != ttl_seconds_provided
    ):
        raise InvalidWorkspaceIntentError(
            "workspace operation intent was reused with another TTL mode"
        )
    if ttl_seconds is not None and float(operation["ttl_seconds"]) != float(ttl_seconds):
        raise InvalidWorkspaceIntentError("workspace operation intent was reused with another TTL")


def _require_workspace_directory(path: Path) -> None:
    """Check ordinary cooperative-host lifecycle damage, not hostile path substitution."""
    if not path.is_dir():
        raise WorkspacePathError(f"workspace path is unavailable: {path}")


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


def _classify_workspace(path: Path) -> dict[str, Any]:
    """Delete only an empty tree; preserve every non-empty or uncertain tree."""
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
        return {
            "classification": "nonempty", "safe_to_delete": False,
            "checked_at": checked_at,
        }
    except OSError as exc:
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
            if assessment["classification"] == "missing":
                pass
            elif detached.is_dir() and not detached.is_symlink():
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


def _receipt_path(layout: _Layout, cleanup: Mapping[str, Any]) -> Path | None:
    raw = cleanup.get("detached_path") or cleanup.get("planned_detached_path")
    if not isinstance(raw, str):
        return None
    path = Path(raw)
    if path.parent != layout.quarantine_dir:
        raise InvalidWorkspaceHandleError("workspace cleanup receipt escaped quarantine")
    return path


def _recover_transition(conn, layout: _Layout, row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Finish filesystem work described by a stale durable transition receipt."""
    state = str(row["state"])
    cleanup = json.loads(row["cleanup_json"] or "{}")
    if not isinstance(cleanup, dict):
        raise InvalidWorkspaceHandleError("workspace cleanup receipt is malformed")
    planned = _receipt_path(layout, cleanup)
    recovered: list[dict[str, Any]] = []

    if state == "preparing":
        # A crash can leave both the predecessor at the planned detached name and
        # the unpublished replacement at the canonical name. Preserve any
        # non-empty replacement before finishing the predecessor receipt.
        if planned is not None and (planned.exists() or planned.is_symlink()):
            canonical = Path(str(row["workspace_path"]))
            if canonical.exists() or canonical.is_symlink():
                recovery_target = _planned_detached_path(
                    layout, str(row["workspace_id"]), str(row["lease_id"]),
                )
                detached, receipt = _detach_workspace(
                    layout, str(row["workspace_id"]), str(row["lease_id"]),
                    planned=recovery_target,
                )
                recovered.append(
                    _finish_detached_cleanup(detached, receipt) if detached is not None else receipt
                )
            recovered.append(_finish_detached_cleanup(planned, cleanup))
        _event(conn, row, "preparation_recovered", {"cleanup": recovered})
        return recovered

    if state == "releasing":
        if planned is None:
            raise InvalidWorkspaceHandleError("workspace release receipt is malformed")
        detached, receipt = _detach_workspace(
            layout, str(row["workspace_id"]), str(row["lease_id"]), planned=planned,
        )
        if detached is None and receipt.get("disposition") == "preserved":
            raise WorkspacePathError(
                "cannot recover workspace release; existing contents were preserved"
            )
        finished = _finish_detached_cleanup(detached, receipt) if detached is not None else receipt
        finished_at = time.time()
        conn.execute(
            """UPDATE workspace_leases SET state='released', released_at=?, cleanup_json=?,
               updated_at=? WHERE lease_id=? AND state='releasing' AND generation=?""",
            (
                finished_at, json.dumps(finished, sort_keys=True), finished_at,
                row["lease_id"], row["generation"],
            ),
        )
        updated = conn.execute(
            "SELECT * FROM workspace_leases WHERE lease_id=?", (row["lease_id"],),
        ).fetchone()
        _event(conn, updated, "release_recovered", {"cleanup": finished})
        return [finished]

    if state == "released" and planned is not None and (
        cleanup.get("disposition") in {"detached", "pending", "releasing"}
    ):
        finished = _finish_detached_cleanup(planned, cleanup)
        conn.execute(
            "UPDATE workspace_leases SET cleanup_json=?, updated_at=? WHERE lease_id=?",
            (json.dumps(finished, sort_keys=True), time.time(), row["lease_id"]),
        )
        updated = conn.execute(
            "SELECT * FROM workspace_leases WHERE lease_id=?", (row["lease_id"],),
        ).fetchone()
        _event(conn, updated, "cleanup_recovered", {"cleanup": finished})
        return [finished]
    return recovered


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

    def new_intent(self) -> dict[str, Any]:
        """Return a caller-persisted idempotency intent for one acquire or reconnect call."""
        return _intent(str(uuid.uuid4()), secrets.token_urlsafe(32))

    def acquire(
        self,
        workspace_id: str,
        *,
        intent: Mapping[str, Any],
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        workspace_id = _validated_workspace_id(workspace_id)
        with _generation_lock(self._layout(), workspace_id):
            return self._acquire_locked(
                workspace_id, intent=intent, ttl_seconds=ttl_seconds,
            )

    def _acquire_locked(
        self,
        workspace_id: str,
        *,
        intent: Mapping[str, Any],
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        workspace_id, ttl = _validated_workspace_id(workspace_id), _ttl(ttl_seconds)
        layout = self._layout()
        operation_id, capability = _parse_intent(intent)
        lease_id, planned_detached = "", None
        generation, reclaimed, recovered = 1, None, []
        preparation_error: WorkspacePathError | None = None
        with transaction(_connect(layout), immediate=True) as conn:
            now = time.time()
            pid, created, host, instance = _owner_stamp()
            operation = conn.execute(
                "SELECT * FROM workspace_lease_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if operation is not None:
                _validate_operation(
                    operation,
                    layout,
                    operation_kind="acquire",
                    workspace_id=workspace_id,
                    output_capability=capability,
                    ttl_seconds=ttl,
                )
                lease_id = str(operation["output_lease_id"])
                if operation["state"] == "committed":
                    _validated_row(conn, layout, _handle(lease_id, capability))
                    return _handle(lease_id, capability)
                if operation["state"] == "failed":
                    raise WorkspacePathError(
                        str(operation["error_text"] or "workspace acquisition previously failed")
                    )
                current = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,),
                ).fetchone()
                if current is None or current["state"] != "preparing":
                    raise InvalidWorkspaceIntentError(
                        "planned workspace acquisition has no matching preparation"
                    )
                if not hmac.compare_digest(
                    str(current["plugin_identity_digest"]), layout.plugin_identity_digest,
                ):
                    raise InvalidWorkspaceIntentError(
                        "planned workspace acquisition belongs to another plugin"
                    )
                owner_status = _owner_status(current)
                if owner_status not in {"self", "dead"}:
                    raise WorkspaceInUseError(
                        f"workspace {workspace_id!r} is being prepared by another live owner"
                    )
                ttl = float(operation["ttl_seconds"])
                generation = int(current["generation"])
                cleanup = json.loads(current["cleanup_json"] or "{}")
                planned_detached = _receipt_path(layout, cleanup)
                if planned_detached is None:
                    raise InvalidWorkspaceIntentError(
                        "planned workspace acquisition has a malformed cleanup receipt"
                    )
                recovered = list(cleanup.get("recovered_cleanup") or [])
                # A process may die after moving the predecessor to the planned name and
                # creating the canonical directory, but before the activation transaction
                # commits.  Reconcile both names under the same durable intent, then resume.
                resumed_recovery = _recover_transition(conn, layout, current)
                if resumed_recovery:
                    recovered.extend(resumed_recovery)
                    cleanup["recovered_cleanup"] = recovered
                reclaimed = cleanup.get("reclaimed")
                conn.execute(
                    """UPDATE workspace_leases SET owner_pid=?, owner_create_time=?, owner_host=?,
                       owner_instance=?, heartbeat_at=?, expires_at=?, cleanup_json=?, updated_at=?
                       WHERE lease_id=? AND state='preparing' AND generation=?""",
                    (
                        pid, created, host, instance, now, now + ttl,
                        json.dumps(cleanup, sort_keys=True), now,
                        lease_id, generation,
                    ),
                )
            else:
                lease_id = str(uuid.uuid4())
                planned_detached = _planned_detached_path(layout, workspace_id, lease_id)
                old = conn.execute(
                    "SELECT * FROM workspace_leases WHERE workspace_id=?", (workspace_id,)
                ).fetchone()
                if old is not None:
                    generation = int(old["generation"]) + 1
                    if (
                        old["plugin_namespace"] != layout.plugin_namespace
                        or old["profile_key"] != layout.profile_key
                        or not hmac.compare_digest(
                            str(old["plugin_identity_digest"]), layout.plugin_identity_digest,
                        )
                    ):
                        raise WorkspacePathError(
                            "workspace lease database belongs to another plugin or profile"
                        )
                    if old["state"] in {"active", "preparing", "releasing"}:
                        owner_status = _owner_status(old)
                        # TTL is a liveness signal for the current owner, never proof that a
                        # different or unverifiable live process may be displaced.
                        if owner_status != "dead":
                            raise WorkspaceInUseError(
                                f"workspace {workspace_id!r} is already owned by a live process"
                            )
                        reclaimed = "owner_dead"
                    recovered = _recover_transition(conn, layout, old)
                cleanup = {
                    "classification": "pending", "disposition": "preparing",
                    "original_path": str(layout.workspaces_dir / workspace_id),
                    "planned_detached_path": str(planned_detached),
                    "reclaimed": reclaimed,
                }
                if recovered:
                    cleanup["recovered_cleanup"] = recovered
                conn.execute(
                    """INSERT INTO workspace_lease_operations
                       (operation_id, operation_kind, workspace_id, input_lease_id,
                        input_capability_hash, output_lease_id, output_capability_hash,
                        plugin_identity_digest, ttl_seconds, ttl_argument_mode,
                        result_generation, state,
                        error_text, created_at, updated_at)
                       VALUES (?, 'acquire', ?, NULL, NULL, ?, ?, ?, ?, 1, NULL, 'planned',
                               NULL, ?, ?)""",
                    (
                        operation_id, workspace_id, lease_id, _capability_hash(capability),
                        layout.plugin_identity_digest, ttl, now, now,
                    ),
                )
                row_values = (
                    workspace_id, lease_id, _capability_hash(capability), HANDLE_VERSION,
                    "preparing", layout.plugin_namespace, layout.plugin_identity_digest,
                    layout.profile_key, str(layout.workspaces_dir / workspace_id), pid, created,
                    host, instance, ttl, now, now, now + ttl, None, generation,
                    json.dumps(cleanup, sort_keys=True), now,
                )
                conn.execute(
                    """INSERT OR REPLACE INTO workspace_leases
                       (workspace_id, lease_id, capability_hash, contract_version, state,
                        plugin_namespace, plugin_identity_digest, profile_key, workspace_path,
                        owner_pid, owner_create_time, owner_host, owner_instance, ttl_seconds,
                        acquired_at, heartbeat_at, expires_at, released_at, generation,
                        cleanup_json, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    row_values,
                )
                row = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,),
                ).fetchone()
                _event(conn, row, "preparing", {
                    "operation_id": operation_id,
                    "reclaimed": reclaimed,
                    "cleanup": cleanup,
                    "recovered_cleanup": recovered,
                })

        # The preparing row is now durable.  Hold another IMMEDIATE transaction across the
        # canonical-name handoff; a crash/commit failure leaves a reclaimable preparing generation,
        # never the predecessor row pointing at a replacement directory.
        with transaction(_connect(layout), immediate=True) as conn:
            operation = conn.execute(
                "SELECT * FROM workspace_lease_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise InvalidWorkspaceIntentError("workspace acquisition intent receipt is missing")
            _validate_operation(
                operation,
                layout,
                operation_kind="acquire",
                workspace_id=workspace_id,
                output_capability=capability,
                ttl_seconds=ttl,
            )
            if operation["state"] == "committed":
                _validated_row(conn, layout, _handle(lease_id, capability))
                return _handle(lease_id, capability)
            if operation["state"] == "failed":
                raise WorkspacePathError(
                    str(operation["error_text"] or "workspace acquisition previously failed")
                )
            current = _validated_row(conn, layout, _handle(lease_id, capability))
            if current["state"] != "preparing" or int(current["generation"]) != generation:
                raise InvalidWorkspaceHandleError(
                    "workspace lease changed while it was being prepared"
                )
            detached, cleanup = _detach_workspace(
                layout, workspace_id, lease_id, planned=planned_detached,
            )
            if recovered:
                cleanup["recovered_cleanup"] = recovered
            path = layout.workspaces_dir / workspace_id
            if detached is None and cleanup["disposition"] == "preserved":
                preparation_error = WorkspacePathError(
                    f"cannot detach occupied workspace {path}; existing contents were preserved"
                )
            if preparation_error is None:
                try:
                    path.mkdir()
                except OSError as exc:
                    preparation_error = WorkspacePathError(
                        f"cannot create workspace {path}: {exc}"
                    )
            if detached is not None:
                cleanup = _finish_detached_cleanup(detached, cleanup)
            if preparation_error is not None:
                cleanup.update({
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
                conn.execute(
                    """UPDATE workspace_lease_operations
                       SET state='failed', error_text=?, result_generation=?, updated_at=?
                       WHERE operation_id=? AND state='planned'""",
                    (
                        str(preparation_error), generation, time.time(), operation_id,
                    ),
                )
                _event(conn, failed, "preparation_failed", {"cleanup": cleanup})
            else:
                activated_at = time.time()
                conn.execute(
                    """UPDATE workspace_leases SET state='active', cleanup_json=?, heartbeat_at=?,
                       expires_at=?, updated_at=?
                       WHERE lease_id=? AND state='preparing' AND generation=?""",
                    (
                        json.dumps(cleanup, sort_keys=True), activated_at,
                        activated_at + ttl, activated_at, lease_id, generation,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise InvalidWorkspaceHandleError(
                        "workspace lease changed while it was being prepared"
                    )
                active = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
                conn.execute(
                    """UPDATE workspace_lease_operations
                       SET state='committed', result_generation=?, updated_at=?
                       WHERE operation_id=? AND state='planned'""",
                    (generation, activated_at, operation_id),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise InvalidWorkspaceIntentError(
                        "workspace acquisition intent changed during activation"
                    )
                _event(conn, active, "acquired", {
                    "operation_id": operation_id,
                    "reclaimed": reclaimed,
                    "cleanup": cleanup,
                })

        if preparation_error is not None:
            raise preparation_error
        return _handle(lease_id, capability)

    def renew(
        self, handle: Mapping[str, Any], *, ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        layout = self._layout()
        with transaction(_connect(layout), immediate=True) as conn:
            row = _validated_row(conn, layout, handle)
            now = time.time()
            if row["state"] != "active":
                raise InvalidWorkspaceHandleError("workspace lease has been released")
            _require_workspace_directory(Path(row["workspace_path"]))
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
        self,
        handle: Mapping[str, Any],
        *,
        intent: Mapping[str, Any],
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        workspace_id = self._workspace_id_for_reconnect(handle, intent)
        with _generation_lock(self._layout(), workspace_id):
            return self._reconnect_locked(
                handle, intent=intent, ttl_seconds=ttl_seconds,
            )

    def _reconnect_locked(
        self,
        handle: Mapping[str, Any],
        *,
        intent: Mapping[str, Any],
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        layout = self._layout()
        ttl_was_provided = ttl_seconds is not None
        operation_id, successor_capability = _parse_intent(intent)
        previous_lease_id, previous_capability = _parse_handle(handle)
        with transaction(_connect(layout), immediate=True) as conn:
            operation = conn.execute(
                "SELECT * FROM workspace_lease_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if operation is not None:
                _validate_operation(
                    operation,
                    layout,
                    operation_kind="reconnect",
                    workspace_id=str(operation["workspace_id"]),
                    output_capability=successor_capability,
                    input_handle=handle,
                    ttl_seconds=None if ttl_seconds is None else _ttl(ttl_seconds),
                    ttl_seconds_provided=ttl_was_provided,
                )
                if operation["state"] != "committed":
                    raise InvalidWorkspaceIntentError(
                        "workspace reconnect intent has no committed result"
                    )
                successor_lease_id = str(operation["output_lease_id"])
                _validated_row(
                    conn, layout, _handle(successor_lease_id, successor_capability),
                )
                return _handle(successor_lease_id, successor_capability)

            row = _validated_row(conn, layout, handle)
            now = time.time()
            if row["state"] != "active":
                raise InvalidWorkspaceHandleError("workspace lease has been released")
            _require_workspace_directory(Path(row["workspace_path"]))
            owner, expired = _owner_state(row), float(row["expires_at"]) <= now
            if owner is None:
                raise WorkspaceOwnershipError("workspace lease still belongs to another live process")
            ttl = _ttl(row["ttl_seconds"] if ttl_seconds is None else ttl_seconds)
            _event(conn, row, "reconnect_started", {
                "operation_id": operation_id, "expired": expired,
            })
            successor_lease_id = str(uuid.uuid4())
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
            conn.execute(
                """INSERT INTO workspace_lease_operations
                   (operation_id, operation_kind, workspace_id, input_lease_id,
                    input_capability_hash, output_lease_id, output_capability_hash,
                    plugin_identity_digest, ttl_seconds, ttl_argument_mode,
                    result_generation, state, error_text, created_at, updated_at)
                   VALUES (?, 'reconnect', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed',
                           NULL, ?, ?)""",
                (
                    operation_id, row["workspace_id"], previous_lease_id,
                    _capability_hash(previous_capability), successor_lease_id,
                    _capability_hash(successor_capability), layout.plugin_identity_digest,
                    ttl, int(ttl_was_provided), int(updated["generation"]), now, now,
                ),
            )
            _event(conn, updated, "reconnected", {
                "operation_id": operation_id,
                "predecessor_lease_id": previous_lease_id,
                "previous_owner": "dead" if owner is False else "expired" if expired else "self",
            })
            return _handle(successor_lease_id, successor_capability)

    def inspect(self, handle: Mapping[str, Any]) -> dict[str, Any]:
        layout = self._layout()
        # Inspection is generation-fenced with the same writer lock as release/reconnect.
        # Otherwise a reader can validate generation N, let a replacement generation publish,
        # and then return the stale row paired with the replacement directory/events.
        with transaction(_connect(layout), immediate=True) as conn:
            row = _validated_row(conn, layout, handle)
            now = time.time()
            if row["state"] != "active":
                raise InvalidWorkspaceHandleError("workspace lease has been released")
            _require_workspace_directory(Path(row["workspace_path"]))
            if float(row["expires_at"]) <= now:
                raise WorkspaceLeaseExpiredError("workspace lease expired; reconnect it before use")
            return _public_snapshot(conn, row)

    def release(self, handle: Mapping[str, Any]) -> dict[str, Any]:
        workspace_id = self._workspace_id_for_handle(handle)
        with _generation_lock(self._layout(), workspace_id):
            return self._release_locked(handle)

    def _release_locked(self, handle: Mapping[str, Any]) -> dict[str, Any]:
        layout = self._layout()
        with transaction(_connect(layout), immediate=True) as conn:
            row = _validated_row(conn, layout, handle)
            now = time.time()
            if row["state"] == "released":
                _recover_transition(conn, layout, row)
                row = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (row["lease_id"],),
                ).fetchone()
                return _public_snapshot(conn, row)
            if row["state"] not in {"active", "releasing"}:
                raise InvalidWorkspaceHandleError("workspace lease is not active")
            owner_status = _owner_status(row)
            if owner_status in {"live", "unknown"}:
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
                planned = _receipt_path(layout, cleanup)
                if planned is None:
                    raise InvalidWorkspaceHandleError("workspace release receipt is malformed")

        # The releasing state and planned rename are durable before filesystem mutation.  A retry after
        # process death resumes the same target; a commit failure cannot resurrect an active predecessor.
        with transaction(_connect(layout), immediate=True) as conn:
            current = _validated_row(conn, layout, handle)
            if current["state"] == "released":
                return _public_snapshot(conn, current)
            if current["state"] != "releasing" or current["generation"] != row["generation"]:
                raise InvalidWorkspaceHandleError("workspace lease changed during release")
            detached, cleanup = _detach_workspace(
                layout, current["workspace_id"], current["lease_id"], planned=planned,
            )
            if detached is None and cleanup.get("disposition") == "preserved":
                raise WorkspacePathError(
                    "cannot detach workspace during release; existing contents were preserved"
                )
            if detached is not None:
                cleanup = _finish_detached_cleanup(detached, cleanup)
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
            return _public_snapshot(conn, updated)

    def _workspace_id_for_handle(self, handle: Mapping[str, Any]) -> str:
        """Resolve the lock key from a validated handle without exposing a path."""
        layout = self._layout()
        with transaction(_connect(layout), immediate=True) as conn:
            row = _validated_row(conn, layout, handle)
            return str(row["workspace_id"])

    def _workspace_id_for_reconnect(
        self, handle: Mapping[str, Any], intent: Mapping[str, Any],
    ) -> str:
        """Resolve the generation lock for a request or its committed replay."""
        layout = self._layout()
        operation_id, output_capability = _parse_intent(intent)
        input_lease_id, input_capability = _parse_handle(handle)
        with transaction(_connect(layout), immediate=True) as conn:
            try:
                row = _validated_row(conn, layout, handle)
                return str(row["workspace_id"])
            except InvalidWorkspaceHandleError:
                operation = conn.execute(
                    "SELECT * FROM workspace_lease_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if operation is None:
                    raise
                if (
                    operation["operation_kind"] != "reconnect"
                    or operation["input_lease_id"] != input_lease_id
                    or not hmac.compare_digest(
                        str(operation["input_capability_hash"] or ""),
                        _capability_hash(input_capability),
                    )
                    or not hmac.compare_digest(
                        str(operation["output_capability_hash"]),
                        _capability_hash(output_capability),
                    )
                    or not hmac.compare_digest(
                        str(operation["plugin_identity_digest"]),
                        layout.plugin_identity_digest,
                    )
                ):
                    raise InvalidWorkspaceIntentError(
                        "workspace reconnect intent does not match its predecessor"
                    )
                return str(operation["workspace_id"])

    @contextmanager
    def _pin_dispatch(
        self,
        handle: Mapping[str, Any],
        *,
        minimum_ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ):
        """Fence a synchronous dispatch against release/reconnect/reacquire.

        The process-local generation lock remains held for the whole operation. The
        database validation happens again after acquiring it, so a lifecycle change
        between lock-key lookup and acquisition can only make the handle stale.
        """
        layout = self._layout()
        workspace_id = self._workspace_id_for_handle(handle)
        with _generation_lock(layout, workspace_id):
            with transaction(_connect(layout), immediate=True) as conn:
                row = _validated_row(conn, layout, handle)
                now = time.time()
                if row["state"] != "active":
                    raise InvalidWorkspaceHandleError("workspace lease is not active")
                if float(row["expires_at"]) <= now:
                    raise WorkspaceLeaseExpiredError(
                        "workspace lease expired; reconnect it before use"
                    )
                if _owner_state(row) is not True:
                    raise WorkspaceOwnershipError(
                        "workspace lease is not owned by the current process"
                    )
                path = Path(str(row["workspace_path"]))
                _require_workspace_directory(path)
                generation = int(row["generation"])
                # A dispatch is lease activity. Give the synchronous operation a
                # complete validity window before releasing the database lock;
                # cross-process lifecycle calls still apply their owner fencing.
                dispatch_ttl = max(
                    float(row["ttl_seconds"]), _ttl(minimum_ttl_seconds),
                )
                conn.execute(
                    "UPDATE workspace_leases SET ttl_seconds=?, heartbeat_at=?, "
                    "expires_at=?, updated_at=? WHERE lease_id=? AND state='active' "
                    "AND generation=?",
                    (
                        dispatch_ttl,
                        now,
                        now + dispatch_ttl,
                        now,
                        row["lease_id"],
                        generation,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise InvalidWorkspaceHandleError(
                        "workspace lease changed while dispatch was pinned"
                    )
                task_material = "\0".join((
                    layout.profile_key,
                    layout.plugin_identity_digest,
                    workspace_id,
                    str(generation),
                ))
                task_id = "plugin-workspace-" + hashlib.sha256(
                    task_material.encode("utf-8")
                ).hexdigest()[:32]
            yield _PinnedWorkspace(
                path=path, task_id=task_id, generation=generation,
            )


__all__ = [
    "DEFAULT_TTL_SECONDS", "HANDLE_VERSION", "HOST_FEATURE", "INTENT_VERSION",
    "PluginWorkspaces", "InvalidWorkspaceHandleError", "InvalidWorkspaceIntentError",
    "WorkspaceInUseError", "WorkspaceLeaseError", "WorkspaceLeaseExpiredError",
    "WorkspaceOwnershipError", "WorkspacePathError",
]
