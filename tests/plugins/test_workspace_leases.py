"""Behavior contract for the durable plugin workspace lifecycle (issue #6)."""

from __future__ import annotations

import json
import os
import sqlite3
import socket
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override
from hermes_cli.plugin_workspaces import (
    HOST_FEATURE,
    InvalidWorkspaceHandleError,
    WorkspaceInUseError,
    WorkspaceLeaseExpiredError,
    WorkspaceOwnershipError,
    WorkspacePathError,
)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _context(
    home: Path, plugin_id: str = "pr-review", *, skill_namespace: str = "",
) -> PluginContext:
    home.mkdir(parents=True, exist_ok=True)
    return PluginContext(
        PluginManifest(name=plugin_id, key=plugin_id, skill_namespace=skill_namespace),
        PluginManager(scope_key=hermes_home_key(home)),
    )


def _acquire(
    ctx: PluginContext, workspace_id: str, *, ttl_seconds: float | None = None,
    intent: dict | None = None,
) -> dict:
    intent = intent or ctx.workspaces.new_intent()
    kwargs = {"intent": intent}
    if ttl_seconds is not None:
        kwargs["ttl_seconds"] = ttl_seconds
    return ctx.workspaces.acquire(workspace_id, **kwargs)


def _reconnect(
    ctx: PluginContext, handle: dict, *, ttl_seconds: float | None = None,
    intent: dict | None = None,
) -> dict:
    intent = intent or ctx.workspaces.new_intent()
    kwargs = {"intent": intent}
    if ttl_seconds is not None:
        kwargs["ttl_seconds"] = ttl_seconds
    return ctx.workspaces.reconnect(handle, **kwargs)


def _child_env(home: Path) -> dict[str, str]:
    return {**os.environ, "HERMES_HOME": str(home)}


def _run_git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def _commit(path: Path, filename: str = "tracked.txt") -> None:
    (path / filename).write_text("base\n", encoding="utf-8")
    _run_git(path, "add", filename)
    _run_git(
        path, "-c", "user.name=Hermes Tests", "-c", "user.email=tests@invalid",
        "commit", "-m", "fixture",
    )


def _expire_lease(conn: sqlite3.Connection, lease_id: str) -> None:
    conn.execute(
        """UPDATE workspace_leases SET expires_at=0, expires_monotonic=0
           WHERE lease_id=?""",
        (lease_id,),
    )


