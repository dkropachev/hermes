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
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home, hermes_home_key, mkdir_under_hermes_home
from hermes_cli.plugin_workspace_errors import (
    InvalidWorkspaceHandleError, WorkspaceDurabilityError, WorkspaceInUseError,
    WorkspaceLeaseError, WorkspaceLeaseExpiredError, WorkspaceOwnershipError,
    WorkspacePathError,
)
from hermes_cli.plugin_workspace_contract import (
    DEFAULT_TTL_SECONDS, HANDLE_VERSION, MAX_TTL_SECONDS, MIN_TTL_SECONDS,
    capability_hash as _capability_hash, handle as _handle, intent as _intent,
    native_hashed_namespace as _native_hashed_namespace,
    operation_fingerprint as _operation_fingerprint, parse_handle as _parse_handle,
    parse_intent as _parse_intent, plugin_identity as _plugin_identity,
    plugin_namespace as _plugin_namespace, ttl as _ttl,
    validate_operation as _validate_operation,
    validated_workspace_id as _validated_workspace_id,
)
from hermes_cli.plugin_workspace_cleanup import (
    classify_workspace as _classify_workspace, planned_name as _planned_detached_name,
    render_cleanup_paths as _cleanup_paths,
)
from hermes_cli.plugin_workspace_fs import (
    HeldDirectory as _HeldDirectory, HeldRegularFile as _HeldRegularFile,
)
from hermes_cli.plugin_workspace_registry import (
    OUTER_MARKER_NAME, inner_marker_name, open_storage_anchors,
    registry_binding_name, registry_paths,
)
from hermes_cli.process_identity import _pid_alive_matches, _process_create_time
from hermes_cli.sqlite_util import add_column_if_missing, transaction


HOST_FEATURE = "workspace_leases.v1"
_WORKSPACE_LOCKS: dict[str, threading.RLock] = {}
_WORKSPACE_LOCKS_GUARD = threading.Lock()
_PROCESS_OBSERVER_NONCE = secrets.token_hex(16)


@dataclass
class _Layout:
    home: Path
    profile_key: str
    plugin_namespace: str
    plugin_identity: str
    plugin_data_dir: Path
    data_dir: Path
    registry_root: Path
    registry_dir: Path
    workspaces_dir: Path
    quarantine_dir: Path
    db_path: Path
    outer_marker_path: Path
    inner_marker_path: Path
    registry_binding_path: Path


class _AnchoredConnection:
    """SQLite connection retaining the held data root and no-follow DB leaf until close."""

    def __init__(self, conn, anchors, held_db: _HeldRegularFile, close_validator) -> None:
        self._conn = conn
        self._anchors = anchors
        self._held_db = held_db
        self._close_validator = close_validator
        self.storage_data_identity = anchors.data.identity_json()
        self.storage_db_identity = [int(part) for part in held_db.identity]
        self.storage_roots = (anchors.workspaces, anchors.quarantine)
        self.storage_root_identities = anchors.identities()
        self.workspace_base = anchors.layout.workspaces_dir
        self.layout = anchors.layout

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)

    def close(self) -> None:
        failure = None
        try:
            self._conn.close()
        except BaseException as exc:
            failure = exc
        try:
            self._close_validator()
        except BaseException as exc:
            if failure is None:
                failure = exc
        finally:
            try:
                self._held_db.close()
            finally:
                self._anchors.__exit__(None, None, None)
        if failure is not None:
            raise failure


@contextmanager
def _workspace_lock(layout: _Layout, workspace_id: str):
    key = os.path.normcase(str(layout.workspaces_dir / workspace_id))
    with _WORKSPACE_LOCKS_GUARD:
        lock = _WORKSPACE_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def _layout(plugin_id: str, skill_namespace: str, home_path: Path | None = None) -> _Layout:
    raw_home = Path(home_path if home_path is not None else get_hermes_home()).expanduser()
    mkdir_under_hermes_home(raw_home)
    try:
        home = raw_home.resolve(strict=True)
    except OSError as exc:
        raise WorkspacePathError(f"cannot resolve HERMES_HOME {raw_home}: {exc}") from exc
    namespace = _plugin_namespace(plugin_id, skill_namespace)
    identity = _plugin_identity(plugin_id, skill_namespace)
    plugin_data = home / "plugin-data"
    data_dir = plugin_data / namespace
    registry_root, registry_dir, db_path = registry_paths(home, identity)
    layout = _Layout(
        home=home,
        profile_key=hermes_home_key(home),
        plugin_namespace=namespace,
        plugin_identity=identity,
        plugin_data_dir=plugin_data,
        data_dir=data_dir,
        registry_root=registry_root,
        registry_dir=registry_dir,
        workspaces_dir=data_dir / "workspaces",
        quarantine_dir=data_dir / "workspace-quarantine",
        db_path=db_path,
        outer_marker_path=home / OUTER_MARKER_NAME,
        inner_marker_path=plugin_data / inner_marker_name(identity),
        registry_binding_path=registry_root / registry_binding_name(identity),
    )
    return layout


@contextmanager
def _workspace_roots(layout: _Layout):
    with open_storage_anchors(layout, include_roots=True, publish=False) as anchors:
        yield anchors.workspaces, anchors.quarantine


