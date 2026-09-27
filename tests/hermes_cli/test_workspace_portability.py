"""Portability boundaries for durable plugin workspace lease state.

Workspace ownership records and leased checkout trees describe one machine/profile identity.  They
must not cross a clone, profile archive, or full backup boundary, while unrelated plugin data must.
The import cases model archives produced before this exclusion existed and prove that live target
state is preserved rather than overwritten.
"""

from argparse import Namespace
from pathlib import Path
import tarfile
import zipfile

import pytest

from hermes_cli.plugin_workspace_registry import is_workspace_runtime_path
from hermes_constants import hermes_home_key
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


_RUNTIME_FILES = (
    ".plugin-workspace-leases/plugin-deadbeef/workspace-leases.db",
    ".plugin-workspace-roots-v1.json",
    ".plugin-workspace-roots.lock",
    "..plugin-workspace-roots-v1.json.creating-deadbeef",
    "plugin-data/.workspace-namespace-deadbeef.json",
    "plugin-data/..workspace-namespace-deadbeef.json.creating-deadbeef",
    "plugin-data/pr-review/workspaces/run-1/checkout.txt",
    "plugin-data/pr-review/workspace-quarantine/run-2/receipt.json",
    "plugin-data/pr-review/workspace-leases.db",
    "plugin-data/pr-review/workspace-leases.db-wal",
    "plugin-data/pr-review/workspace-leases.db-shm",
    "plugin-data/pr-review/workspace-leases.db-journal",
)

_PORTABLE_FILES = (
    "plugin-data/pr-review/settings.json",
    "plugin-data/pr-review/workspace-notes/README.md",
    "plugin-data/pr-review/history/workspace-leases.db.notes",
)


@pytest.fixture()
def profile_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def _seed_plugin_data(root: Path, *, runtime: str = "runtime", portable: str = "portable") -> None:
    for rel in _RUNTIME_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(runtime, encoding="utf-8")
    for rel in _PORTABLE_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(portable, encoding="utf-8")


def _runtime_paths_below(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if is_workspace_runtime_path(path.relative_to(root))
    )


def _assert_only_portable_plugin_data(root: Path, value: str = "portable") -> None:
    assert not _runtime_paths_below(root)
    for rel in _PORTABLE_FILES:
        assert (root / rel).read_text(encoding="utf-8") == value


@pytest.mark.parametrize("source_kind", ["default", "named"])
def test_clone_all_omits_workspace_runtime_for_default_and_named_sources(
    profile_home, source_kind
):
    from hermes_cli.profiles import create_profile

    if source_kind == "default":
        source = profile_home
        clone_from = None
    else:
        source = create_profile("source", no_alias=True)
        clone_from = "source"
    (source / "config.yaml").write_text("model: test\n", encoding="utf-8")
    _seed_plugin_data(source)

    clone = create_profile("clone", clone_from=clone_from, clone_all=True, no_alias=True)

    _assert_only_portable_plugin_data(clone)


def test_named_profile_export_omits_workspace_runtime_and_filtered_extra_files(
    profile_home, tmp_path
):
    from hermes_cli.profiles import create_profile, export_profile

    source = create_profile("source", no_alias=True)
    _seed_plugin_data(source)
    archive = export_profile(
        "source",
        str(tmp_path / "source.tar.gz"),
        extra_files={
            "plugin-data/pr-review/workspaces/injected/file.txt": "runtime",
            "..plugin-workspace-roots-v1.json.creating-injected": "runtime",
            "plugin-data/pr-review/export-note.txt": "portable-extra",
        },
    )

    with tarfile.open(archive, "r:gz") as tf:
        relative_names = {
            Path(*Path(member.name).parts[1:]).as_posix()
            for member in tf.getmembers()
            if len(Path(member.name).parts) > 1
        }

    assert not [name for name in relative_names if is_workspace_runtime_path(name)]
    assert set(_PORTABLE_FILES) <= relative_names
    assert "plugin-data/pr-review/export-note.txt" in relative_names


def test_named_profile_import_sanitizes_workspace_runtime_from_old_archive(
    profile_home, tmp_path
):
    from hermes_cli.profiles import import_profile

    old_root = tmp_path / "old-source"
    old_root.mkdir()
    (old_root / "config.yaml").write_text("model: test\n", encoding="utf-8")
    _seed_plugin_data(old_root)
    archive = tmp_path / "old-source.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(old_root, arcname="old-source")

    restored = import_profile(str(archive), name="restored")

    _assert_only_portable_plugin_data(restored)

    # A later import never sanitizes the live profile in place.  Existing targets are refused
    # before extraction, so their active registry, markers, leases, and checkouts remain intact.
    _seed_plugin_data(restored, runtime="live-runtime")
    with pytest.raises(FileExistsError, match="already exists"):
        import_profile(str(archive), name="restored")
    for rel in _RUNTIME_FILES:
        assert (restored / rel).read_text(encoding="utf-8") == "live-runtime"


def test_full_backup_omits_workspace_runtime_at_root_and_named_profile(
    profile_home, tmp_path, monkeypatch
):
    from hermes_cli import backup as backup_mod

    (profile_home / "config.yaml").write_text("model: test\n", encoding="utf-8")
    _seed_plugin_data(profile_home, portable="root-portable")
    named = profile_home / "profiles" / "coder"
    named.mkdir(parents=True)
    _seed_plugin_data(named, portable="named-portable")
    monkeypatch.setattr(backup_mod, "_collect_memory_provider_external_paths", lambda: [])

    archive = tmp_path / "backup.zip"
    assert backup_mod.run_backup(Namespace(output=str(archive)))
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())

    assert not [name for name in names if is_workspace_runtime_path(name)]
    assert set(_PORTABLE_FILES) <= names
    assert {f"profiles/coder/{rel}" for rel in _PORTABLE_FILES} <= names


