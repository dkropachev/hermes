"""Behavior contract for cooperative lease-bound plugin tool dispatch."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_constants import hermes_home_key
from hermes_cli.plugin_workspaces import (
    InvalidWorkspaceHandleError,
    WorkspaceLeaseExpiredError,
)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _context(home: Path, plugin_id: str = "pr-review") -> PluginContext:
    home.mkdir(parents=True, exist_ok=True)
    return PluginContext(
        PluginManifest(name=plugin_id, key=plugin_id),
        PluginManager(scope_key=hermes_home_key(home)),
    )


def _acquire(ctx: PluginContext, workspace_id: str = "run-1") -> dict:
    return ctx.workspaces.acquire(
        workspace_id,
        intent=ctx.workspaces.new_intent(),
        ttl_seconds=60,
    )


@contextmanager
def _fake_file_ops(_root):
    yield object()


def test_dispatch_uses_host_derived_paths_and_canonical_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cli.plugin_workspace_dispatch as dispatch
    import tools.file_tools as file_tools
    import tools.terminal_tool as terminal_tool
    from tools.registry import registry

    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx)
    root = Path(ctx.workspaces.inspect(handle)["path"])
    calls: list[tuple[str, tuple, dict]] = []

    monkeypatch.setattr(dispatch, "_local_file_operations", _fake_file_ops)
    monkeypatch.setattr(
        registry,
        "dispatch",
        lambda *args, **kwargs: pytest.fail(
            "workspace dispatch used overrideable registry"
        ),
    )

    def capture(name):
        def handler(*args, **kwargs):
            calls.append((name, args, kwargs))
            return json.dumps({"ok": True})

        return handler

    monkeypatch.setattr(terminal_tool, "terminal_tool", capture("terminal"))
    monkeypatch.setattr(file_tools, "read_file_tool", capture("read"))
    monkeypatch.setattr(file_tools, "write_file_tool", capture("write"))
    monkeypatch.setattr(file_tools, "patch_tool", capture("edit"))

    assert ctx.workspace_tools is ctx.workspace_tools
    ctx.workspace_tools.terminal(handle, "git status", timeout=30)
    ctx.workspace_tools.read_file(handle, "src/main.py", offset=2, limit=7)
    ctx.workspace_tools.write_file(handle, "src/main.py", "new")
    ctx.workspace_tools.edit_file(handle, "src/main.py", "old", "new", replace_all=True)

    terminal = calls[0][2]
    assert terminal["workdir"] == str(root)
    assert terminal["background"] is False
    assert terminal["_host_local"] is True
    assert terminal["_allow_yield_to_background"] is False
    assert terminal["timeout"] == 30
    assert "force" not in terminal and "pty" not in terminal

    expected = str(root / "src" / "main.py")
    task_ids = {terminal["task_id"]}
    for name, args, kwargs in calls[1:]:
        actual_path = kwargs["path"] if name == "edit" else args[0]
        assert actual_path == expected
        assert kwargs["_resolved_path"] == expected
        assert kwargs["_file_ops"] is not None
        task_ids.add(kwargs["task_id"])
    assert len(task_ids) == 1
    assert calls[-1][2]["mode"] == "replace"


@pytest.mark.parametrize(
    "bad_path",
    [
        "/tmp/out",
        "C:\\temp\\out",
        "C:relative",
        "\\\\server\\share\\out",
        "../out",
        "a/../../out",
    ],
)
def test_file_paths_reject_absolute_drive_unc_and_traversal(
    tmp_path: Path,
    bad_path: str,
) -> None:
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx)
    with pytest.raises(ValueError, match="relative_path|leased workspace"):
        ctx.workspace_tools.read_file(handle, bad_path)


@pytest.mark.parametrize(
    "method, kwargs",
    [
        ("terminal", {"command": "pwd", "workdir": "/tmp"}),
        ("terminal", {"command": "pwd", "background": True}),
        ("terminal", {"command": "pwd", "task_id": "foreign"}),
        ("terminal", {"command": "pwd", "pty": True}),
        ("terminal", {"command": "pwd", "force": True}),
        ("write_file", {"relative_path": "x", "content": "x", "cross_profile": True}),
    ],
)
def test_callers_cannot_supply_dispatch_routing_overrides(
    tmp_path: Path,
    method: str,
    kwargs: dict,
) -> None:
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx)
    with pytest.raises(TypeError, match="unexpected keyword"):
        getattr(ctx.workspace_tools, method)(handle, **kwargs)


def test_stale_foreign_and_expired_handles_fail_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.terminal_tool as terminal_tool

    home = tmp_path / "home"
    ctx = _context(home)
    stale = _acquire(ctx, "stale")
    current = ctx.workspaces.reconnect(
        stale,
        intent=ctx.workspaces.new_intent(),
        ttl_seconds=60,
    )
    foreign_plugin = _context(home, "other")
    foreign_profile = _context(tmp_path / "other-home")
    expired = _acquire(ctx, "expired")
    db = home / "plugin-data" / "pr-review" / "workspace-leases.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE workspace_leases SET expires_at=0 WHERE lease_id=?",
            (expired["lease_id"],),
        )

    monkeypatch.setattr(
        terminal_tool,
        "terminal_tool",
        lambda *args, **kwargs: pytest.fail("invalid handle reached terminal"),
    )
    with pytest.raises(InvalidWorkspaceHandleError):
        ctx.workspace_tools.terminal(stale, "pwd")
    with pytest.raises(InvalidWorkspaceHandleError):
        foreign_plugin.workspace_tools.terminal(current, "pwd")
    with pytest.raises(InvalidWorkspaceHandleError):
        foreign_profile.workspace_tools.terminal(current, "pwd")
    with pytest.raises(WorkspaceLeaseExpiredError):
        ctx.workspace_tools.terminal(expired, "pwd")


def test_terminal_rejects_timeout_that_would_promote_to_background(
    tmp_path: Path,
) -> None:
    from tools.terminal_tool import FOREGROUND_MAX_TIMEOUT

    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx)
    with pytest.raises(ValueError, match="timeout"):
        ctx.workspace_tools.terminal(
            handle,
            "pwd",
            timeout=FOREGROUND_MAX_TIMEOUT + 1,
        )


def test_dispatch_pin_blocks_release_until_synchronous_call_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.terminal_tool as terminal_tool

    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "pinned")
    entered = threading.Event()
    finish = threading.Event()
    released = threading.Event()

    def blocking_terminal(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return json.dumps({"exit_code": 0})

    monkeypatch.setattr(terminal_tool, "terminal_tool", blocking_terminal)
    dispatch_thread = threading.Thread(
        target=lambda: ctx.workspace_tools.terminal(handle, "pwd"),
        daemon=True,
    )
    dispatch_thread.start()
    assert entered.wait(5)

    def release():
        ctx.workspaces.release(handle)
        released.set()

    release_thread = threading.Thread(target=release, daemon=True)
    release_thread.start()
    assert not released.wait(0.2)
    finish.set()
    dispatch_thread.join(5)
    release_thread.join(5)
    assert released.is_set()

    successor = _acquire(ctx, "pinned")
    assert successor != handle


def test_dispatch_pin_renews_a_complete_operation_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.terminal_tool as terminal_tool

    home = tmp_path / "home"
    ctx = _context(home)
    handle = _acquire(ctx, "renewed")
    db = home / "plugin-data" / "pr-review" / "workspace-leases.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE workspace_leases SET ttl_seconds=1, expires_at=? WHERE lease_id=?",
            (time.time() + 1, handle["lease_id"]),
        )
    monkeypatch.setattr(
        terminal_tool,
        "terminal_tool",
        lambda *args, **kwargs: json.dumps({"exit_code": 0}),
    )

    ctx.workspace_tools.terminal(handle, "pwd")

    snapshot = ctx.workspaces.inspect(handle)
    assert snapshot["ttlSeconds"] >= terminal_tool.FOREGROUND_MAX_TIMEOUT
    assert (
        snapshot["expiresAt"] - snapshot["heartbeatAt"]
        >= terminal_tool.FOREGROUND_MAX_TIMEOUT
    )


def test_canonical_file_operations_round_trip_in_workspace(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx)
    root = Path(ctx.workspaces.inspect(handle)["path"])
    (root / "unread.txt").write_text("outside\n", encoding="utf-8")

    blocked = json.loads(
        ctx.workspace_tools.write_file(handle, "unread.txt", "overwrite\n")
    )
    assert blocked["stale_write_blocked"] is True
    assert (root / "unread.txt").read_text(encoding="utf-8") == "outside\n"

    written = json.loads(ctx.workspace_tools.write_file(handle, "notes.txt", "alpha\n"))
    assert not written.get("error")
    read = json.loads(ctx.workspace_tools.read_file(handle, "notes.txt"))
    assert "alpha" in read["content"]
    edited = json.loads(
        ctx.workspace_tools.edit_file(handle, "notes.txt", "alpha", "beta")
    )
    assert not edited.get("error")
    assert (root / "notes.txt").read_text(encoding="utf-8") == "beta\n"


def test_terminal_uses_canonical_approval_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.terminal_tool as terminal_tool

    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx)
    calls = []
    real_guard = terminal_tool._run_approval_guards

    def recording_guard(command, env_type, config, *, force):
        calls.append((command, env_type, force))
        return real_guard(command, env_type, config, force=force)

    monkeypatch.setattr(terminal_tool, "_run_approval_guards", recording_guard)
    result = json.loads(ctx.workspace_tools.terminal(handle, "pwd", timeout=30))

    assert result["exit_code"] == 0
    assert calls == [("pwd", "local", False)]


def test_dispatch_host_feature_is_independent(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    assert ctx.has_host_feature("workspace_leases.v1") is True
    assert ctx.has_host_feature("workspace_bound_dispatch.v1") is True
    assert ctx.has_host_feature("workspace_bound_dispatch.future") is False
