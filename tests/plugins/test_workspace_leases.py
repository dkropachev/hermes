"""Behavior contract for the durable plugin workspace lifecycle (issue #6)."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
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


def test_handle_is_opaque_serializable_and_profile_bound(tmp_path: Path) -> None:
    home_a, home_b = tmp_path / "profiles" / "a", tmp_path / "profiles" / "b"
    ctx = _context(home_a)
    handle = ctx.workspaces.acquire("run-1")

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
    handle = ctx.workspaces.acquire("run-1")
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

    successor = ctx.workspaces.reconnect(handle)
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
    handle = _context(home).workspaces.acquire("shared", ttl_seconds=60)
    script = """
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.plugin_workspaces import WorkspaceInUseError
ctx = PluginContext(PluginManifest(name='pr-review'), PluginManager())
try:
    ctx.workspaces.acquire('shared', ttl_seconds=60)
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
from pathlib import Path
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.plugin_workspaces import WorkspaceInUseError

ready, outcome, gate, release = (Path(value) for value in sys.argv[1:])
ctx = PluginContext(PluginManifest(name='pr-review'), PluginManager())
ready.touch()
deadline = time.monotonic() + 10
while not gate.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('timed out waiting for acquisition gate')
    time.sleep(0.01)
try:
    ctx.workspaces.acquire('simultaneous', ttl_seconds=60)
except WorkspaceInUseError:
    outcome.write_text('refused', encoding='utf-8')
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
        deadline = time.monotonic() + 10
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
        stdout, stderr = process.communicate(timeout=10)
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
handle = ctx.workspaces.acquire('restart-safe', ttl_seconds=3600)
Path(ctx.workspaces.inspect(handle)['path'], 'work.txt').write_text('preserve me', encoding='utf-8')
print(json.dumps(handle))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=PROJECT_ROOT, env=_child_env(home),
        check=True, capture_output=True, text=True, timeout=30,
    )
    predecessor = json.loads(result.stdout.strip().splitlines()[-1])

    ctx = _context(home)
    successor = ctx.workspaces.reconnect(predecessor)
    snapshot = ctx.workspaces.inspect(successor)
    assert Path(snapshot["path"], "work.txt").read_text(encoding="utf-8") == "preserve me"
    assert snapshot["events"][-1]["type"] == "reconnected"
    with pytest.raises(InvalidWorkspaceHandleError):
        ctx.workspaces.inspect(predecessor)


def test_expired_generation_is_atomically_reclaimed(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    old = ctx.workspaces.acquire("ttl", ttl_seconds=60)
    old_path = Path(ctx.workspaces.inspect(old)["path"])
    (old_path / "untracked.txt").write_text("old", encoding="utf-8")
    db = tmp_path / "home/plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE workspace_leases SET expires_at=0 WHERE lease_id=?", (old["lease_id"],))

    successor = ctx.workspaces.acquire("ttl", ttl_seconds=60)
    snapshot = ctx.workspaces.inspect(successor)
    assert snapshot["generation"] == 2
    assert not Path(snapshot["path"], "untracked.txt").exists()
    assert snapshot["cleanup"]["disposition"] == "quarantined"
    assert Path(snapshot["cleanup"]["quarantine_path"], "untracked.txt").exists()
    with pytest.raises(InvalidWorkspaceHandleError):
        ctx.workspaces.inspect(old)