def _seed_original_h1_database(
    home: Path, *, workspace_id: str = "legacy-run", plugin_namespace: str = "pr-review",
    profile_key: str | None = None, workspace_path: str | None = None,
) -> tuple[dict, Path]:
    from hermes_cli import plugin_workspaces

    data_dir = home / "plugin-data/pr-review"
    workspaces = data_dir / "workspaces"
    quarantine = data_dir / "workspace-quarantine"
    workspace = workspaces / workspace_id
    workspace.mkdir(parents=True)
    quarantine.mkdir()
    db = data_dir / "workspace-leases.db"
    capability = "legacy-capability-" + "x" * 32
    lease_id = "11111111-1111-4111-8111-111111111111"
    now = time.time()
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE workspace_leases (
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
            CREATE TABLE workspace_lease_events (
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
            CREATE INDEX workspace_lease_events_lookup
                ON workspace_lease_events(workspace_id, event_id);
            """
        )
        conn.execute(
            """INSERT INTO workspace_leases
               (workspace_id, lease_id, capability_hash, contract_version, state,
                plugin_namespace, profile_key, workspace_path, owner_pid, owner_create_time,
                owner_host, owner_instance, ttl_seconds, acquired_at, heartbeat_at, expires_at,
                released_at, generation, cleanup_json, updated_at)
               VALUES (?, ?, ?, 1, 'active', ?, ?, ?, ?, ?, ?, ?, 300, ?, ?, ?, NULL, 1, ?, ?)""",
            (
                workspace_id, lease_id, plugin_workspaces._capability_hash(capability),
                plugin_namespace, profile_key or hermes_home_key(home),
                workspace_path or str(workspace), -1,
                None, socket.gethostname(),
                plugin_workspaces._host_instance(), now, now, now + 300,
                json.dumps({"classification": "missing", "disposition": "absent"}), now,
            ),
        )
    return {
        "contract_version": 1, "lease_id": lease_id, "capability": capability,
    }, workspace


def _swap_root_to_symlink(root: Path, external: Path) -> Path:
    backup = root.with_name(f"{root.name}-held")
    os.rename(root, backup)
    root.symlink_to(external, target_is_directory=True)
    return backup


def _restore_swapped_root(root: Path, backup: Path) -> None:
    root.unlink()
    os.rename(backup, root)


def test_handle_is_opaque_serializable_and_profile_bound(tmp_path: Path) -> None:
    home_a, home_b = tmp_path / "profiles" / "a", tmp_path / "profiles" / "b"
    ctx = _context(home_a)
    handle = _acquire(ctx, "run-1")

    assert json.loads(json.dumps(handle)) == handle
    assert set(handle) == {"contract_version", "lease_id", "capability"}
    snapshot = ctx.workspaces.inspect(handle)
    assert snapshot["path"] == str(home_a.resolve() / "plugin-data/pr-review/workspaces/run-1")
    assert snapshot["events"][-1]["leaseId"] == handle["lease_id"]
    assert snapshot["owner"]["instance"]

    with pytest.raises(InvalidWorkspaceHandleError):
        _context(home_a, "another-plugin").workspaces.inspect(handle)
    with pytest.raises(InvalidWorkspaceHandleError):
        _context(home_b).workspaces.inspect(handle)

    # A cached context is bound to its manager/profile even if ambient multiplex scope changes.
    token = set_hermes_home_override(home_b)
    try:
        assert ctx.workspaces.inspect(handle)["path"].startswith(str(home_a.resolve()))
    finally:
        reset_hermes_home_override(token)

    db = home_a / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        assert handle["capability"] not in "\n".join(conn.iterdump())
    if os.name != "nt":
        private_dirs = [
            home_a / "plugin-data",
            home_a / "plugin-data/pr-review",
            home_a / "plugin-data/pr-review/workspaces",
            Path(snapshot["path"]),
        ]
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in private_dirs)
        assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_handle_validation_token_rotation_and_idempotent_release(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "run-1")
    forged = {**handle, "capability": "x" * len(handle["capability"])}
    with pytest.raises(InvalidWorkspaceHandleError):
        ctx.workspaces.inspect(forged)
    oversized = {**handle, "capability": "x" * 257}
    with pytest.raises(InvalidWorkspaceHandleError, match="malformed"):
        ctx.workspaces.inspect(oversized)

    first_renewal = ctx.workspaces.renew(handle)
    second_renewal = ctx.workspaces.renew(handle)
    assert (first_renewal["leaseId"], first_renewal["generation"]) == (
        second_renewal["leaseId"], second_renewal["generation"],
    )
    assert second_renewal["expiresAt"] >= first_renewal["expiresAt"]

    reused_secret = ctx.workspaces.new_intent()
    reused_secret["capability"] = handle["capability"]
    with pytest.raises(InvalidWorkspaceHandleError, match="fresh successor"):
        ctx.workspaces.reconnect(handle, intent=reused_secret)

    successor = _reconnect(ctx, handle)
    assert successor != handle
    assert ctx.workspaces.inspect(successor)["generation"] == 2
    with pytest.raises(InvalidWorkspaceHandleError):
        ctx.workspaces.inspect(handle)

    released = ctx.workspaces.release(successor)
    assert released["state"] == "released"
    assert ctx.workspaces.release(successor)["state"] == "released"
    with pytest.raises(InvalidWorkspaceHandleError):
        ctx.workspaces.inspect(successor)
    with pytest.raises(InvalidWorkspaceHandleError):
        ctx.workspaces.renew(successor)


def test_competing_process_cannot_acquire_live_workspace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    handle = _acquire(_context(home), "shared", ttl_seconds=60)
    script = """
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.plugin_workspaces import WorkspaceInUseError
ctx = PluginContext(PluginManifest(name='pr-review'), PluginManager())
try:
    ctx.workspaces.acquire('shared', intent=ctx.workspaces.new_intent(), ttl_seconds=60)
except WorkspaceInUseError:
    print('refused')
else:
    print('acquired')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=PROJECT_ROOT, env=_child_env(home),
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert result.stdout.strip().splitlines()[-1] == "refused"
    assert _context(home).workspaces.inspect(handle)["state"] == "active"


def test_simultaneous_processes_have_exactly_one_winner(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    gate, release = tmp_path / "gate", tmp_path / "release"
    ready = [tmp_path / f"ready-{index}" for index in range(2)]
    outcomes = [tmp_path / f"outcome-{index}" for index in range(2)]
    script = r"""
import sys
import time
import traceback
from pathlib import Path
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.plugin_workspaces import WorkspaceInUseError

ready, outcome, gate, release = (Path(value) for value in sys.argv[1:])
ctx = PluginContext(PluginManifest(name='pr-review'), PluginManager())
ready.touch()
deadline = time.monotonic() + 30
while not gate.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('timed out waiting for acquisition gate')
    time.sleep(0.01)
try:
    ctx.workspaces.acquire('simultaneous', intent=ctx.workspaces.new_intent(), ttl_seconds=60)
except WorkspaceInUseError:
    outcome.write_text('refused', encoding='utf-8')
except BaseException as exc:
    outcome.write_text(
        'error:' + type(exc).__name__ + ':' + str(exc) + '\n' + traceback.format_exc(),
        encoding='utf-8',
    )
else:
    outcome.write_text('acquired', encoding='utf-8')
    while not release.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('timed out waiting for release gate')
        time.sleep(0.01)
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable, "-c", script, str(ready[index]), str(outcomes[index]),
                str(gate), str(release),
            ],
            cwd=PROJECT_ROOT,
            env=_child_env(home),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]

    try:
        deadline = time.monotonic() + 30
        while not all(path.exists() for path in ready):
            if time.monotonic() >= deadline:
                raise AssertionError("child processes did not reach the acquisition gate")
            time.sleep(0.01)
        gate.touch()
        while not all(path.exists() for path in outcomes):
            if time.monotonic() >= deadline:
                raise AssertionError("child processes did not report acquisition outcomes")
            time.sleep(0.01)
    finally:
        release.touch()

    failures = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        if process.returncode:
            failures.append(f"exit={process.returncode} stdout={stdout!r} stderr={stderr!r}")
    assert failures == []
    assert sorted(path.read_text(encoding="utf-8") for path in outcomes) == [
        "acquired", "refused",
    ]


def test_dead_owner_can_reconnect_and_old_handle_is_fenced(tmp_path: Path) -> None:
    home = tmp_path / "home"
    script = """
import json
from pathlib import Path
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
ctx = PluginContext(PluginManifest(name='pr-review'), PluginManager())
handle = ctx.workspaces.acquire(
    'restart-safe', intent=ctx.workspaces.new_intent(), ttl_seconds=3600)
Path(ctx.workspaces.inspect(handle)['path'], 'work.txt').write_text('preserve me', encoding='utf-8')
print(json.dumps(handle))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=PROJECT_ROOT, env=_child_env(home),
        check=True, capture_output=True, text=True, timeout=30,
    )
    predecessor = json.loads(result.stdout.strip().splitlines()[-1])

    ctx = _context(home)
    successor = _reconnect(ctx, predecessor)
    snapshot = ctx.workspaces.inspect(successor)
    assert Path(snapshot["path"], "work.txt").read_text(encoding="utf-8") == "preserve me"
    assert snapshot["events"][-1]["type"] == "reconnected"
    with pytest.raises(InvalidWorkspaceHandleError):
        ctx.workspaces.inspect(predecessor)


def test_expired_generation_is_atomically_reclaimed(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    old = _acquire(ctx, "ttl", ttl_seconds=60)
    old_path = Path(ctx.workspaces.inspect(old)["path"])
    (old_path / "untracked.txt").write_text("old", encoding="utf-8")
    db = tmp_path / "home/plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        _expire_lease(conn, old["lease_id"])

    successor = _acquire(ctx, "ttl", ttl_seconds=60)
    snapshot = ctx.workspaces.inspect(successor)
    assert snapshot["generation"] == 2
    assert not Path(snapshot["path"], "untracked.txt").exists()
    assert snapshot["cleanup"]["disposition"] == "quarantined"
    assert Path(snapshot["cleanup"]["quarantine_path"], "untracked.txt").exists()
    with pytest.raises(InvalidWorkspaceHandleError):
        ctx.workspaces.inspect(old)


def test_expired_handle_requires_reconnect_for_direct_use(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "expired", ttl_seconds=60)
    db = tmp_path / "home/plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        _expire_lease(conn, handle["lease_id"])

    with pytest.raises(WorkspaceLeaseExpiredError):
        ctx.workspaces.inspect(handle)
    successor = _reconnect(ctx, handle)
    assert ctx.workspaces.inspect(successor)["generation"] == 2


def test_same_boot_monotonic_expiry_ignores_wall_clock_jumps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    wall, monotonic = [100.0], [10.0]
    monkeypatch.setattr(plugin_workspaces, "_host_instance", lambda: "boot-id:test")
    monkeypatch.setattr(plugin_workspaces.time, "time", lambda: wall[0])
    monkeypatch.setattr(plugin_workspaces.time, "monotonic", lambda: monotonic[0])
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "clock", ttl_seconds=10)

    wall[0] = 1_000_000.0
    monotonic[0] = 11.0
    assert ctx.workspaces.inspect(handle)["state"] == "active"
    with pytest.raises(WorkspaceInUseError):
        _acquire(ctx, "clock", ttl_seconds=10)

    wall[0] = -1_000_000.0
    monotonic[0] = 20.0
    with pytest.raises(WorkspaceLeaseExpiredError):
        ctx.workspaces.inspect(handle)
    successor = _acquire(ctx, "clock", ttl_seconds=10)
    assert ctx.workspaces.inspect(successor)["generation"] == 2


def test_verified_reboot_is_dead_but_unknown_owner_needs_full_observation_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    wall, monotonic, boot = [100.0], [10.0], ["boot-id:a"]
    monkeypatch.setattr(plugin_workspaces, "_host_instance", lambda: boot[0])
    monkeypatch.setattr(plugin_workspaces.time, "time", lambda: wall[0])
    monkeypatch.setattr(plugin_workspaces.time, "monotonic", lambda: monotonic[0])
    ctx = _context(tmp_path / "home")
    before_reboot = _acquire(ctx, "reboot", ttl_seconds=60)
    boot[0] = "boot-id:b"
    after_reboot = _acquire(ctx, "reboot", ttl_seconds=60)
    assert ctx.workspaces.inspect(after_reboot)["generation"] == 2
    with pytest.raises(InvalidWorkspaceHandleError):
        ctx.workspaces.inspect(before_reboot)

    unknown = _acquire(ctx, "unknown", ttl_seconds=5)
    db = tmp_path / "home/plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE workspace_leases SET owner_host='foreign-host',
               owner_machine_identity='machine-sha256:foreign', owner_instance='unverified',
               expires_at=0, expires_monotonic=0, expiry_observer=NULL,
               expiry_observed_monotonic=NULL WHERE lease_id=?""",
            (unknown["lease_id"],),
        )
    wall[0] = 9_999_999.0
    monotonic[0] = 100.0
    assert ctx.workspaces.inspect(unknown)["state"] == "active"
    monotonic[0] = 104.9
    assert ctx.workspaces.inspect(unknown)["state"] == "active"
    monotonic[0] = 105.0
    with pytest.raises(WorkspaceLeaseExpiredError):
        ctx.workspaces.inspect(unknown)


def test_same_hostname_foreign_machine_is_unknown_but_same_machine_reboot_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    machine, boot, monotonic = ["machine-sha256:local"], ["boot-id:local"], [10.0]
    monkeypatch.setattr(plugin_workspaces, "_machine_identity", lambda: machine[0])
    monkeypatch.setattr(plugin_workspaces, "_host_instance", lambda: boot[0])
    monkeypatch.setattr(plugin_workspaces.time, "monotonic", lambda: monotonic[0])
    ctx = _context(tmp_path / "home")
    foreign = _acquire(ctx, "foreign-machine", ttl_seconds=5)
    db = tmp_path / "home/plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE workspace_leases SET owner_host=?,
               owner_machine_identity='machine-sha256:foreign', owner_instance='boot-id:foreign',
               expires_at=0, expires_monotonic=0, expiry_observer=NULL,
               expiry_observed_monotonic=NULL WHERE lease_id=?""",
            (socket.gethostname(), foreign["lease_id"]),
        )
    assert ctx.workspaces.inspect(foreign)["state"] == "active"
    monotonic[0] = 14.9
    assert ctx.workspaces.inspect(foreign)["state"] == "active"
    monotonic[0] = 15.0
    with pytest.raises(WorkspaceLeaseExpiredError):
        ctx.workspaces.inspect(foreign)

    rebooted = _acquire(ctx, "same-machine-reboot", ttl_seconds=300)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE workspace_leases SET owner_machine_identity=?, owner_instance='boot-id:old'
               WHERE lease_id=?""",
            (machine[0], rebooted["lease_id"]),
        )
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM workspace_leases WHERE lease_id=?", (rebooted["lease_id"],),
        ).fetchone()
    assert plugin_workspaces._owner_status(row) == "dead"
    successor = _acquire(ctx, "same-machine-reboot")
    assert ctx.workspaces.inspect(successor)["generation"] == 2


def test_symlink_aliases_are_never_followed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel").write_text("outside", encoding="utf-8")
    root = home / "plugin-data/pr-review/workspaces"
    root.mkdir(parents=True)
    (home / "plugin-data/pr-review/workspace-quarantine").mkdir()
    (root / "aliased").symlink_to(external, target_is_directory=True)

    ctx = _context(home)
    handle = _acquire(ctx, "aliased")
    leased = Path(ctx.workspaces.inspect(handle)["path"])
    assert leased.is_dir() and not leased.is_symlink()
    assert (external / "sentinel").read_text(encoding="utf-8") == "outside"

    hostile_target = tmp_path / "hostile"
    hostile_target.mkdir()
    leased.rmdir()
    leased.symlink_to(hostile_target, target_is_directory=True)
    with pytest.raises(WorkspacePathError):
        ctx.workspaces.inspect(handle)


def test_symlinked_host_namespace_is_rejected(tmp_path: Path) -> None:
    home, external = tmp_path / "home", tmp_path / "external"
    home.mkdir()
    external.mkdir()
    (home / "plugin-data").symlink_to(external, target_is_directory=True)
    with pytest.raises(WorkspacePathError):
        _acquire(_context(home), "run-1")


@pytest.mark.linux_only
def test_workspaces_root_swap_before_mkdir_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    home, external = tmp_path / "home", tmp_path / "outside"
    external.mkdir()
    (external / "sentinel").write_text("outside", encoding="utf-8")
    ctx = _context(home)
    layout = ctx.workspaces._layout()
    original = plugin_workspaces._HeldDirectory.mkdir
    swapped: list[Path] = []

    def swap_before_mkdir(self, name, mode=0o700):
        if self.path == layout.workspaces_dir and name == "swap-mkdir" and not swapped:
            swapped.append(_swap_root_to_symlink(layout.workspaces_dir, external))
        return original(self, name, mode)

    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "mkdir", swap_before_mkdir)
    try:
        with pytest.raises(WorkspacePathError, match="replaced|identity"):
            _acquire(ctx, "swap-mkdir")
        assert (external / "sentinel").read_text(encoding="utf-8") == "outside"
        assert not (external / "swap-mkdir").exists()
    finally:
        if swapped:
            _restore_swapped_root(layout.workspaces_dir, swapped[0])


@pytest.mark.linux_only
def test_failed_held_directory_enter_does_not_leak_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspace_fs

    before = len(list(Path("/proc/self/fd").iterdir()))

    def fail_verify(_self):
        raise WorkspacePathError("injected identity mismatch")

    monkeypatch.setattr(plugin_workspace_fs.HeldDirectory, "verify", fail_verify)
    for _ in range(25):
        with pytest.raises(WorkspacePathError, match="injected"):
            plugin_workspace_fs.HeldDirectory(tmp_path).__enter__()
    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after == before


@pytest.mark.linux_only
def test_workspaces_root_swap_before_release_rename_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    home, external = tmp_path / "home", tmp_path / "outside"
    external.mkdir()
    (external / "sentinel").write_text("outside", encoding="utf-8")
    ctx = _context(home)
    handle = _acquire(ctx, "swap-release")
    layout = ctx.workspaces._layout()
    original = plugin_workspaces._HeldDirectory.rename_to
    swapped: list[Path] = []

    def swap_before_rename(self, name, target, target_name):
        if self.path == layout.workspaces_dir and not swapped:
            swapped.append(_swap_root_to_symlink(layout.workspaces_dir, external))
        return original(self, name, target, target_name)

    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "rename_to", swap_before_rename)
    try:
        with pytest.raises(WorkspacePathError, match="replaced|identity"):
            ctx.workspaces.release(handle)
        assert (external / "sentinel").read_text(encoding="utf-8") == "outside"
        assert not (external / "swap-release").exists()
    finally:
        if swapped:
            _restore_swapped_root(layout.workspaces_dir, swapped[0])
    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "rename_to", original)
    assert ctx.workspaces.release(handle)["state"] == "released"


@pytest.mark.linux_only
def test_quarantine_root_swap_during_cleanup_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    home, external = tmp_path / "home", tmp_path / "outside"
    external.mkdir()
    (external / "sentinel").write_text("outside", encoding="utf-8")
    ctx = _context(home)
    handle = _acquire(ctx, "swap-cleanup")
    workspace = Path(ctx.workspaces.inspect(handle)["path"])
    (workspace / "keep.txt").write_text("preserve", encoding="utf-8")
    layout = ctx.workspaces._layout()
    original = plugin_workspaces._classify_workspace
    swapped: list[Path] = []

    def swap_before_classify(path):
        if not swapped and "/fd/" in str(path):
            swapped.append(_swap_root_to_symlink(layout.quarantine_dir, external))
        return original(path)

    monkeypatch.setattr(plugin_workspaces, "_classify_workspace", swap_before_classify)
    try:
        with pytest.raises(WorkspacePathError, match="replaced|identity"):
            ctx.workspaces.release(handle)
        assert (external / "sentinel").read_text(encoding="utf-8") == "outside"
        assert not any(path.name.startswith(".release-") for path in external.iterdir())
    finally:
        if swapped:
            _restore_swapped_root(layout.quarantine_dir, swapped[0])
    monkeypatch.setattr(plugin_workspaces, "_classify_workspace", original)
    released = ctx.workspaces.release(handle)
    assert released["cleanup"]["disposition"] == "quarantined"


@pytest.mark.windows_only
def test_windows_held_root_blocks_reparse_swap(tmp_path: Path) -> None:
    from hermes_cli import plugin_workspaces

    ctx = _context(tmp_path / "home")
    layout = ctx.workspaces._layout()
    backup = layout.workspaces_dir.with_name("workspaces-swap")
    with plugin_workspaces._HeldDirectory(layout.workspaces_dir):
        with pytest.raises(OSError):
            os.replace(layout.workspaces_dir, backup)
    assert layout.workspaces_dir.is_dir()
    assert not backup.exists()


def test_trailing_dot_ids_are_rejected_or_hashed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = _context(home)
    with pytest.raises(ValueError, match="trailing"):
        _acquire(ctx, "run.")

    native = _context(home, "native.")
    native_handle = _acquire(native, "run")
    native_namespace = Path(native.workspaces.inspect(native_handle)["path"]).parents[1].name
    assert native_namespace.startswith("hermes-native-")
    assert not native_namespace.endswith(".")

    portable = _context(home, "portable.", skill_namespace="agent-plugin-portable.")
    portable_handle = _acquire(portable, "run")
    portable_namespace = Path(portable.workspaces.inspect(portable_handle)["path"]).parents[1].name
    assert portable_namespace.startswith("agent-plugin-")
    assert not portable_namespace.endswith(".")


@pytest.mark.windows_only
def test_windows_trailing_dot_workspace_alias_is_refused(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "run")
    with pytest.raises(ValueError, match="trailing"):
        _acquire(ctx, "run.")
    assert Path(ctx.workspaces.inspect(handle)["path"]).name == "run"


def test_cleanup_receipts_preserve_every_uncertain_or_unique_tree(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")

    setups = {
        "uncertain": lambda path: (path / "note.txt").write_text("data", encoding="utf-8"),
        "untracked": lambda path: (
            _run_git(path, "init"),
            (path / "new.txt").write_text("data", encoding="utf-8"),
        ),
        "dirty": lambda path: (
            _run_git(path, "init"), _commit(path),
            (path / "tracked.txt").write_text("changed\n", encoding="utf-8"),
        ),
        "ignored": lambda path: (
            _run_git(path, "init"),
            (path / ".gitignore").write_text("cache.bin\n", encoding="utf-8"),
            _run_git(path, "add", ".gitignore"),
            _run_git(
                path, "-c", "user.name=Hermes Tests", "-c", "user.email=tests@invalid",
                "commit", "-m", "ignore fixture",
            ),
            (path / "cache.bin").write_text("generated but unique", encoding="utf-8"),
        ),
        "unpushed": lambda path: (_run_git(path, "init"), _commit(path)),
        "clean_git": lambda path: (
            _run_git(path, "init"), _commit(path),
            _run_git(path, "update-ref", "refs/remotes/origin/main", "HEAD"),
        ),
    }

    for expected, setup in setups.items():
        handle = _acquire(ctx, expected)
        path = Path(ctx.workspaces.inspect(handle)["path"])
        setup(path)
        cleanup = ctx.workspaces.release(handle)["cleanup"]
        assert cleanup["classification"] == expected
        assert cleanup["disposition"] == "quarantined"
        assert Path(cleanup["quarantine_path"]).exists()

    empty = _acquire(ctx, "empty")
    empty_path = Path(ctx.workspaces.inspect(empty)["path"])
    cleanup = ctx.workspaces.release(empty)["cleanup"]
    assert cleanup["classification"] == "clean_empty"
    assert cleanup["disposition"] == "removed"
    assert not empty_path.exists()


@pytest.mark.parametrize("hidden_ref", ["branch", "tag", "reflog"])
def test_clean_current_tree_preserves_hidden_git_history(
    tmp_path: Path, hidden_ref: str,
) -> None:
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, f"hidden-{hidden_ref}")
    path = Path(ctx.workspaces.inspect(handle)["path"])
    _run_git(path, "init")
    _commit(path)
    base_branch = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    _run_git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _run_git(path, "checkout", "-b", "hidden-work")
    (path / "unique.txt").write_text(hidden_ref, encoding="utf-8")
    _run_git(path, "add", "unique.txt")
    _run_git(
        path, "-c", "user.name=Hermes Tests", "-c", "user.email=tests@invalid",
        "commit", "-m", "unique hidden work",
    )
    unique = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    if hidden_ref == "tag":
        _run_git(path, "tag", "hidden-tag", unique)
    _run_git(path, "checkout", base_branch)
    if hidden_ref != "branch":
        _run_git(path, "branch", "-D", "hidden-work")

    cleanup = ctx.workspaces.release(handle)["cleanup"]
    assert cleanup["classification"] == "clean_git"
    assert cleanup["disposition"] == "quarantined"
    quarantine = Path(cleanup["quarantine_path"])
    _run_git(quarantine, "cat-file", "-e", f"{unique}^{{commit}}")


def test_native_reserved_namespace_cannot_collide_with_portable_plugin(tmp_path: Path) -> None:
    home = tmp_path / "home"
    portable_namespace = "agent-plugin-collision-deadbeef"
    portable = _context(
        home, "portable-source", skill_namespace=portable_namespace,
    )
    native = _context(home, portable_namespace)
    portable_handle = _acquire(portable, "run")
    native_handle = _acquire(native, "run")
    portable_path = Path(portable.workspaces.inspect(portable_handle)["path"])
    native_path = Path(native.workspaces.inspect(native_handle)["path"])

    assert portable_path.parent.parent.name == portable_namespace
    assert native_path.parent.parent.name.startswith("hermes-native-")
    assert portable_path != native_path
    assert _acquire(_context(home), "exact-pr-review")
    assert (home / "plugin-data/pr-review/workspaces/exact-pr-review").is_dir()
    portable_db = portable_path.parents[1] / "workspace-leases.db"
    with sqlite3.connect(portable_db) as conn:
        conn.execute(
            "UPDATE workspace_leases SET plugin_identity='forged' WHERE lease_id=?",
            (portable_handle["lease_id"],),
        )
    with pytest.raises(WorkspacePathError, match="refusing to claim"):
        portable.workspaces.inspect(portable_handle)


def test_generated_native_namespace_cannot_collide_with_literal_native_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    generated = _context(home, "MixedCasePlugin")
    generated_handle = _acquire(generated, "run")
    generated_path = Path(generated.workspaces.inspect(generated_handle)["path"])
    generated_namespace = generated_path.parent.parent.name
    literal = _context(home, generated_namespace)
    literal_handle = _acquire(literal, "run")
    literal_path = Path(literal.workspaces.inspect(literal_handle)["path"])

    assert generated_namespace.startswith("hermes-native-")
    assert literal_path.parent.parent.name.startswith("hermes-native-")
    assert literal_path != generated_path


def test_unverified_current_boot_witness_does_not_mark_verified_owner_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    created = plugin_workspaces._process_create_time()
    row = {
        "owner_pid": os.getpid(), "owner_create_time": created,
        "owner_host": plugin_workspaces.socket.gethostname(),
        "owner_machine_identity": plugin_workspaces._machine_identity(),
        "owner_instance": "boot-id:recorded",
    }
    monkeypatch.setattr(plugin_workspaces, "_host_instance", lambda: "unverified")
    monkeypatch.setattr(plugin_workspaces, "_pid_alive_matches", lambda _pid, _created: True)
    monkeypatch.setattr(plugin_workspaces, "_process_create_time", lambda: created)
    assert plugin_workspaces._owner_status(row) == "self"


def test_acquire_samples_after_lock_and_refreshes_after_slow_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    ctx = _context(tmp_path / "home")
    old = _acquire(ctx, "slow", ttl_seconds=60)
    old_path = Path(ctx.workspaces.inspect(old)["path"])
    (old_path / "preserve.txt").write_text("old", encoding="utf-8")
    db = tmp_path / "home/plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE workspace_leases SET expires_at=5, expires_monotonic=5
               WHERE lease_id=?""",
            (old["lease_id"],),
        )

    clock = [0.0]
    real_transaction = plugin_workspaces.transaction
    first_write = [True]

    @contextmanager
    def contended_transaction(conn, *, immediate=False):
        with real_transaction(conn, immediate=immediate) as locked:
            if immediate and first_write[0]:
                first_write[0] = False
                clock[0] = 10.0  # the waiter obtains BEGIN IMMEDIATE after the old TTL elapsed
            yield locked

    real_finish = plugin_workspaces._finish_detached_cleanup

    def slow_finish(path, receipt):
        clock[0] += 10.0
        return real_finish(path, receipt)

    monkeypatch.setattr(plugin_workspaces.time, "time", lambda: clock[0])
    monkeypatch.setattr(plugin_workspaces.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(plugin_workspaces, "transaction", contended_transaction)
    monkeypatch.setattr(plugin_workspaces, "_finish_detached_cleanup", slow_finish)
    successor = _acquire(ctx, "slow", ttl_seconds=1)
    snapshot = ctx.workspaces.inspect(successor)
    assert snapshot["heartbeatAt"] == 20.0
    assert snapshot["expiresAt"] == 21.0
    assert snapshot["heartbeatMonotonic"] == 20.0
    assert snapshot["expiresMonotonic"] == 21.0
    assert snapshot["generation"] == 2


def test_crashed_acquire_reconciles_both_names_and_keeps_receipts(tmp_path: Path) -> None:
    from hermes_cli import plugin_workspaces

    home = tmp_path / "home"
    ctx = _context(home)
    failed = _acquire(ctx, "crash-acquire")
    failed_path = Path(ctx.workspaces.inspect(failed)["path"])
    (failed_path / "predecessor.txt").write_text("preserve", encoding="utf-8")
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT lease_id, generation FROM workspace_leases WHERE workspace_id='crash-acquire'"
        ).fetchone()
        planned = plugin_workspaces._planned_detached_path(
            ctx.workspaces._layout(), "crash-acquire", row[0], row[1], "acquire",
        )
        conn.execute(
            """UPDATE workspace_leases SET state='preparing', owner_pid=-1, expires_at=0,
               cleanup_json=? WHERE workspace_id='crash-acquire'""",
            (json.dumps({
                "operation": "acquire", "disposition": "preparing",
                "planned_detached_path": str(planned),
            }),),
        )
    os.replace(failed_path, planned)
    failed_path.mkdir()  # crash after rename+mkdir but before the preparation receipt commits

    successor = _acquire(ctx, "crash-acquire")
    snapshot = ctx.workspaces.inspect(successor)
    assert (planned / "predecessor.txt").read_text(encoding="utf-8") == "preserve"
    recovery = [
        receipt for receipt in snapshot["cleanupReceipts"]
        if receipt["operation"] == "recovery"
    ]
    assert recovery and recovery[-1]["phase"] == "reconciled"


