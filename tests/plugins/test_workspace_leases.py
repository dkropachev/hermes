"""Behavior contract for the durable plugin workspace lifecycle (issue #6)."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override
from hermes_cli.plugin_workspaces import (
    HOST_FEATURE,
    InvalidWorkspaceHandleError,
    WorkspaceInUseError,
    WorkspaceLeaseExpiredError,
    WorkspacePathError,
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
        if expected == "clean_git":
            assert cleanup["disposition"] == "removed"
            assert not path.exists()
        else:
            assert cleanup["disposition"] == "quarantined"
            assert Path(cleanup["quarantine_path"]).exists()

    empty = ctx.workspaces.acquire("empty")
    empty_path = Path(ctx.workspaces.inspect(empty)["path"])
    cleanup = ctx.workspaces.release(empty)["cleanup"]
    assert cleanup["classification"] == "clean_empty"
    assert cleanup["disposition"] == "removed"
    assert not empty_path.exists()


def test_host_feature_probe_and_validator_fallback(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    assert ctx.has_host_feature(HOST_FEATURE) is True
    assert ctx.has_host_feature("workspace_leases.future") is False

    from hermes_cli.plugin_validate import _run_capability_probe

    plugin = tmp_path / "probe-plugin"
    plugin.mkdir()
    (plugin / "__init__.py").write_text(
        "def register(ctx):\n"
        "    if ctx.has_host_feature('workspace_leases.v1'):\n"
        "        raise RuntimeError('validator must report host feature unavailable')\n",
        encoding="utf-8",
    )
    recorded, error = _run_capability_probe(plugin, {"name": "probe-plugin"})
    assert error == ""
    assert recorded is not None