def _validate_legacy_rows(conn, layout: _Layout) -> None:
    rows = conn.execute(
        """SELECT workspace_id, plugin_namespace, plugin_identity, profile_key, workspace_path
           FROM workspace_leases"""
    ).fetchall()
    invalid: list[str] = []
    for row in rows:
        workspace_id = row["workspace_id"]
        try:
            _validated_workspace_id(workspace_id)
        except ValueError:
            invalid.append(f"workspace_id={workspace_id!r}")
            continue
        expected_path = f"workspaces/{workspace_id}"
        legacy_path = str(layout.workspaces_dir / workspace_id)
        identity = row["plugin_identity"]
        if (
            row["plugin_namespace"] != layout.plugin_namespace
            or row["profile_key"] not in (layout.profile_key, hermes_home_key(layout.home))
            or row["workspace_path"] not in (expected_path, legacy_path)
            or identity not in (None, layout.plugin_identity)
        ):
            invalid.append(f"workspace_id={workspace_id!r}")
    if invalid:
        raise WorkspacePathError(
            "refusing to claim legacy workspace lease rows that do not match this "
            f"plugin/profile/layout: {', '.join(invalid[:5])}"
        )
    conn.execute(
        """UPDATE workspace_leases SET plugin_identity=COALESCE(plugin_identity, ?),
           profile_key=?, workspace_path='workspaces/' || workspace_id""",
        (layout.plugin_identity, layout.profile_key),
    )