def test_full_import_preserves_live_workspace_runtime_and_skips_old_archive_state(
    profile_home, tmp_path, monkeypatch, capsys
):
    from hermes_cli import backup as backup_mod
    import hermes_cli.gateway as gateway_mod

    # Seed every other runtime path. Import must preserve these and must not create the rest.
    for index, rel in enumerate(_RUNTIME_FILES):
        if index % 2:
            continue
        path = profile_home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("live-root", encoding="utf-8")
        named_path = profile_home / "profiles" / "coder" / rel
        named_path.parent.mkdir(parents=True, exist_ok=True)
        named_path.write_text("live-named", encoding="utf-8")

    files = {"config.yaml": "model: restored\n"}
    for rel in _RUNTIME_FILES:
        files[rel] = "foreign-root"
        files[f"profiles/coder/{rel}"] = "foreign-named"
    for rel in _PORTABLE_FILES:
        files[rel] = "restored-root"
        files[f"profiles/coder/{rel}"] = "restored-named"
    archive = tmp_path / "old-backup.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for rel, content in files.items():
            zf.writestr(rel, content)

    monkeypatch.setattr(gateway_mod, "_is_service_running", lambda: True)
    backup_mod.run_import(Namespace(zipfile=str(archive), force=True))

    for index, rel in enumerate(_RUNTIME_FILES):
        root_path = profile_home / rel
        named_path = profile_home / "profiles" / "coder" / rel
        if index % 2:
            assert not root_path.exists(), rel
            assert not named_path.exists(), rel
        else:
            assert root_path.read_text(encoding="utf-8") == "live-root"
            assert named_path.read_text(encoding="utf-8") == "live-named"
    for rel in _PORTABLE_FILES:
        assert (profile_home / rel).read_text(encoding="utf-8") == "restored-root"
        assert (profile_home / "profiles" / "coder" / rel).read_text(
            encoding="utf-8"
        ) == "restored-named"
    assert "Preserved 24 runtime state file(s)" in capsys.readouterr().out


def test_quick_snapshot_candidate_invariant_omits_workspace_runtime(
    profile_home, monkeypatch
):
    from hermes_cli import backup as backup_mod

    (profile_home / "config.yaml").write_text("model: test\n", encoding="utf-8")
    _seed_plugin_data(profile_home)

    actual = {rel for _src, rel, _in_dir in backup_mod._quick_snapshot_candidates(profile_home)}
    assert not [rel for rel in actual if is_workspace_runtime_path(rel)]
    assert not [rel for rel in actual if rel.startswith("plugin-data/")]

    # Guard the invariant if the quick set later grows to include plugin data: ordinary state may
    # be selected, but lease ownership state and checkout trees still may not be.
    monkeypatch.setattr(
        backup_mod,
        "_QUICK_STATE_FILES",
        ("plugin-data", ".plugin-workspace-leases", ".plugin-workspace-roots-v1.json"),
    )
    expanded = {
        rel for _src, rel, _in_dir in backup_mod._quick_snapshot_candidates(profile_home)
    }
    assert not [rel for rel in expanded if is_workspace_runtime_path(rel)]
    assert set(_PORTABLE_FILES) <= expanded


def test_supported_profile_rename_preserves_workspace_lease(
    profile_home, monkeypatch,
) -> None:
    from hermes_cli import profiles

    old_home = profiles.create_profile("lease-old", no_alias=True)
    ctx = PluginContext(
        PluginManifest(name="pr-review", key="pr-review"),
        PluginManager(scope_key=hermes_home_key(old_home)),
    )
    intent = ctx.workspaces.new_intent()
    handle = ctx.workspaces.acquire("rename-run", intent=intent)
    workspace = Path(ctx.workspaces.inspect(handle)["path"])
    (workspace / "preserve.txt").write_text("unique", encoding="utf-8")
    monkeypatch.setattr(profiles, "_check_gateway_running", lambda _home: False)
    monkeypatch.setattr(profiles, "_live_default_multiplexer", lambda: None)
    monkeypatch.setattr(profiles, "remove_wrapper_script", lambda _name: None)
    monkeypatch.setattr(profiles, "check_alias_collision", lambda _name: "disabled in test")
    monkeypatch.setattr(
        "hermes_cli.profile_identity._migrate_profile_identity", lambda *_args: None,
    )

    new_home = profiles.rename_profile("lease-old", "lease-new")
    renamed = PluginContext(
        PluginManifest(name="pr-review", key="pr-review"),
        PluginManager(scope_key=hermes_home_key(new_home)),
    )
    snapshot = renamed.workspaces.inspect(handle)
    assert snapshot["path"] == str(new_home / "plugin-data/pr-review/workspaces/rename-run")
    assert Path(snapshot["path"], "preserve.txt").read_text(encoding="utf-8") == "unique"
    assert renamed.workspaces.acquire("rename-run", intent=intent) == handle
    assert renamed.workspaces.renew(handle)["leaseId"] == handle["lease_id"]
