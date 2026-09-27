"""Native Windows security contracts for durable plugin workspace leases."""

from __future__ import annotations

import multiprocessing
import os
import stat
import subprocess
from pathlib import Path

import pytest

from hermes_constants import hermes_home_key
from hermes_cli.plugin_workspace_errors import WorkspacePathError
from hermes_cli.plugin_workspace_fs import HeldDirectory, HeldRegularFile
from hermes_cli.plugin_workspace_registry import LOCK_NAME, REGISTRY_NAME
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


pytestmark = pytest.mark.windows_only


def _context(home: Path) -> PluginContext:
    home.mkdir(parents=True, exist_ok=True)
    return PluginContext(
        PluginManifest(name="pr-review", key="pr-review"),
        PluginManager(scope_key=hermes_home_key(home)),
    )


def _acquire(ctx: PluginContext, workspace_id: str) -> dict:
    return ctx.workspaces.acquire(workspace_id, intent=ctx.workspaces.new_intent())


def _make_junction(link: Path, target: Path) -> None:
    # Binary capture is deliberate: localized cmd.exe output may not decode as UTF-8.
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(detail or f"mklink /J failed: {result.returncode}")


def _security_descriptor(path: Path):
    from hermes_cli.windows_ssh_runtime import _win32

    w = _win32()
    information = (
        w.win32security.OWNER_SECURITY_INFORMATION
        | w.win32security.DACL_SECURITY_INFORMATION
    )
    return w.win32security.GetFileSecurity(str(path), information), information


def _security_sddl(path: Path) -> str:
    from hermes_cli.windows_ssh_runtime import _win32

    descriptor, information = _security_descriptor(path)
    return _win32().win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        descriptor, 1, information,
    )


def _assert_owner_system_dacl(path: Path, *, directory: bool) -> None:
    from hermes_cli.windows_ssh_runtime import _allowed_sids, _current_sid, _sid_str, _win32

    w = _win32()
    descriptor, _information = _security_descriptor(path)
    allowed = _allowed_sids()
    assert _sid_str(descriptor.GetSecurityDescriptorOwner()) == _sid_str(_current_sid())
    assert descriptor.GetSecurityDescriptorControl()[0] & w.win32security.SE_DACL_PROTECTED
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None
    allow_types = {
        w.win32security.ACCESS_ALLOWED_ACE_TYPE,
        w.win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
        getattr(w.win32security, "ACCESS_ALLOWED_CALLBACK_ACE_TYPE", 9),
        getattr(w.win32security, "ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE", 11),
    }
    grants = {sid: 0 for sid in allowed}
    inherit = w.win32con.OBJECT_INHERIT_ACE | w.win32con.CONTAINER_INHERIT_ACE
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        ace_type, flags = ace[0]
        if ace_type not in allow_types or not ace[1]:
            continue
        sid = _sid_str(ace[-1])
        assert sid in allowed
        grants[sid] |= int(ace[1])
        if directory:
            assert flags & inherit == inherit
    assert {sid for sid, mask in grants.items() if mask} == allowed
    assert all(
        mask & w.ntsecuritycon.FILE_ALL_ACCESS == w.ntsecuritycon.FILE_ALL_ACCESS
        for mask in grants.values()
    )


def _add_everyone_read_ace(path: Path) -> None:
    from hermes_cli.windows_ssh_runtime import _win32

    w = _win32()
    descriptor, _information = _security_descriptor(path)
    dacl = descriptor.GetSecurityDescriptorDacl()
    everyone = w.win32security.ConvertStringSidToSid("S-1-1-0")
    dacl.AddAccessAllowedAceEx(
        w.win32security.ACL_REVISION, 0, w.ntsecuritycon.FILE_GENERIC_READ, everyone,
    )
    security_information = (
        w.win32security.DACL_SECURITY_INFORMATION
        | getattr(w.win32security, "PROTECTED_DACL_SECURITY_INFORMATION", 0x80000000)
    )
    w.win32security.SetNamedSecurityInfo(
        str(path), w.win32security.SE_FILE_OBJECT, security_information,
        None, None, dacl, None,
    )