def _initialize(conn, layout: _Layout, root_identities: Mapping[str, Any]) -> None:
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
            owner_machine_identity TEXT,
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
            root_identities_json TEXT,
            workspace_identity_json TEXT,
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
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(workspace_leases)")}
        additions = {
            "plugin_identity": "plugin_identity TEXT",
            "owner_machine_identity": "owner_machine_identity TEXT",
            "acquire_intent_id": "acquire_intent_id TEXT",
            "root_identities_json": "root_identities_json TEXT",
            "workspace_identity_json": "workspace_identity_json TEXT",
            "heartbeat_monotonic": "heartbeat_monotonic REAL",
            "expires_monotonic": "expires_monotonic REAL",
            "expiry_observer": "expiry_observer TEXT",
            "expiry_observed_monotonic": "expiry_observed_monotonic REAL",
        }
        for column, ddl in additions.items():
            if column not in columns:
                add_column_if_missing(conn, "workspace_leases", column, ddl)
        _validate_legacy_rows(conn, layout)
        for operation in conn.execute(
            """SELECT operation_id, kind, workspace_id, ttl_seconds, input_lease_id
               FROM workspace_lease_operations"""
        ).fetchall():
            conn.execute(
                """UPDATE workspace_lease_operations SET request_fingerprint=?
                   WHERE operation_id=?""",
                (
                    _operation_fingerprint(
                        layout, operation["kind"], operation["workspace_id"],
                        float(operation["ttl_seconds"]), operation["input_lease_id"],
                    ),
                    operation["operation_id"],
                ),
            )
        legacy_roots = json.dumps(dict(root_identities), sort_keys=True)
        conn.execute(
            """UPDATE workspace_leases SET root_identities_json=?
               WHERE root_identities_json IS NULL""",
            (legacy_roots,),
        )
        legacy_leaves = conn.execute(
            """SELECT workspace_id, state FROM workspace_leases
               WHERE workspace_identity_json IS NULL AND state='active'"""
        ).fetchall()
        for legacy in legacy_leaves:
            workspace_id = str(legacy["workspace_id"])
            if not conn.storage_roots[0].exists(workspace_id):
                if legacy["state"] == "active":
                    raise WorkspacePathError(
                        f"active legacy workspace path is missing: {workspace_id}"
                    )
                continue
            identity = _validate_workspace_entry(
                layout, workspace_id, conn.storage_roots[0],
            )
            conn.execute(
                """UPDATE workspace_leases SET workspace_identity_json=?
                   WHERE workspace_id=? AND workspace_identity_json IS NULL""",
                (json.dumps(identity), workspace_id),
            )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS workspace_lease_acquire_intent
               ON workspace_leases(acquire_intent_id)"""
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _preflight_existing_root_identities(conn, current: Mapping[str, Any]) -> None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_leases'"
    ).fetchone()
    if table is None:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(workspace_leases)")}
    if "root_identities_json" not in columns:
        return
    rows = conn.execute(
        """SELECT DISTINCT root_identities_json FROM workspace_leases
           WHERE root_identities_json IS NOT NULL"""
    ).fetchall()
    for row in rows:
        try:
            persisted = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise WorkspacePathError("workspace storage root identity is malformed") from exc
        if persisted != dict(current):
            raise WorkspacePathError(
                "workspace storage root identity changed before database open; "
                f"expected={persisted}, current={dict(current)}"
            )


def _connect(layout: _Layout):
    database_names = (
        "workspace-leases.db", "workspace-leases.db-wal",
        "workspace-leases.db-shm", "workspace-leases.db-journal",
    )
    anchors_cm = open_storage_anchors(layout, include_roots=False, publish=False)
    anchors = anchors_cm.__enter__()
    held_db = _HeldRegularFile(
        anchors.registry_namespace, "workspace-leases.db",
        create=anchors.binding_missing,
    )

    def validate_database_files(*, secure_modes: bool) -> None:
        for name in database_names:
            try:
                info = anchors.registry_namespace.stat(name)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkspacePathError(
                    f"workspace lease database file is unsafe: {layout.registry_dir / name}"
                )
            if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                anchors.registry_namespace.secure_regular_file(
                    name, writable=True,
                )
            elif secure_modes:
                anchors.registry_namespace.chmod(name, 0o600)
        anchors.home.verify()
        anchors.plugin_data.verify()
        anchors.data.verify()
        anchors.registry.verify()
        anchors.registry_namespace.verify()

    try:
        validate_database_files(secure_modes=False)
        if anchors.binding_missing:
            unexpected = set(anchors.registry_namespace.list_names()) - set(database_names)
            if unexpected:
                raise WorkspacePathError(
                    "unbound workspace registry namespace contains unexpected state: "
                    + ", ".join(sorted(unexpected)[:5])
                )
        if anchors.outer_missing:
            existing = [
                name for name in database_names if anchors.registry_namespace.exists(name)
            ]
            if existing:
                raise WorkspacePathError(
                    "unbound workspace registry database requires offline operator recovery; "
                    f"refusing to open while present: {', '.join(existing)}"
                )
        if anchors.outer_missing:
            anchors.publish_outer()
        anchors.ensure_roots()
        if anchors.binding_missing and (
            anchors.workspaces.list_names() or anchors.quarantine.list_names()
        ):
            raise WorkspacePathError(
                "unbound workspace registry roots are nonempty; "
                "offline operator recovery is required"
            )
        if not anchors.binding_missing and not anchors.registry_namespace.exists(
            "workspace-leases.db"
        ):
            raise WorkspacePathError("workspace registry bound database is missing")
        held_db.__enter__()
        expected_database_identity = anchors.expected_database_identity()
        if expected_database_identity is not None and (
            expected_database_identity != [int(part) for part in held_db.identity]
        ):
            raise WorkspacePathError("workspace registry ready database identity changed")

        db_open_path, _nofollow = held_db.open_path()
        raw_conn = sqlite3.connect(db_open_path, timeout=5.0)
        raw_conn.row_factory = sqlite3.Row
        raw_conn.execute("PRAGMA busy_timeout=5000")
        raw_conn.set_authorizer(
            lambda action, _arg1, _arg2, _db, _source: (
                sqlite3.SQLITE_DENY
                if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH)
                else sqlite3.SQLITE_OK
            )
        )
        if anchors.binding_missing:
            known_tables = {
                "workspace_leases", "workspace_lease_events",
                "workspace_cleanup_receipts", "workspace_lease_operations",
            }
            existing_tables = {
                str(row[0]) for row in raw_conn.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
                )
            }
            if existing_tables - known_tables or any(
                raw_conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                for table in existing_tables
            ):
                raise WorkspacePathError(
                    "unbound workspace registry database contains state; "
                    "offline operator recovery is required"
                )
        held_db.verify()
        conn = _AnchoredConnection(
            raw_conn, anchors_cm, held_db,
            lambda: (validate_database_files(secure_modes=True), held_db.verify()),
        )
        _preflight_existing_root_identities(conn, conn.storage_root_identities)
        from hermes_state_wal import apply_wal_with_fallback

        for attempt in range(5):
            try:
                apply_wal_with_fallback(
                    conn, db_label=f"plugin-data/{layout.plugin_namespace}/workspace-leases.db",
                )
                break
            except sqlite3.OperationalError as exc:
                if str(exc).lower() != "database is locked" or attempt == 4:
                    raise
                time.sleep(0.01 * (2**attempt))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        _initialize(conn, layout, conn.storage_root_identities)
        if anchors.binding_missing:
            anchors.commit_binding([int(part) for part in held_db.identity])
        elif anchors.inner_missing:
            anchors.commit_binding([int(part) for part in held_db.identity])
        validate_database_files(secure_modes=True)
        held_db.verify()
        return conn
    except BaseException:
        if "conn" in locals():
            conn.close()
        else:
            if "raw_conn" in locals():
                raw_conn.close()
            held_db.close(strict=False)
            anchors_cm.__exit__(*sys.exc_info())
        raise


def _host_instance() -> str:
    """Stable OS boot witness where available; never synthesize one from wall-clock time."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if boot_id:
            return f"boot-id:{boot_id}"
    except OSError:
        pass
    return "unverified"