def test_recovery_retry_inventories_prior_deterministic_recovery_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    home = tmp_path / "home"
    ctx = _context(home)
    failed = _acquire(ctx, "crash-recovery")
    canonical = Path(ctx.workspaces.inspect(failed)["path"])
    (canonical / "canonical.txt").write_text("canonical", encoding="utf-8")
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        lease_id, generation = conn.execute(
            "SELECT lease_id, generation FROM workspace_leases WHERE workspace_id='crash-recovery'"
        ).fetchone()
        planned = plugin_workspaces._planned_detached_path(
            ctx.workspaces._layout(), "crash-recovery", lease_id, generation, "acquire",
        )
        planned.mkdir()
        (planned / "detached.txt").write_text("detached", encoding="utf-8")
        conn.execute(
            """UPDATE workspace_leases SET state='preparing', owner_pid=-1, expires_at=0,
               cleanup_json=? WHERE workspace_id='crash-recovery'""",
            (json.dumps({
                "operation": "acquire", "disposition": "preparing",
                "planned_detached_path": str(planned),
            }),),
        )
    recovery_path = plugin_workspaces._planned_detached_path(
        ctx.workspaces._layout(), "crash-recovery", lease_id, generation, "recovery",
    )
    real_receipt = plugin_workspaces._cleanup_receipt
    fail_once = [True]

    def crash_before_receipt(conn, row, operation, phase, details):
        if operation == "recovery" and phase == "reconciled" and fail_once[0]:
            fail_once[0] = False
            raise RuntimeError("injected crash before recovery receipt commit")
        return real_receipt(conn, row, operation, phase, details)

    monkeypatch.setattr(plugin_workspaces, "_cleanup_receipt", crash_before_receipt)
    with pytest.raises(RuntimeError, match="injected crash"):
        _acquire(ctx, "crash-recovery")
    assert not canonical.exists()
    assert (recovery_path / "canonical.txt").read_text(encoding="utf-8") == "canonical"

    successor = _acquire(ctx, "crash-recovery")
    receipts = ctx.workspaces.inspect(successor)["cleanupReceipts"]
    reconciled = [receipt for receipt in receipts if receipt["operation"] == "recovery"][-1]
    paths = {
        item.get("quarantine_path")
        for item in reconciled["details"]["preserved"]
    }
    assert str(planned) in paths
    assert str(recovery_path) in paths