def test_expired_handle_requires_reconnect_for_direct_use(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    handle = ctx.workspaces.acquire("expired", ttl_seconds=60)
    db = tmp_path / "home/plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE workspace_leases SET expires_at=0 WHERE lease_id=?", (handle["lease_id"],))

    with pytest.raises(WorkspaceLeaseExpiredError):
        ctx.workspaces.inspect(handle)
    successor = ctx.workspaces.reconnect(handle)
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
    handle = ctx.workspaces.acquire("aliased")
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
        _context(home).workspaces.acquire("run-1")


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
        handle = ctx.workspaces.acquire(expected)
        path = Path(ctx.workspaces.inspect(handle)["path"])
        setup(path)
        cleanup = ctx.workspaces.release(handle)["cleanup"]
        assert cleanup["classification"] == expected
        assert cleanup["disposition"] == "quarantined"
        assert Path(cleanup["quarantine_path"]).exists()

    empty = ctx.workspaces.acquire("empty")
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
    handle = ctx.workspaces.acquire(f"hidden-{hidden_ref}")
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
    portable_handle = portable.workspaces.acquire("run")
    native_handle = native.workspaces.acquire("run")
    portable_path = Path(portable.workspaces.inspect(portable_handle)["path"])
    native_path = Path(native.workspaces.inspect(native_handle)["path"])

    assert portable_path.parent.parent.name == portable_namespace
    assert native_path.parent.parent.name.startswith("hermes-native-")
    assert portable_path != native_path
    assert _context(home).workspaces.acquire("exact-pr-review")
    assert (home / "plugin-data/pr-review/workspaces/exact-pr-review").is_dir()
    portable_db = portable_path.parents[1] / "workspace-leases.db"
    with sqlite3.connect(portable_db) as conn:
        conn.execute(
            "UPDATE workspace_leases SET plugin_identity='forged' WHERE lease_id=?",
            (portable_handle["lease_id"],),
        )
    with pytest.raises(InvalidWorkspaceHandleError):
        portable.workspaces.inspect(portable_handle)


def test_generated_native_namespace_cannot_collide_with_literal_native_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    generated = _context(home, "MixedCasePlugin")
    generated_handle = generated.workspaces.acquire("run")
    generated_path = Path(generated.workspaces.inspect(generated_handle)["path"])
    generated_namespace = generated_path.parent.parent.name
    literal = _context(home, generated_namespace)
    literal_handle = literal.workspaces.acquire("run")
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
    old = ctx.workspaces.acquire("slow", ttl_seconds=60)
    old_path = Path(ctx.workspaces.inspect(old)["path"])
    (old_path / "preserve.txt").write_text("old", encoding="utf-8")
    db = tmp_path / "home/plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE workspace_leases SET expires_at=5 WHERE lease_id=?", (old["lease_id"],))

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
    monkeypatch.setattr(plugin_workspaces, "transaction", contended_transaction)
    monkeypatch.setattr(plugin_workspaces, "_finish_detached_cleanup", slow_finish)
    successor = ctx.workspaces.acquire("slow", ttl_seconds=1)
    snapshot = ctx.workspaces.inspect(successor)
    assert snapshot["heartbeatAt"] == 20.0
    assert snapshot["expiresAt"] == 21.0
    assert snapshot["generation"] == 2


def test_crashed_acquire_reconciles_both_names_and_keeps_receipts(tmp_path: Path) -> None:
    from hermes_cli import plugin_workspaces

    home = tmp_path / "home"
    ctx = _context(home)
    failed = ctx.workspaces.acquire("crash-acquire")
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

    successor = ctx.workspaces.acquire("crash-acquire")
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
    failed = ctx.workspaces.acquire("crash-recovery")
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
        ctx.workspaces.acquire("crash-recovery")
    assert not canonical.exists()
    assert (recovery_path / "canonical.txt").read_text(encoding="utf-8") == "canonical"

    successor = ctx.workspaces.acquire("crash-recovery")
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
    handle = ctx.workspaces.acquire("crash-release")
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
    handle = ctx.workspaces.acquire("empty-release-crash")
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


@pytest.mark.parametrize("owner_status", ["live", "unknown"])
def test_expired_reconnect_can_fence_live_or_unknown_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_status: str,
) -> None:
    from hermes_cli import plugin_workspaces

    ctx = _context(tmp_path / "home")
    handle = ctx.workspaces.acquire("expired-owner")
    monkeypatch.setattr(plugin_workspaces, "_owner_status", lambda _row: owner_status)
    with pytest.raises(WorkspaceOwnershipError, match="another live process"):
        ctx.workspaces.reconnect(handle)
    db = tmp_path / "home/plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE workspace_leases SET expires_at=0 WHERE lease_id=?", (handle["lease_id"],))
    successor = ctx.workspaces.reconnect(handle)
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