def _machine_identity() -> str:
    """Hashed stable machine identity, or ``unverified`` when the OS cannot prove one."""
    raw = ""
    if sys.platform.startswith("linux"):
        for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                raw = path.read_text(encoding="ascii").strip()
            except OSError:
                continue
            if raw:
                break
    elif sys.platform == "darwin":  # pragma: no cover - exercised on macOS CI
        try:
            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=2, check=False,
            )
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout)
            raw = match.group(1).strip() if match else ""
        except (OSError, subprocess.SubprocessError):
            raw = ""
    elif os.name == "nt":  # pragma: no cover - exercised on Windows CI
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                access=winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
            ) as key:
                raw = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
        except (OSError, ImportError):
            raw = ""
    if not raw:
        return "unverified"
    digest = hashlib.sha256(raw.casefold().encode("utf-8")).hexdigest()
    return f"machine-sha256:{digest}"


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
    recorded_machine = str(
        _row_value(row, "owner_machine_identity") or "unverified"
    )
    current_machine = _machine_identity()
    if (
        _verified_instance(recorded_machine)
        and _verified_instance(current_machine)
        and recorded_machine != current_machine
    ):
        return "unknown"
    recorded_create = _row_value(row, "owner_create_time")
    if pid == os.getpid() and recorded_create is not None:
        current_create = _process_create_time()
        if current_create is None:
            return "unknown"
        return (
            "self"
            if abs(float(current_create) - float(recorded_create)) < 2.0
            else "dead"
        )
    if not (_verified_instance(recorded_machine) and _verified_instance(current_machine)):
        return "unknown"
    if recorded_machine != current_machine:
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
    return "live"


def _owner_state(row: Mapping[str, Any]) -> bool | None:
    """Compatibility-shaped owner result used by lifecycle decisions."""
    status = _owner_status(row)
    return True if status == "self" else False if status == "dead" else None


def _verified_instance(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value != "unverified"


def _expiry_observer_token() -> str:
    machine, instance = _machine_identity(), _host_instance()
    if _verified_instance(machine) and _verified_instance(instance):
        return f"boot:{machine}:{instance}"
    return f"process:{_PROCESS_OBSERVER_NONCE}"


def _fresh_expiry(ttl: float) -> tuple[float, float, float, float, str, float]:
    wall, monotonic = time.time(), time.monotonic()
    return (
        wall, wall + ttl, monotonic, monotonic + ttl,
        _expiry_observer_token(), monotonic,
    )


def _lease_expired(conn, row: Mapping[str, Any]) -> bool:
    """Canonical expiry decision: same-boot monotonic, otherwise one full local TTL observation."""
    current_machine, current_instance = _machine_identity(), _host_instance()
    owner_machine = str(
        _row_value(row, "owner_machine_identity") or "unverified"
    )
    owner_instance = str(_row_value(row, "owner_instance") or "unverified")
    now = time.monotonic()
    if (
        owner_machine == current_machine
        and _verified_instance(owner_machine)
        and _verified_instance(current_machine)
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


def _response_handle(lease_id: str, capability: str) -> dict[str, Any]:
    """Response boundary kept separate so crash-after-commit behavior is fault-testable."""
    return _handle(lease_id, capability)


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
    expected = f"workspaces/{row['workspace_id']}"
    if row["workspace_path"] != expected:
        raise InvalidWorkspaceHandleError("workspace lease has an invalid persisted path")
    if validate_path:
        _validate_workspace_entry(
            layout, str(row["workspace_id"]), conn.storage_roots[0],
            row["workspace_identity_json"],
        )
    _validate_row_roots(row, layout, current_identities=conn.storage_root_identities)
    return row


def _validate_workspace_entry(
    layout: _Layout, workspace_id: str, workspaces: _HeldDirectory,
    expected_identity_json: str | None = None,
) -> list[int]:
    try:
        info = workspaces.stat(workspace_id)
    except OSError as exc:
        raise WorkspacePathError(
            f"workspace path is unavailable: {layout.workspaces_dir / workspace_id}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspacePathError(
            f"workspace path is not a regular directory: "
            f"{layout.workspaces_dir / workspace_id}"
        )
    with workspaces.child_directory(workspace_id) as workspace:
        identity = workspace.identity_json()
    if expected_identity_json is not None:
        try:
            expected_identity = json.loads(expected_identity_json)
        except (TypeError, ValueError) as exc:
            raise WorkspacePathError("workspace leaf identity is malformed") from exc
        if identity != expected_identity:
            raise WorkspacePathError("workspace leaf identity changed")
    workspace.harden_security()
    workspaces.verify()
    return identity


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
            "at": float(row["recorded_at"]),
            "details": _cleanup_paths(
                conn.layout, workspace_id, json.loads(row["details_json"]),
                lease_id=str(row["lease_id"]), generation=int(row["generation"]),
                operation=str(row["operation"]),
            ),
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
            "details": _cleanup_paths(
                conn.layout, workspace_id, json.loads(row["details_json"]),
                lease_id=str(row["lease_id"]), generation=int(row["generation"]),
            ),
        }
        for row in reversed(rows)
    ]


def _planned_detached_path(
    layout: _Layout, workspace_id: str, lease_id: str, generation: int, operation: str,
) -> Path:
    return layout.quarantine_dir / _planned_detached_name(
        workspace_id, lease_id, generation, operation,
    )


def _preparing_cleanup(
    workspace_id: str, planned_detached: Path, root_identities: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "operation": "acquire", "classification": "pending",
        "disposition": "preparing", "original_path": f"workspaces/{workspace_id}",
        "planned_detached_name": planned_detached.name,
        "root_identities": dict(root_identities),
    }


def _root_identities(layout: _Layout) -> dict[str, Any]:
    with open_storage_anchors(layout, include_roots=True, publish=False) as anchors:
        return anchors.identities()


def _held_root_identities(
    conn, roots: tuple[_HeldDirectory, _HeldDirectory],
) -> dict[str, Any]:
    if roots != conn.storage_roots:
        raise WorkspacePathError("workspace roots are not bound to this registry connection")
    return dict(conn.storage_root_identities)