def test_crashed_release_resumes_detached_cleanup_and_receipt(tmp_path: Path) -> None:
    from hermes_cli import plugin_workspaces

    home = tmp_path / "home"
    ctx = _context(home)
    handle = _acquire(ctx, "crash-release")
    path = Path(ctx.workspaces.inspect(handle)["path"])
    (path / "unique.txt").write_text("preserve", encoding="utf-8")
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        lease_id, generation = conn.execute(
            "SELECT lease_id, generation FROM workspace_leases WHERE workspace_id='crash-release'"
        ).fetchone()
        planned = plugin_workspaces._planned_detached_path(
            ctx.workspaces._layout(), "crash-release", lease_id, generation, "release",
        )
        conn.execute(
            "UPDATE workspace_leases SET state='releasing', cleanup_json=? WHERE lease_id=?",
            (json.dumps({
                "operation": "release", "disposition": "releasing",
                "planned_detached_path": str(planned),
            }), lease_id),
        )
    os.replace(path, planned)  # crash after detach and before its receipt/state commit

    released = ctx.workspaces.release(handle)
    assert released["state"] == "released"
    assert released["cleanup"]["disposition"] == "quarantined"
    assert Path(released["cleanup"]["quarantine_path"], "unique.txt").exists()
    phases = [
        receipt["phase"] for receipt in released["cleanupReceipts"]
        if receipt["leaseId"] == handle["lease_id"] and receipt["operation"] == "release"
    ]
    assert phases[-2:] == ["released", "cleanup_completed"]


