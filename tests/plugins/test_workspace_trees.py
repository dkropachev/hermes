"""Tree transfer preserves source bytes without following repository symlinks."""
import base64
import os
from pathlib import Path

import pytest

from hermes_cli.plugin_workspaces import InvalidWorkspaceHandleError
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_constants import hermes_home_key

pytestmark = pytest.mark.linux_only


def context(home):
    home.mkdir(parents=True)
    return PluginContext(PluginManifest(name="tree-contract", source="user"),
                         PluginManager(scope_key=hermes_home_key(home)))


def entry(path, raw=b"content\n", mode="100644"):
    return {"path": path, "mode": mode, "data": base64.b64encode(raw).decode()}


def acquire(ctx):
    return ctx.workspaces.acquire("source", intent=ctx.workspaces.new_intent())


def test_binary_modes_symlinks_and_profile_scope_round_trip(tmp_path):
    a, b = context(tmp_path / "a"), context(tmp_path / "b")
    handle = acquire(a)
    other = acquire(b)
    source = [entry("src/main.py"), entry("bin/run", b"#!/bin/sh\n", "100755"),
              entry("binary", b"\0\xff\x80"), entry("external-link", b"/unrelated/secret", "120000")]
    assert a.has_host_feature("workspace_trees.v1")
    loaded = a.workspace_tools.materialize_tree(handle, source)
    assert loaded["complete"]
    assert loaded["totalBytes"] == sum(len(base64.b64decode(i["data"])) for i in source)
    assert [{k: v for k, v in i.items() if k != "sha256"} for i in loaded["entries"]] == sorted(source, key=lambda i: i["path"])
    with pytest.raises(InvalidWorkspaceHandleError):
        b.workspace_tools.snapshot_tree(handle)
    assert b.workspace_tools.snapshot_tree(other)["entries"] == []
    assert a.workspace_tools.snapshot_tree(handle) == loaded
    with pytest.raises(ValueError, match="empty"):
        a.workspace_tools.materialize_tree(handle, source)
    a.workspaces.release(handle)
    b.workspaces.release(other)


@pytest.mark.parametrize("paths", [["../outside"], ["."], ["/absolute"], ["a//b"],
                                   ["parent", "parent/file"], ["same", "same"],
                                   ["A/first", "a/second"]])
def test_unsafe_or_ambiguous_tree_is_rejected_before_writing(tmp_path, paths):
    ctx = context(tmp_path / "profile")
    handle = acquire(ctx)
    with pytest.raises(ValueError):
        ctx.workspace_tools.materialize_tree(handle, [entry(p) for p in paths])
    assert ctx.workspace_tools.snapshot_tree(handle)["entries"] == []
    ctx.workspaces.release(handle)


def test_snapshot_observes_changes_and_never_follows_symlink_directories(tmp_path):
    ctx = context(tmp_path / "profile")
    handle = acquire(ctx)
    ctx.workspace_tools.materialize_tree(handle, [entry("before"), entry("remove")])
    root = Path(ctx.workspaces.inspect(handle)["path"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("private")
    (root / "linked-directory").symlink_to(outside, target_is_directory=True)
    (root / "before").write_bytes(b"changed")
    (root / "remove").unlink()
    (root / "new").write_bytes(b"\xff")
    captured = ctx.workspace_tools.snapshot_tree(handle)
    files = {i["path"]: i for i in captured["entries"]}
    assert set(files) == {"before", "new", "linked-directory"}
    assert base64.b64decode(files["before"]["data"]) == b"changed"
    assert files["linked-directory"]["mode"] == "120000"
    assert (outside / "secret").read_text() == "private"
    ctx.workspaces.release(handle)


def test_special_files_and_transfer_limits_fail_without_running_workspace_code(tmp_path, monkeypatch):
    import hermes_cli.plugin_workspace_trees as trees
    ctx = context(tmp_path / "profile")
    handle = acquire(ctx)
    root = Path(ctx.workspaces.inspect(handle)["path"])
    os.mkfifo(root / "pipe")
    with pytest.raises(ValueError, match="special file"):
        ctx.workspace_tools.snapshot_tree(handle)
    (root / "pipe").unlink()
    monkeypatch.setattr(trees, "MAX_BYTES", 2)
    with pytest.raises(ValueError, match="limit"):
        ctx.workspace_tools.materialize_tree(handle, [entry("too-large")])
    assert not list(root.iterdir())
    ctx.workspaces.release(handle)