def _row_root_identities(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        identities = json.loads(row["root_identities_json"] or "null")
    except (TypeError, ValueError, KeyError) as exc:
        raise WorkspacePathError("workspace lease root identities are malformed") from exc
    if not isinstance(identities, dict) or identities.get("version") != 2:
        raise WorkspacePathError("workspace lease has no durable root identities")
    return identities


def _validate_row_roots(
    row: Mapping[str, Any], layout: _Layout,
    roots: tuple[_HeldDirectory, _HeldDirectory] | None = None,
    data_identity: list[int] | None = None,
    current_identities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected = _row_root_identities(row)
    if current_identities is not None:
        current = dict(current_identities)
    elif roots is None:
        current = _root_identities(layout)
    else:
        raise WorkspacePathError("full workspace registry identity is required for validation")
    if expected != current:
        raise WorkspacePathError(
            "workspace storage root identity changed; refusing lease mutation "
            f"(expected={expected}, current={current})"
        )
    return expected


def _validate_cleanup_roots(
    cleanup: Mapping[str, Any], roots: tuple[_HeldDirectory, _HeldDirectory],
) -> None:
    identities = cleanup.get("root_identities")
    if not isinstance(identities, dict):
        raise WorkspacePathError("cleanup plan has no durable root identities")
    expected_workspaces = identities.get("workspaces")
    expected_quarantine = identities.get("quarantine")
    if expected_workspaces is None or expected_quarantine is None:
        raise WorkspacePathError("cleanup plan has no durable root identities")
    current_workspaces = roots[0].identity_json()
    current_quarantine = roots[1].identity_json()
    if expected_workspaces != current_workspaces or expected_quarantine != current_quarantine:
        raise WorkspacePathError(
            "workspace cleanup root identity changed; refusing to interpret path absence "
            f"(expected workspaces={expected_workspaces}, quarantine={expected_quarantine}; "
            f"current workspaces={current_workspaces}, quarantine={current_quarantine})"
        )


def _detach_workspace(
    layout: _Layout, workspace_id: str, lease_id: str, generation: int,
    operation: str, *, planned: Path | None = None,
    roots: tuple[_HeldDirectory, _HeldDirectory] | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Atomically remove the leased name before DB unlock so a successor can never be cleaned."""
    path = layout.workspaces_dir / workspace_id
    detached = planned or _planned_detached_path(
        layout, workspace_id, lease_id, generation, operation,
    )
    if detached.parent != layout.quarantine_dir:
        raise WorkspacePathError("detached workspace target escaped quarantine")

    def mutate(held: tuple[_HeldDirectory, _HeldDirectory]):
        workspaces, quarantine = held
        if not workspaces.exists(workspace_id):
            if quarantine.exists(detached.name):
                return detached, {
                    "classification": "pending", "disposition": "detached",
                    "original_path": f"workspaces/{workspace_id}",
                    "detached_name": detached.name,
                }
            return None, {
                "classification": "missing", "disposition": "absent",
                "original_path": f"workspaces/{workspace_id}",
            }
        if quarantine.exists(detached.name):
            return None, {
                "classification": "uncertain", "disposition": "preserved",
                "original_path": f"workspaces/{workspace_id}",
                "detached_name": detached.name,
                "cleanup_error": "both canonical and planned detached paths exist",
            }
        try:
            workspaces.rename_to(workspace_id, quarantine, detached.name)
        except OSError as exc:
            return None, {
                "classification": "uncertain", "disposition": "preserved",
                "original_path": f"workspaces/{workspace_id}",
                "cleanup_error": f"{type(exc).__name__}: {exc}",
            }
        return detached, {
            "classification": "pending", "disposition": "detached",
            "original_path": f"workspaces/{workspace_id}",
            "detached_name": detached.name,
        }

    if roots is not None:
        return mutate(roots)
    with _workspace_roots(layout) as held:
        return mutate(held)


def _finish_detached_cleanup(
    layout: _Layout, detached: Path, receipt: dict[str, Any],
) -> dict[str, Any]:
    with _workspace_roots(layout) as roots:
        _validate_cleanup_roots(receipt, roots)
        return _finish_detached_cleanup_held(detached, receipt, roots[1])


def _finish_detached_cleanup_held(
    detached: Path, receipt: dict[str, Any], quarantine: _HeldDirectory,
) -> dict[str, Any]:
    if detached.parent != quarantine.path:
        raise WorkspacePathError("detached cleanup escaped the held quarantine root")
    held_path = quarantine.child_path(detached.name)
    assessment = _classify_workspace(held_path)
    finished = {**receipt, **assessment}
    try:
        if assessment["safe_to_delete"]:
            # The only existing safe-to-delete tree is empty. Atomic rmdir refuses a late file;
            # recursive deletion would race classification and destroy newly-created content.
            try:
                quarantine.rmdir(detached.name)
            except OSError as exc:
                if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    late = _classify_workspace(held_path)
                    finished.update({
                        **late, "safe_to_delete": False, "disposition": "quarantined",
                        "quarantine_name": detached.name, "cleanup_race": "late_content",
                    })
                    finished.pop("detached_name", None)
                    return finished
                raise
            finished["disposition"] = "removed"
            finished.pop("detached_name", None)
        else:
            finished.update({"disposition": "quarantined", "quarantine_name": detached.name})
            finished.pop("detached_name", None)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            finished.update({
                "classification": "missing", "safe_to_delete": True,
                "disposition": "removed", "cleanup_race": "already_removed",
            })
            finished.pop("detached_name", None)
            return finished
        finished.update({
            "disposition": "preserved", "quarantine_name": detached.name,
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


def _reconcile_pending_generation(
    conn, layout: _Layout, row: Mapping[str, Any],
    roots: tuple[_HeldDirectory, _HeldDirectory] | None = None,
) -> None:
    """Preserve names left by a crashed transition before the current row is replaced."""
    if roots is None:
        roots = conn.storage_roots
    workspaces, quarantine = roots
    identities = _validate_row_roots(
        row, layout, current_identities=conn.storage_root_identities,
    )
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
        names = [
            name for name in quarantine.list_names()
            if name == recovery_base.name or name.startswith(f"{recovery_base.name}-")
        ]
        return [layout.quarantine_dir / name for name in sorted(names)]

    def remember(source: str, path: Path) -> None:
        if not any(item.get("quarantine_name") == path.name for item in preserved):
            preserved.append({
                "source": source, "disposition": "quarantined",
                "quarantine_name": path.name,
            })

    # A previous retry may have crashed after moving the canonical name to recovery but before its
    # receipt transaction committed. Inventory every deterministic recovery slot on every pass.
    for existing in recovery_paths():
        remember("recovery", existing)

    if workspaces.exists(workspace_id):
        target = planned
        if quarantine.exists(target.name):
            target = recovery_base
            suffix = 2
            while quarantine.exists(target.name):
                target = recovery_base.with_name(f"{recovery_base.name}-{suffix}")
                suffix += 1
        try:
            workspaces.rename_to(workspace_id, quarantine, target.name)
            remember("canonical", target)
        except OSError as exc:
            raise WorkspacePathError(
                f"cannot preserve interrupted workspace generation at {canonical}: {exc}"
            ) from exc
    if quarantine.exists(planned.name):
        remember("detached", planned)
    for existing in recovery_paths():
        remember("recovery", existing)
    if not preserved:
        preserved.append({"disposition": "absent"})
    _cleanup_receipt(
        conn, row, "recovery", "reconciled", {
            "interrupted_operation": operation, "preserved": preserved,
            "root_identities": identities,
        },
    )


def _public_snapshot(conn, row: Mapping[str, Any]) -> dict[str, Any]:
    cleanup = json.loads(row["cleanup_json"]) if row["cleanup_json"] else None
    if cleanup is not None:
        cleanup = _cleanup_paths(
            conn.layout, str(row["workspace_id"]), cleanup,
            lease_id=str(row["lease_id"]), generation=int(row["generation"]),
            operation=str(cleanup.get("operation") or ""),
        )
    return {
        "contractVersion": int(row["contract_version"]),
        "leaseId": row["lease_id"],
        "workspaceId": row["workspace_id"],
        "path": str(Path(conn.workspace_base) / row["workspace_id"]),
        "state": row["state"],
        "generation": int(row["generation"]),
        "owner": {
            "pid": int(row["owner_pid"]), "createTime": row["owner_create_time"],
            "host": row["owner_host"], "machineIdentity": row["owner_machine_identity"],
            "instance": row["owner_instance"],
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
        pid, created, host, instance = _owner_stamp()
        machine = _machine_identity()
        reclaimed: str | None = None
        with transaction(_connect(layout), immediate=True) as conn:
            recovery_roots = conn.storage_roots
            request_fingerprint = _operation_fingerprint(layout, "acquire", workspace_id, ttl)
            now, expires_wall, heartbeat_mono, expires_mono, observer, observed_mono = (
                _fresh_expiry(ttl)
            )
            storage_roots = _held_root_identities(conn, recovery_roots)
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
                _validate_row_roots(
                    intent_row, layout,
                    current_identities=conn.storage_root_identities,
                )
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
                    _validate_workspace_entry(
                        layout, workspace_id, conn.storage_roots[0],
                        intent_row["workspace_identity_json"],
                    )
                    changed = conn.execute(
                        """UPDATE workspace_leases SET owner_pid=?, owner_create_time=?,
                           owner_host=?, owner_machine_identity=?, owner_instance=?,
                           heartbeat_at=?, expires_at=?,
                           heartbeat_monotonic=?, expires_monotonic=?, expiry_observer=?,
                           expiry_observed_monotonic=?, updated_at=?
                           WHERE lease_id=? AND generation=? AND state='active'
                           AND acquire_intent_id=?""",
                        (
                            pid, created, host, machine, instance, now, expires_wall,
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
                    _reconcile_pending_generation(conn, layout, intent_row, recovery_roots)
                    generation = int(intent_row["generation"]) + 1
                    lease_id = str(uuid.uuid4())
                elif state == "preparing":
                    generation = int(intent_row["generation"])
                    lease_id = str(intent_row["lease_id"])
                    _reconcile_pending_generation(conn, layout, intent_row, recovery_roots)
                else:
                    raise WorkspaceInUseError(
                        f"workspace {workspace_id!r} is in a release transition"
                    )
                reclaimed = "intent_retry"
                planned_detached = _planned_detached_path(
                    layout, workspace_id, lease_id, generation, "acquire",
                )
                cleanup = _preparing_cleanup(
                    workspace_id, planned_detached, storage_roots,
                )
                changed = conn.execute(
                    """UPDATE workspace_leases SET lease_id=?, state='preparing', owner_pid=?,
                       owner_create_time=?, owner_host=?, owner_machine_identity=?, owner_instance=?,
                       ttl_seconds=?,
                       heartbeat_at=?, expires_at=?, heartbeat_monotonic=?, expires_monotonic=?,
                       expiry_observer=?, expiry_observed_monotonic=?, released_at=NULL,
                       generation=?, cleanup_json=?, updated_at=?
                       WHERE workspace_id=? AND acquire_intent_id=?""",
                    (
                        lease_id, pid, created, host, machine, instance, ttl, now, expires_wall,
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
                    _reconcile_pending_generation(conn, layout, old, recovery_roots)
                    _supersede_lease_operations(conn, old["lease_id"], now)
                lease_id = str(uuid.uuid4())
                planned_detached = _planned_detached_path(
                    layout, workspace_id, lease_id, generation, "acquire",
                )
                cleanup = _preparing_cleanup(
                    workspace_id, planned_detached, storage_roots,
                )
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
                        owner_create_time, owner_host, owner_machine_identity, owner_instance,
                        ttl_seconds, acquired_at,
                        heartbeat_at, expires_at, heartbeat_monotonic, expires_monotonic,
                        expiry_observer, expiry_observed_monotonic, released_at, generation,
                        acquire_intent_id, root_identities_json, workspace_identity_json,
                        cleanup_json, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        workspace_id, lease_id, capability_hash, HANDLE_VERSION, "preparing",
                        layout.plugin_namespace, layout.plugin_identity, layout.profile_key,
                        f"workspaces/{workspace_id}", pid, created, host, machine, instance,
                        ttl, now, now, expires_wall, heartbeat_mono, expires_mono,
                        observer, observed_mono, None, generation, operation_id,
                        json.dumps(storage_roots, sort_keys=True),
                        None,
                        json.dumps(cleanup, sort_keys=True), now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM workspace_leases WHERE lease_id=?", (lease_id,),
                ).fetchone()
                _cleanup_receipt(conn, row, "acquire", "planned", cleanup)
                _event(conn, row, "preparing", {"reclaimed": reclaimed, "cleanup": cleanup})

        detached, cleanup = self._prepare_acquire_workspace(
            layout, workspace_id, lease_id, generation, operation_id,
            capability, planned_detached,
        )

        if detached is not None:
            finished_cleanup = _finish_detached_cleanup(layout, detached, cleanup)
            finished_cleanup.update({
                "operation": "acquire", "planned_detached_name": planned_detached.name,
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
                raise InvalidWorkspaceHandleError("workspace preparation lost its fence")
            if (
                current["state"] != "preparing"
                or int(current["generation"]) != generation
                or current["acquire_intent_id"] != operation_id
            ):
                raise InvalidWorkspaceHandleError("workspace activation lost its fence")
            workspace_identity = _validate_workspace_entry(
                layout, str(current["workspace_id"]), conn.storage_roots[0],
            )
            changed = conn.execute(
                """UPDATE workspace_leases SET state='active', heartbeat_at=?, expires_at=?,
                   heartbeat_monotonic=?, expires_monotonic=?, expiry_observer=?,
                   expiry_observed_monotonic=?, workspace_identity_json=?, updated_at=? WHERE lease_id=?
                   AND state='preparing' AND generation=? AND acquire_intent_id=?""",
                (
                    ready_at, ready_expires_wall, ready_mono, ready_expires_mono,
                    ready_observer, ready_observed_mono,
                    json.dumps(workspace_identity), ready_at, lease_id,
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

    def _prepare_acquire_workspace(
        self, layout: _Layout, workspace_id: str, lease_id: str, generation: int,
        operation_id: str, capability: str, planned_detached: Path,
    ) -> tuple[Path | None, dict[str, Any]]:
        preparation_error: WorkspacePathError | None = None
        with transaction(_connect(layout), immediate=True) as conn:
            roots = conn.storage_roots
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
                layout, workspace_id, lease_id, generation, "acquire",
                planned=planned_detached, roots=roots,
            )
            plan_roots = _validate_row_roots(
                current, layout, current_identities=conn.storage_root_identities,
            )
            cleanup.update({
                "operation": "acquire", "planned_detached_name": planned_detached.name,
                "root_identities": plan_roots,
            })
            _cleanup_receipt(conn, current, "acquire", "detached", cleanup)
            path = layout.workspaces_dir / workspace_id
            if detached is None and cleanup["disposition"] == "preserved":
                preparation_error = WorkspacePathError(
                    f"cannot detach occupied workspace {path}; existing contents were preserved"
                )
            if preparation_error is None:
                try:
                    roots[0].mkdir(workspace_id, 0o700)
                    if os.name != "nt":
                        os.chmod(
                            workspace_id, 0o700, dir_fd=roots[0].fd,
                            follow_symlinks=False,
                        )
                    child_info = roots[0].stat(workspace_id)
                    if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(child_info.st_mode):
                        raise WorkspacePathError(
                            f"workspace path is not a regular directory: {path}"
                        )
                    roots[0].verify()
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
        return detached, cleanup

    def renew(
        self, handle: Mapping[str, Any], *, ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        layout = self._layout()
        with transaction(_connect(layout), immediate=True) as conn:
            row = _validated_row(conn, layout, handle, validate_path=False)
            if row["state"] != "active":
                raise InvalidWorkspaceHandleError("workspace lease has been released")
            _validate_workspace_entry(
                layout, str(row["workspace_id"]), conn.storage_roots[0],
                row["workspace_identity_json"],
            )
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
                _validate_workspace_entry(
                    layout, str(row["workspace_id"]), conn.storage_roots[0],
                    row["workspace_identity_json"],
                )
                (
                    fresh_wall, fresh_expires_wall, fresh_mono, fresh_expires_mono,
                    fresh_observer, fresh_observed_mono,
                ) = _fresh_expiry(ttl)
                replay_pid, replay_created, replay_host, replay_instance = _owner_stamp()
                replay_machine = _machine_identity()
                changed = conn.execute(
                    """UPDATE workspace_leases SET owner_pid=?, owner_create_time=?, owner_host=?,
                       owner_machine_identity=?, owner_instance=?, ttl_seconds=?, heartbeat_at=?, expires_at=?,
                       heartbeat_monotonic=?, expires_monotonic=?, expiry_observer=?,
                       expiry_observed_monotonic=?, updated_at=?
                       WHERE lease_id=? AND generation=? AND state='active'""",
                    (
                        replay_pid, replay_created, replay_host, replay_machine,
                        replay_instance, ttl,
                        fresh_wall, fresh_expires_wall,
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
                _validate_workspace_entry(
                    layout, str(row["workspace_id"]), conn.storage_roots[0],
                    row["workspace_identity_json"],
                )
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
                machine = _machine_identity()
                (
                    fresh_wall, fresh_expires_wall, fresh_mono, fresh_expires_mono,
                    fresh_observer, fresh_observed_mono,
                ) = _fresh_expiry(ttl)
                changed = conn.execute(
                    """UPDATE workspace_leases SET owner_pid=?, owner_create_time=?, owner_host=?,
                       owner_machine_identity=?, owner_instance=?, ttl_seconds=?, heartbeat_at=?, expires_at=?,
                       heartbeat_monotonic=?, expires_monotonic=?, expiry_observer=?,
                       expiry_observed_monotonic=?, updated_at=?, lease_id=?, capability_hash=?,
                       generation=?
                       WHERE lease_id=? AND state='active' AND generation=?""",
                    (
                        pid, created, host, machine, instance, ttl,
                        fresh_wall, fresh_expires_wall,
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
            _validate_workspace_entry(
                layout, str(row["workspace_id"]), conn.storage_roots[0],
                row["workspace_identity_json"],
            )
            if _lease_expired(conn, row):
                raise WorkspaceLeaseExpiredError("workspace lease expired; reconnect it before use")
            return _public_snapshot(conn, row)

    def release(self, handle: Mapping[str, Any]) -> dict[str, Any]:
        layout = self._layout()
        detached: Path | None = None
        released_snapshot: dict[str, Any] | None = None
        release_error: WorkspacePathError | None = None
        with transaction(_connect(layout), immediate=True) as conn:
            release_roots = conn.storage_roots
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
                detached = planned if release_roots[1].exists(planned.name) else None
                if detached is None:
                    cleanup = {
                        **cleanup, "classification": "missing", "safe_to_delete": True,
                        "disposition": "removed",
                        "reconciled_reason": "detached_target_absent",
                        "planned_detached_name": planned.name,
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
                        "disposition": "releasing",
                        "original_path": f"workspaces/{row['workspace_id']}",
                        "planned_detached_name": planned.name,
                        "root_identities": _row_root_identities(row),
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
                detach_roots = conn.storage_roots
                current = _validated_row(conn, layout, handle, validate_path=False)
                if current["state"] == "released":
                    released_snapshot = _public_snapshot(conn, current)
                else:
                    if (
                        current["state"] != "releasing"
                        or current["generation"] != row["generation"]
                    ):
                        raise InvalidWorkspaceHandleError("workspace lease changed during release")
                    if detach_roots[0].exists(str(current["workspace_id"])):
                        _validate_workspace_entry(
                            layout, str(current["workspace_id"]), detach_roots[0],
                            current["workspace_identity_json"],
                        )
                    detached, cleanup = _detach_workspace(
                        layout, current["workspace_id"], current["lease_id"],
                        current["generation"], "release", planned=planned,
                        roots=detach_roots,
                    )
                    cleanup["operation"] = "release"
                    cleanup["planned_detached_name"] = planned.name
                    cleanup["root_identities"] = _row_root_identities(current)
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
                            f"workspace release could not detach "
                            f"{layout.workspaces_dir / current['workspace_id']}; "
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
        cleanup = _finish_detached_cleanup(layout, detached, cleanup)
        cleanup["operation"] = "release"
        cleanup["planned_detached_name"] = planned.name
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