def test_released_missing_detached_target_is_terminally_reconciled(tmp_path: Path) -> None:
    from hermes_cli import plugin_workspaces

    home = tmp_path / "home"
    ctx = _context(home)
    handle = _acquire(ctx, "empty-release-crash")
    canonical = Path(ctx.workspaces.inspect(handle)["path"])
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        lease_id, generation = conn.execute(
            """SELECT lease_id, generation FROM workspace_leases
               WHERE workspace_id='empty-release-crash'"""
        ).fetchone()
        planned = plugin_workspaces._planned_detached_path(
            ctx.workspaces._layout(), "empty-release-crash", lease_id, generation, "release",
        )
        os.replace(canonical, planned)
        planned.rmdir()  # crash after physical cleanup, before completion receipt/state update
        conn.execute(
            """UPDATE workspace_leases SET state='released', released_at=1, cleanup_json=?
               WHERE lease_id=?""",
            (json.dumps({
                "operation": "release", "classification": "pending",
                "disposition": "detached", "planned_detached_path": str(planned),
            }), lease_id),
        )

    released = ctx.workspaces.release(handle)
    assert released["cleanup"]["disposition"] == "removed"
    assert released["cleanup"]["reconciled_reason"] == "detached_target_absent"
    phases = [
        receipt["phase"] for receipt in released["cleanupReceipts"]
        if receipt["leaseId"] == handle["lease_id"]
    ]
    assert phases[-1] == "cleanup_reconciled"
    assert ctx.workspaces.release(handle)["cleanup"] == released["cleanup"]


