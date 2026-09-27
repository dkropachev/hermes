"""Behavior contract for the durable plugin workspace lifecycle (issue #6)."""

from __future__ import annotations

import json
import os
import sqlite3
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
)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _context(home: Path, plugin_id: str = "pr-review") -> PluginContext:
    home.mkdir(parents=True, exist_ok=True)
    return PluginContext(
        PluginManifest(name=plugin_id, key=plugin_id),
        PluginManager(scope_key=hermes_home_key(home)),
    )


def _child_env(home: Path) -> dict[str, str]:
    return {**os.environ, "HERMES_HOME": str(home)}


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


def test_handle_validation_token_rotation_and_idempotent_release(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    for invalid_id in ("", "../escape", "UPPER", "a/b", "con", "run.", "a" * 129):
        with pytest.raises(ValueError, match="workspace_id"):
            ctx.workspaces.acquire(invalid_id)

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


def test_foreign_process_cannot_reconnect_live_owner_after_ttl(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = _context(home)
    handle = ctx.workspaces.acquire("shared", ttl_seconds=60)
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE workspace_leases SET expires_at=0 WHERE lease_id=?", (handle["lease_id"],))

    script = """
import json
import sys
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_cli.plugin_workspaces import WorkspaceOwnershipError
ctx = PluginContext(PluginManifest(name='pr-review'), PluginManager())
try:
    ctx.workspaces.reconnect(json.loads(sys.argv[1]))
except WorkspaceOwnershipError:
    print('refused')
else:
    print('reconnected')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(handle)], cwd=PROJECT_ROOT,
        env=_child_env(home), check=True, capture_output=True, text=True, timeout=30,
    )
    assert result.stdout.strip().splitlines()[-1] == "refused"
    assert ctx.workspaces.inspect(ctx.workspaces.reconnect(handle))["state"] == "active"


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


def test_process_and_boot_witnesses_make_persisted_owners_reclaimable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    ctx = _context(home)
    db = home / "plugin-data/pr-review/workspace-leases.db"

    stale_process = ctx.workspaces.acquire("stale-process")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE workspace_leases SET owner_create_time=0 "
            "WHERE lease_id=?",
            (stale_process["lease_id"],),
        )
    process_snapshot = ctx.workspaces.inspect(ctx.workspaces.reconnect(stale_process))
    assert process_snapshot["events"][-1]["details"]["previous_owner"] == "dead"

    stale_boot = ctx.workspaces.acquire("stale-boot")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE workspace_leases SET owner_instance='boot-id:previous-boot' "
            "WHERE lease_id=?",
            (stale_boot["lease_id"],),
        )
    boot_snapshot = ctx.workspaces.inspect(ctx.workspaces.reconnect(stale_boot))
    assert boot_snapshot["events"][-1]["details"]["previous_owner"] == "dead"

    # Losing access to the current boot witness is not evidence of a reboot.
    import hermes_cli.plugin_workspaces as workspace_module

    current = ctx.workspaces.acquire("current-boot")
    monkeypatch.setattr(workspace_module, "_host_instance", lambda: "unverified")
    assert ctx.workspaces.renew(current)["state"] == "active"


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


def test_stale_preparation_and_release_receipts_are_recovered(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = _context(home)
    db = home / "plugin-data/pr-review/workspace-leases.db"
    quarantine = home / "plugin-data/pr-review/workspace-quarantine"

    preparing = ctx.workspaces.acquire("preparing-crash")
    canonical = Path(ctx.workspaces.inspect(preparing)["path"])
    (canonical / "predecessor.txt").write_text("predecessor", encoding="utf-8")
    planned = quarantine / ".detached-preparing-crash-crashed"
    canonical.replace(planned)
    canonical.mkdir()
    (canonical / "unpublished.txt").write_text("unpublished", encoding="utf-8")
    receipt = {
        "classification": "pending",
        "disposition": "preparing",
        "original_path": str(canonical),
        "planned_detached_path": str(planned),
    }
    with sqlite3.connect(db) as conn:
        conn.execute(
            """UPDATE workspace_leases SET state='preparing', owner_pid=99999999,
               owner_create_time=0, expires_at=0, cleanup_json=? WHERE lease_id=?""",
            (json.dumps(receipt), preparing["lease_id"]),
        )

    successor = ctx.workspaces.acquire("preparing-crash")
    recovered = ctx.workspaces.inspect(successor)["cleanup"]["recovered_cleanup"]
    recovered_files = {
        child.name
        for item in recovered
        if item.get("disposition") == "quarantined"
        for child in Path(item["quarantine_path"]).iterdir()
    }
    assert recovered_files == {"predecessor.txt", "unpublished.txt"}

    releasing = ctx.workspaces.acquire("release-crash")
    release_path = Path(ctx.workspaces.inspect(releasing)["path"])
    (release_path / "work.txt").write_text("keep", encoding="utf-8")
    release_target = quarantine / ".detached-release-crash-crashed"
    release_path.replace(release_target)
    release_receipt = {
        "classification": "pending",
        "disposition": "releasing",
        "original_path": str(release_path),
        "planned_detached_path": str(release_target),
    }
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE workspace_leases SET state='releasing', cleanup_json=? WHERE lease_id=?",
            (json.dumps(release_receipt), releasing["lease_id"]),
        )
    released = ctx.workspaces.release(releasing)
    assert released["cleanup"]["disposition"] == "quarantined"
    assert Path(released["cleanup"]["quarantine_path"], "work.txt").is_file()


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


def test_expiry_is_sampled_after_transaction_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.plugin_workspaces as workspace_module

    home = tmp_path / "home"
    ctx = _context(home)
    handle = ctx.workspaces.acquire("lock-delayed")
    db = home / "plugin-data/pr-review/workspace-leases.db"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE workspace_leases SET expires_at=5 WHERE lease_id=?", (handle["lease_id"],))

    entered = False
    real_transaction = workspace_module.transaction

    @contextmanager
    def observed_transaction(conn, *, immediate=False):
        nonlocal entered
        with real_transaction(conn, immediate=immediate) as active:
            entered = True
            yield active

    monkeypatch.setattr(workspace_module, "transaction", observed_transaction)
    monkeypatch.setattr(workspace_module.time, "time", lambda: 10.0 if entered else 1.0)
    with pytest.raises(WorkspaceLeaseExpiredError):
        ctx.workspaces.renew(handle)


def test_cleanup_deletes_only_empty_and_quarantines_every_nonempty_tree(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")

    nonempty = ctx.workspaces.acquire("nonempty")
    nonempty_path = Path(ctx.workspaces.inspect(nonempty)["path"])
    (nonempty_path / "work.txt").write_text("data", encoding="utf-8")
    cleanup = ctx.workspaces.release(nonempty)["cleanup"]
    assert cleanup["classification"] == "nonempty"
    assert cleanup["disposition"] == "quarantined"
    assert Path(cleanup["quarantine_path"], "work.txt").is_file()

    empty = ctx.workspaces.acquire("empty")
    empty_path = Path(ctx.workspaces.inspect(empty)["path"])
    cleanup = ctx.workspaces.release(empty)["cleanup"]
    assert cleanup["classification"] == "clean_empty"
    assert cleanup["disposition"] == "removed"
    assert not empty_path.exists()


def test_host_feature_probe(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    assert ctx.has_host_feature(HOST_FEATURE) is True
    assert ctx.has_host_feature("workspace_leases.future") is False