def _lock_probe(path: str, output) -> None:
    """Try the production byte range without waiting; runs in a spawned Windows process."""
    from hermes_cli.windows_ssh_runtime import _win32

    w = _win32()
    handle = w.win32file.CreateFile(
        path, w.win32con.GENERIC_READ | w.win32con.GENERIC_WRITE,
        w.win32con.FILE_SHARE_READ | w.win32con.FILE_SHARE_WRITE,
        None, w.win32con.OPEN_EXISTING,
        w.win32con.FILE_ATTRIBUTE_NORMAL | 0x00200000, None,
    )
    overlapped = w.pywintypes.OVERLAPPED()
    try:
        try:
            w.win32file.LockFileEx(handle, 0x2 | 0x1, 0, 1, 0, overlapped)
        except w.pywintypes.error as exc:
            output.put(("blocked", int(getattr(exc, "winerror", exc.args[0]))))
        else:
            w.win32file.UnlockFileEx(handle, 0, 1, 0, overlapped)
            output.put(("acquired", 0))
    finally:
        w.win32file.CloseHandle(handle)


def _run_lock_probe(path: Path) -> tuple[str, int]:
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    process = context.Process(target=_lock_probe, args=(str(path), output))
    process.start()
    try:
        result = output.get(timeout=15)
        process.join(timeout=15)
        assert process.exitcode == 0
        return result
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        output.close()
        output.join_thread()


def test_workspace_registry_objects_use_protected_owner_system_dacls(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # Existing host-owned roots exercise authenticated hardening, not only secure creation.
    (home / "plugin-data").mkdir()
    (home / REGISTRY_NAME).mkdir()
    home_security = _security_sddl(home)
    ctx = _context(home)
    handle = _acquire(ctx, "dacl-run")
    layout = ctx.workspaces._layout()

    directories = (
        layout.plugin_data_dir,
        layout.registry_root,
        layout.data_dir,
        layout.registry_dir,
        layout.workspaces_dir,
        layout.quarantine_dir,
        Path(ctx.workspaces.inspect(handle)["path"]),
    )
    files = (
        layout.outer_marker_path,
        home / LOCK_NAME,
        layout.inner_marker_path,
        layout.registry_binding_path,
        layout.db_path,
    )
    for path in directories:
        _assert_owner_system_dacl(path, directory=True)
    for path in files:
        _assert_owner_system_dacl(path, directory=False)
    from hermes_cli import plugin_workspaces

    conn = plugin_workspaces._connect(layout)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE workspace_leases SET updated_at=updated_at WHERE lease_id=?",
            (handle["lease_id"],),
        )
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(f"{layout.db_path}{suffix}")
            if candidate.exists():
                _assert_owner_system_dacl(candidate, directory=False)
        conn.rollback()
    finally:
        conn.close()
    # HERMES_HOME is only the anchor; workspace initialization must not rewrite its ACL.
    assert _security_sddl(home) == home_security


def test_marker_temporary_files_are_private_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspace_registry

    home = tmp_path / "home"
    ctx = _context(home)
    layout = ctx.workspaces._layout()
    original = plugin_workspace_registry.publish_private_file
    observed: set[str] = set()

    def inspect_before_publish(parent, source, target):
        _assert_owner_system_dacl(parent.path / source, directory=False)
        observed.add(target)
        return original(parent, source, target)

    monkeypatch.setattr(
        plugin_workspace_registry, "publish_private_file", inspect_before_publish,
    )
    _acquire(ctx, "marker-temporary")
    assert observed == {
        layout.outer_marker_path.name,
        layout.registry_binding_path.name,
        layout.inner_marker_path.name,
    }


def test_read_only_marker_verification_rejects_without_repairing_dacl(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "marker-run")
    marker = ctx.workspaces._layout().outer_marker_path
    _add_everyone_read_ace(marker)
    poisoned = _security_sddl(marker)

    with pytest.raises(WorkspacePathError, match="DACL grants another principal"):
        ctx.workspaces.inspect(handle)

    assert _security_sddl(marker) == poisoned