def test_release_permission_failure_stays_releasing_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    home = tmp_path / "home"
    ctx = _context(home)
    handle = _acquire(ctx, "permission-release")
    original = plugin_workspaces._HeldDirectory.rename_to

    def denied(*_args, **_kwargs):
        raise PermissionError("detach denied")

    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "rename_to", denied)
    with pytest.raises(WorkspacePathError, match="same handle can retry"):
        ctx.workspaces.release(handle)
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        state, cleanup_json = conn.execute(
            "SELECT state, cleanup_json FROM workspace_leases WHERE lease_id=?",
            (handle["lease_id"],),
        ).fetchone()
    assert state == "releasing"
    assert json.loads(cleanup_json)["disposition"] == "preserved"

    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "rename_to", original)
    released = ctx.workspaces.release(handle)
    assert released["state"] == "released"
    assert ctx.workspaces.release(handle)["state"] == "released"


def test_release_both_names_failure_stays_releasing_and_retries(tmp_path: Path) -> None:
    from hermes_cli import plugin_workspaces

    home = tmp_path / "home"
    ctx = _context(home)
    handle = _acquire(ctx, "both-release")
    snapshot = ctx.workspaces.inspect(handle)
    planned = plugin_workspaces._planned_detached_path(
        ctx.workspaces._layout(), "both-release", handle["lease_id"],
        snapshot["generation"], "release",
    )
    planned.mkdir()
    (planned / "other.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(WorkspacePathError, match="same handle can retry"):
        ctx.workspaces.release(handle)
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT state FROM workspace_leases WHERE lease_id=?", (handle["lease_id"],),
        ).fetchone()[0] == "releasing"
    planned.rename(planned.with_name("operator-preserved"))
    released = ctx.workspaces.release(handle)
    assert released["state"] == "released"


@pytest.mark.parametrize("owner_status", ["live", "unknown"])
def test_expired_reconnect_can_fence_live_or_unknown_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_status: str,
) -> None:
    from hermes_cli import plugin_workspaces

    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "expired-owner")
    monkeypatch.setattr(plugin_workspaces, "_owner_status", lambda _row: owner_status)
    with pytest.raises(WorkspaceOwnershipError, match="another live process"):
        _reconnect(ctx, handle)
    db = tmp_path / "home/plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        _expire_lease(conn, handle["lease_id"])
    successor = _reconnect(ctx, handle)
    assert ctx.workspaces.inspect(successor)["generation"] == 2


def test_database_wal_and_shm_are_private_under_permissive_umask(tmp_path: Path) -> None:
    from hermes_cli import plugin_workspaces

    ctx = _context(tmp_path / "home")
    previous_umask = os.umask(0)
    conn = None
    try:
        layout = ctx.workspaces._layout()
        conn = plugin_workspaces._connect(layout)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO workspace_lease_events VALUES (NULL, 'w', 'l', 1, 'test', 0, 1, NULL, '{}')"
        )
        for path in (
            layout.db_path, Path(str(layout.db_path) + "-wal"),
            Path(str(layout.db_path) + "-shm"),
        ):
            assert path.exists()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        conn.rollback()
    finally:
        if conn is not None:
            conn.close()
        os.umask(previous_umask)


def test_original_h1_database_upgrades_without_stranding_handle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    handle, workspace = _seed_original_h1_database(home)
    (workspace / "preserve.txt").write_text("legacy", encoding="utf-8")
    ctx = _context(home)

    inspected = ctx.workspaces.inspect(handle)
    assert inspected["workspaceId"] == "legacy-run"
    assert Path(inspected["path"], "preserve.txt").read_text(encoding="utf-8") == "legacy"
    successor = _reconnect(ctx, handle)
    assert ctx.workspaces.inspect(successor)["generation"] == 2
    released = ctx.workspaces.release(successor)
    assert released["state"] == "released"
    assert released["cleanup"]["disposition"] == "quarantined"

    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(workspace_leases)")}
        identity, heartbeat_mono, intent_id = conn.execute(
            """SELECT plugin_identity, heartbeat_monotonic, acquire_intent_id
               FROM workspace_leases WHERE workspace_id='legacy-run'"""
        ).fetchone()
    assert {
        "plugin_identity", "acquire_intent_id", "heartbeat_monotonic",
        "expires_monotonic", "expiry_observer", "expiry_observed_monotonic",
    } <= columns
    assert identity == ctx.workspaces._layout().plugin_identity
    assert heartbeat_mono is not None
    assert intent_id is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("plugin_namespace", "another-plugin"),
        ("profile_key", "/foreign/profile"),
        ("workspace_id", "legacy-run."),
        ("workspace_path", "/foreign/workspace"),
    ],
)
def test_original_h1_database_conflicts_are_never_claimed(
    tmp_path: Path, field: str, value: str,
) -> None:
    home = tmp_path / field
    kwargs = {field: value}
    handle, _workspace = _seed_original_h1_database(home, **kwargs)
    ctx = _context(home)

    with pytest.raises(WorkspacePathError, match="refusing to claim"):
        ctx.workspaces.inspect(handle)
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(workspace_leases)")}
        if "plugin_identity" in columns:
            identity = conn.execute(
                "SELECT plugin_identity FROM workspace_leases",
            ).fetchone()[0]
            assert identity is None


def test_inspect_holds_fence_across_release_and_reacquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    ctx = _context(tmp_path / "home")
    old = _acquire(ctx, "inspect-race")
    inspected = threading.Event()
    resume = threading.Event()
    original_validate = plugin_workspaces._validated_row
    pause_once = [True]

    def paused_validate(conn, layout, handle, *, validate_path=True):
        row = original_validate(conn, layout, handle, validate_path=validate_path)
        if handle.get("lease_id") == old["lease_id"] and pause_once[0]:
            pause_once[0] = False
            inspected.set()
            assert resume.wait(5)
        return row

    monkeypatch.setattr(plugin_workspaces, "_validated_row", paused_validate)

    def replace_generation():
        ctx.workspaces.release(old)
        return _acquire(ctx, "inspect-race")

    with ThreadPoolExecutor(max_workers=2) as pool:
        inspection = pool.submit(ctx.workspaces.inspect, old)
        assert inspected.wait(5)
        replacement = pool.submit(replace_generation)
        resume.set()
        old_snapshot = inspection.result(timeout=5)
        successor = replacement.result(timeout=5)

    assert old_snapshot["leaseId"] == old["lease_id"]
    assert old_snapshot["generation"] == 1
    assert ctx.workspaces.inspect(successor)["generation"] == 2


def test_acquire_response_ambiguity_replays_exact_handle_from_new_process(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    ctx = _context(home)
    intent = ctx.workspaces.new_intent()
    script = r"""
import json
import os
import sys
from hermes_cli import plugin_workspaces
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

intent = json.loads(sys.argv[1])
ctx = PluginContext(PluginManifest(name='pr-review'), PluginManager())
plugin_workspaces._response_handle = lambda *_args: os._exit(73)
ctx.workspaces.acquire('ambiguous-acquire', intent=intent)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, json.dumps(intent)], cwd=PROJECT_ROOT,
        env=_child_env(home), capture_output=True, text=True, timeout=30,
    )
    assert child.returncode == 73

    replayed = _acquire(ctx, "ambiguous-acquire", intent=intent)
    assert replayed["capability"] == intent["capability"]
    assert ctx.workspaces.inspect(replayed)["state"] == "active"


def test_reconnect_response_ambiguity_replays_successor_from_new_process(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    ctx = _context(home)
    predecessor = _acquire(ctx, "ambiguous-reconnect")
    intent = ctx.workspaces.new_intent()
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        _expire_lease(conn, predecessor["lease_id"])
    script = r"""
import json
import os
import sys
from hermes_cli import plugin_workspaces
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

predecessor, intent = json.loads(sys.argv[1]), json.loads(sys.argv[2])
ctx = PluginContext(PluginManifest(name='pr-review'), PluginManager())
plugin_workspaces._response_handle = lambda *_args: os._exit(74)
ctx.workspaces.reconnect(predecessor, intent=intent)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, json.dumps(predecessor), json.dumps(intent)],
        cwd=PROJECT_ROOT, env=_child_env(home), capture_output=True, text=True, timeout=30,
    )
    assert child.returncode == 74

    successor = _reconnect(ctx, predecessor, intent=intent)
    assert successor["capability"] == intent["capability"]
    assert ctx.workspaces.inspect(successor)["generation"] == 2
    with pytest.raises(InvalidWorkspaceHandleError):
        ctx.workspaces.inspect(predecessor)


@pytest.mark.parametrize(
    "fault_point", ["before_second_transaction", "after_mkdir", "before_activation_commit"],
)
def test_matching_acquire_intent_resumes_synchronous_preparation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_point: str,
) -> None:
    from hermes_cli import plugin_workspaces

    ctx = _context(tmp_path / fault_point)
    intent = ctx.workspaces.new_intent()
    restore = None
    if fault_point == "before_second_transaction":
        original = plugin_workspaces._connect
        calls = [0]

        def fail_second(layout):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("before second transaction")
            return original(layout)

        monkeypatch.setattr(plugin_workspaces, "_connect", fail_second)
        restore = lambda: monkeypatch.setattr(plugin_workspaces, "_connect", original)
    elif fault_point == "after_mkdir":
        original = plugin_workspaces._HeldDirectory.mkdir

        def fail_sync(self, name, mode=0o700):
            original(self, name, mode)
            raise RuntimeError("after mkdir")

        monkeypatch.setattr(plugin_workspaces._HeldDirectory, "mkdir", fail_sync)
        restore = lambda: monkeypatch.setattr(
            plugin_workspaces._HeldDirectory, "mkdir", original,
        )
    else:
        original = plugin_workspaces._event

        def fail_activation(conn, row, event_type, details=None):
            if event_type == "acquired":
                raise RuntimeError("before activation commit")
            return original(conn, row, event_type, details)

        monkeypatch.setattr(plugin_workspaces, "_event", fail_activation)
        restore = lambda: monkeypatch.setattr(plugin_workspaces, "_event", original)

    with pytest.raises(RuntimeError):
        _acquire(ctx, "retryable", intent=intent)
    restore()
    handle = _acquire(ctx, "retryable", intent=intent)
    assert handle["capability"] == intent["capability"]
    assert ctx.workspaces.inspect(handle)["state"] == "active"


def test_filesystem_transitions_are_synced_before_state_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    ctx = _context(tmp_path / "home")
    calls: list[Path] = []
    original = plugin_workspaces._HeldDirectory.sync

    def recording_sync(self):
        calls.append(self.path)
        return original(self)

    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "sync", recording_sync)
    first = _acquire(ctx, "durable")
    workspace = Path(ctx.workspaces.inspect(first)["path"])
    (workspace / "keep.txt").write_text("preserve", encoding="utf-8")
    ctx.workspaces.release(first)
    layout = ctx.workspaces._layout()
    assert layout.workspaces_dir in calls
    assert layout.quarantine_dir in calls


def test_detach_sync_fault_is_retryable_with_same_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    home = tmp_path / "home"
    ctx = _context(home)
    predecessor = _acquire(ctx, "detach-fault")
    predecessor_path = Path(ctx.workspaces.inspect(predecessor)["path"])
    (predecessor_path / "preserve.txt").write_text("old", encoding="utf-8")
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        _expire_lease(conn, predecessor["lease_id"])
    intent = ctx.workspaces.new_intent()
    original = plugin_workspaces._HeldDirectory.sync
    fail_once = [True]

    def fail_after_rename(self):
        if fail_once[0]:
            fail_once[0] = False
            raise OSError("power loss after rename")
        return original(self)

    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "sync", fail_after_rename)
    with pytest.raises(plugin_workspaces.WorkspaceDurabilityError) as raised:
        _acquire(ctx, "detach-fault", intent=intent)
    assert raised.value.mutation_completed is True
    with sqlite3.connect(db) as conn:
        state, cleanup_json = conn.execute(
            "SELECT state, cleanup_json FROM workspace_leases WHERE workspace_id='detach-fault'"
        ).fetchone()
    assert state == "preparing"
    deterministic = Path(json.loads(cleanup_json)["planned_detached_path"])
    assert (deterministic / "preserve.txt").read_text(encoding="utf-8") == "old"
    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "sync", original)

    successor = _acquire(ctx, "detach-fault", intent=intent)
    snapshot = ctx.workspaces.inspect(successor)
    preserved = [
        item["details"] for item in snapshot["cleanupReceipts"]
        if item["operation"] == "recovery"
    ]
    assert preserved
    quarantine_paths = {
        entry.get("quarantine_path")
        for receipt in preserved
        for entry in receipt.get("preserved", [])
    }
    assert any(
        path and Path(path, "preserve.txt").exists()
        for path in quarantine_paths
    )