@pytest.mark.parametrize(
    "marker_attribute",
    ["outer_marker_path", "registry_binding_path", "inner_marker_path"],
)
def test_read_only_marker_verification_does_not_request_write_access(
    tmp_path: Path, marker_attribute: str,
) -> None:
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "read-only-marker")
    marker = Path(getattr(ctx.workspaces._layout(), marker_attribute))
    os.chmod(marker, stat.S_IREAD)
    try:
        assert marker.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
        assert ctx.workspaces.inspect(handle)["workspaceId"] == "read-only-marker"
    finally:
        os.chmod(marker, stat.S_IWRITE)


def test_bootstrap_lock_contends_across_native_windows_processes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = _context(home)
    _acquire(ctx, "lock-run")
    lock_path = home / LOCK_NAME

    with HeldDirectory(home) as held_home:
        with HeldRegularFile(held_home, LOCK_NAME, create=False, writable=True) as lock:
            lock.lock_exclusive()
            assert _run_lock_probe(lock_path) == ("blocked", 33)
            lock.unlock()
            assert _run_lock_probe(lock_path) == ("acquired", 0)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_hardlinked_database_files_are_rejected(tmp_path: Path, suffix: str) -> None:
    home = tmp_path / "home"
    ctx = _context(home)
    handle = _acquire(ctx, "safe-run")
    layout = ctx.workspaces._layout()
    candidate = Path(f"{layout.db_path}{suffix}")
    source = tmp_path / f"external{suffix or '-main'}.db"
    if suffix:
        source.write_bytes(b"external sentinel")
        candidate.unlink(missing_ok=True)
    else:
        os.link(candidate, source)
    if suffix:
        os.link(source, candidate)
    before = source.read_bytes()
    try:
        with pytest.raises(WorkspacePathError, match="database file is unsafe"):
            ctx.workspaces.inspect(handle)
        assert source.read_bytes() == before
        assert not Path(f"{source}-wal").exists()
        assert not Path(f"{source}-shm").exists()
    finally:
        if suffix:
            candidate.unlink(missing_ok=True)
            source.unlink(missing_ok=True)
        else:
            source.unlink(missing_ok=True)


def test_replacing_leased_workspace_with_junction_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = _context(home)
    handle = _acquire(ctx, "junction-run")
    workspace = Path(ctx.workspaces.inspect(handle)["path"])
    backup = workspace.with_name("junction-run-original")
    os.rename(workspace, backup)

    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "sentinel.txt").write_text("outside", encoding="utf-8")
    try:
        _make_junction(workspace, target)
    except OSError as exc:
        os.rename(backup, workspace)
        pytest.skip(f"junction creation is unavailable: {exc}")
    try:
        for operation in (
            lambda: ctx.workspaces.inspect(handle),
            lambda: ctx.workspaces.renew(handle),
            lambda: ctx.workspaces.release(handle),
        ):
            with pytest.raises(WorkspacePathError, match="reparse point|regular directory"):
                operation()
        assert (target / "sentinel.txt").read_text(encoding="utf-8") == "outside"
    finally:
        workspace.rmdir()
        os.rename(backup, workspace)


def test_preexisting_workspace_junction_is_not_quarantined_or_traversed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = _context(home)
    bootstrap = _acquire(ctx, "bootstrap")
    ctx.workspaces.release(bootstrap)
    layout = ctx.workspaces._layout()
    target = tmp_path / "external-preexisting"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("outside", encoding="utf-8")
    junction = layout.workspaces_dir / "poisoned-run"
    intent = ctx.workspaces.new_intent()
    try:
        _make_junction(junction, target)
    except OSError as exc:
        pytest.skip(f"junction creation is unavailable: {exc}")
    try:
        with pytest.raises(WorkspacePathError, match="reparse point"):
            ctx.workspaces.acquire("poisoned-run", intent=intent)
        assert junction.exists()
        assert sentinel.read_text(encoding="utf-8") == "outside"
    finally:
        junction.rmdir()
    recovered = ctx.workspaces.acquire("poisoned-run", intent=intent)
    ctx.workspaces.release(recovered)