def test_mkdir_strict_flush_failure_never_commits_active_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    home = tmp_path / "home"
    ctx = _context(home)
    intent = ctx.workspaces.new_intent()
    original = plugin_workspaces._HeldDirectory.sync
    fail_once = [True]

    def fail_workspace_dir(self):
        if self.path.name == "workspaces" and fail_once[0]:
            fail_once[0] = False
            raise OSError("strict directory flush failed")
        return original(self)

    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "sync", fail_workspace_dir)
    with pytest.raises(plugin_workspaces.WorkspacePathError, match="flush failed"):
        _acquire(ctx, "mkdir-flush", intent=intent)
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        state = conn.execute(
            "SELECT state FROM workspace_leases WHERE workspace_id='mkdir-flush'"
        ).fetchone()[0]
    assert state == "released"
    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "sync", original)
    assert ctx.workspaces.inspect(_acquire(ctx, "mkdir-flush", intent=intent))["state"] == "active"


def test_rmdir_flush_failure_reconciles_completed_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "rmdir-flush")
    snapshot = ctx.workspaces.inspect(handle)
    layout = ctx.workspaces._layout()
    planned = plugin_workspaces._planned_detached_path(
        layout, "rmdir-flush", handle["lease_id"], snapshot["generation"], "release",
    )
    original = plugin_workspaces._HeldDirectory.sync

    def fail_after_rmdir(self):
        if self.path == layout.quarantine_dir and not planned.exists():
            raise OSError("rmdir metadata flush failed")
        return original(self)

    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "sync", fail_after_rmdir)
    with pytest.raises(plugin_workspaces.WorkspaceDurabilityError) as raised:
        ctx.workspaces.release(handle)
    assert raised.value.mutation_completed is True
    monkeypatch.setattr(plugin_workspaces._HeldDirectory, "sync", original)
    released = ctx.workspaces.release(handle)
    assert released["cleanup"]["disposition"] == "removed"
    assert released["cleanup"]["reconciled_reason"] == "detached_target_absent"


@pytest.mark.windows_only
def test_windows_strict_metadata_flush_and_write_through_move(tmp_path: Path) -> None:
    from hermes_cli import plugin_workspace_fs

    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    plugin_workspace_fs.strict_sync_directory(source)
    plugin_workspace_fs.strict_replace(source, target, tmp_path)
    assert target.is_dir() and not source.exists()


def test_live_pid_is_not_killed_by_unstable_wall_clock_boot_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    created = plugin_workspaces._process_create_time()
    row = {
        "owner_pid": os.getpid(), "owner_create_time": created,
        "owner_host": plugin_workspaces.socket.gethostname(),
        "owner_machine_identity": "unverified",
        "owner_instance": "unverified",
    }
    monkeypatch.setattr(
        plugin_workspaces.Path, "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no stable boot id")),
    )
    monkeypatch.setattr(plugin_workspaces, "_pid_alive_matches", lambda *_args: True)
    monkeypatch.setattr(plugin_workspaces, "_process_create_time", lambda: created)
    assert plugin_workspaces._host_instance() == "unverified"
    assert plugin_workspaces._owner_status(row) == "unknown"


def test_slow_release_cleanup_records_old_generation_after_successor_acquires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    ctx = _context(tmp_path / "home")
    old = _acquire(ctx, "slow-release")
    entered = threading.Event()
    resume = threading.Event()
    original = plugin_workspaces._finish_detached_cleanup

    def blocked_cleanup(path, receipt):
        entered.set()
        assert resume.wait(5)
        return original(path, receipt)

    monkeypatch.setattr(plugin_workspaces, "_finish_detached_cleanup", blocked_cleanup)
    with ThreadPoolExecutor(max_workers=1) as pool:
        releasing = pool.submit(ctx.workspaces.release, old)
        assert entered.wait(5)
        successor = _acquire(ctx, "slow-release")
        successor_path = Path(ctx.workspaces.inspect(successor)["path"])
        (successor_path / "successor.txt").write_text("safe", encoding="utf-8")
        resume.set()
        released = releasing.result(timeout=5)

    assert released["cleanup"]["disposition"] == "removed"
    assert (successor_path / "successor.txt").read_text(encoding="utf-8") == "safe"
    receipts = ctx.workspaces.inspect(successor)["cleanupReceipts"]
    assert any(
        receipt["leaseId"] == old["lease_id"]
        and receipt["phase"] == "cleanup_completed"
        for receipt in receipts
    )
    assert any(
        event["leaseId"] == old["lease_id"]
        and event["type"] == "cleanup_completed"
        for event in released["events"]
    )


def test_empty_cleanup_race_quarantines_late_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspaces

    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "late-file")
    original = plugin_workspaces._classify_workspace
    injected = [False]

    def classify_then_write(path):
        assessment = original(path)
        if assessment["classification"] == "clean_empty" and not injected[0]:
            injected[0] = True
            (path / "late.txt").write_text("preserve", encoding="utf-8")
        return assessment

    monkeypatch.setattr(plugin_workspaces, "_classify_workspace", classify_then_write)
    released = ctx.workspaces.release(handle)
    assert released["cleanup"]["disposition"] == "quarantined"
    assert released["cleanup"]["cleanup_race"] == "late_content"
    assert Path(released["cleanup"]["quarantine_path"], "late.txt").exists()


def test_host_feature_probe_and_validator_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _context(tmp_path / "home")
    assert ctx.has_host_feature(HOST_FEATURE) is True
    assert ctx.has_host_feature("workspace_leases.future") is False

    from hermes_cli.plugin_validate import _run_capability_probe

    plugin = tmp_path / "probe-plugin"
    plugin.mkdir()
    source = (
        "def register(ctx):\n"
        "    probe = getattr(ctx, 'has_host_feature', None)\n"
        "    if not callable(probe) or not probe('workspace_leases.v1'):\n"
        "        ctx.register_tool('review-only')\n"
        "        return\n"
        "    from hermes_cli.plugin_workspaces import WorkspaceLeaseError\n"
        "    raise WorkspaceLeaseError('validator must report host feature unavailable')\n"
    )
    (plugin / "__init__.py").write_text(source, encoding="utf-8")
    recorded, error = _run_capability_probe(plugin, {"name": "probe-plugin"})
    assert error == ""
    assert recorded is not None
    assert recorded["tools"] == ["review-only"]

    import builtins

    registered: list[str] = []

    class OldHostContext:
        def register_tool(self, name, *args, **kwargs):
            registered.append(name)

    real_import = builtins.__import__

    def old_host_import(name, *args, **kwargs):
        if name == "hermes_cli.plugin_workspaces":
            raise AssertionError("lease module import attempted on old host")
        return real_import(name, *args, **kwargs)

    namespace: dict[str, object] = {}
    monkeypatch.setattr(builtins, "__import__", old_host_import)
    exec(compile(source, "old-host-plugin/__init__.py", "exec"), namespace)
    namespace["register"](OldHostContext())
    assert registered == ["review-only"]
