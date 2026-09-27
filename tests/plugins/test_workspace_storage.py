"""Storage-registry and database security contracts for plugin workspace leases."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_constants import hermes_home_key
from hermes_cli.plugin_workspaces import WorkspacePathError
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _context(home: Path) -> PluginContext:
    home.mkdir(parents=True, exist_ok=True)
    return PluginContext(
        PluginManifest(name="pr-review", key="pr-review"),
        PluginManager(scope_key=hermes_home_key(home)),
    )


def _acquire(
    ctx: PluginContext, workspace_id: str, *, intent: dict | None = None,
) -> dict:
    return ctx.workspaces.acquire(
        workspace_id, intent=intent or ctx.workspaces.new_intent(),
    )


def _reconnect(ctx: PluginContext, handle: dict) -> dict:
    return ctx.workspaces.reconnect(handle, intent=ctx.workspaces.new_intent())


def _child_env(home: Path) -> dict[str, str]:
    env = {**os.environ, "HERMES_HOME": str(home)}
    env.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)
    return env


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
    quarantine.mkdir(exist_ok=True)
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


def _seed_bound_original_h1_database(
    home: Path, *, workspace_id: str = "legacy-run",
    plugin_namespace: str = "pr-review", profile_key: str | None = None,
    workspace_path: str | None = None,
) -> tuple[PluginContext, dict, Path]:
    """Install the old schema only after publishing the new storage bindings."""
    from hermes_cli import plugin_workspaces
    from hermes_cli.plugin_workspace_registry import _publish_marker, open_storage_anchors

    ctx = _context(home)
    bootstrap = _acquire(ctx, "schema-bootstrap")
    ctx.workspaces.release(bootstrap)
    layout = ctx.workspaces._layout()

    with open_storage_anchors(layout, include_roots=True, publish=False) as anchors:
        layout.registry_binding_path.unlink()
        layout.inner_marker_path.unlink()
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{layout.db_path}{suffix}").unlink(missing_ok=True)
        handle, workspace = _seed_original_h1_database(
            home, workspace_id=workspace_id, plugin_namespace=plugin_namespace,
            profile_key=profile_key, workspace_path=workspace_path,
        )
        legacy_db = home / "plugin-data/pr-review/workspace-leases.db"
        os.replace(legacy_db, layout.db_path)
        with plugin_workspaces._HeldRegularFile(
            anchors.registry_namespace, "workspace-leases.db", create=False,
        ) as held_db:
            binding = {
                **(anchors.binding_payload or {}),
                "database_identity": [int(part) for part in held_db.identity],
            }
        _publish_marker(
            anchors.registry, layout.registry_binding_path.name, binding,
        )
        _publish_marker(anchors.plugin_data, layout.inner_marker_path.name, binding)
    return ctx, handle, workspace


def _swap_root_to_symlink(root: Path, external: Path) -> Path:
    backup = root.with_name(f"{root.name}-held")
    os.rename(root, backup)
    root.symlink_to(external, target_is_directory=True)
    return backup


def _restore_swapped_root(root: Path, backup: Path) -> None:
    root.unlink()
    os.rename(backup, root)


def _swap_root_to_empty_directory(root: Path) -> Path:
    backup = root.with_name(f"{root.name}-held")
    os.rename(root, backup)
    root.mkdir(mode=0o700)
    return backup


def _restore_swapped_directory(root: Path, backup: Path) -> None:
    rejected = root.with_name(f"{root.name}-rejected")
    os.rename(root, rejected)
    os.rename(backup, root)


@pytest.mark.parametrize("root_attr", ["data_dir", "plugin_data_dir"])
def test_live_lease_rejects_recreated_storage_root_and_recovers_after_restore(
    tmp_path: Path, root_attr: str,
) -> None:
    home = tmp_path / root_attr
    ctx = _context(home)
    intent = ctx.workspaces.new_intent()
    handle = _acquire(ctx, "root-swap-live", intent=intent)
    workspace = Path(ctx.workspaces.inspect(handle)["path"])
    (workspace / "preserve.txt").write_text("original", encoding="utf-8")
    layout = ctx.workspaces._layout()
    root = getattr(layout, root_attr)
    preserved_relative = workspace.relative_to(root)
    backup = _swap_root_to_empty_directory(root)

    try:
        with pytest.raises(WorkspacePathError, match="identity|marker|binding|replaced"):
            _acquire(ctx, "root-swap-live", intent=intent)
        assert (backup / preserved_relative / "preserve.txt").read_text(
            encoding="utf-8",
        ) == "original"
    finally:
        _restore_swapped_directory(root, backup)

    restored = ctx.workspaces.inspect(handle)
    assert Path(restored["path"], "preserve.txt").read_text(encoding="utf-8") == "original"
    assert _acquire(ctx, "root-swap-live", intent=intent) == handle


@pytest.mark.parametrize(
    "marker_attr", ["outer_marker_path", "registry_binding_path", "inner_marker_path"],
)
@pytest.mark.parametrize("corruption", ["symlink", "malformed"])
def test_storage_root_markers_reject_symlinks_and_malformed_json(
    tmp_path: Path, marker_attr: str, corruption: str,
) -> None:
    ctx = _context(tmp_path / f"{marker_attr}-{corruption}")
    handle = _acquire(ctx, "marker-corruption")
    marker = getattr(ctx.workspaces._layout(), marker_attr)
    external = tmp_path / f"{marker_attr}-{corruption}-external.json"
    external.write_text('{"sentinel":"untouched"}\n', encoding="utf-8")
    before = external.read_bytes()
    marker.unlink()
    if corruption == "symlink":
        try:
            marker.symlink_to(external)
        except OSError as exc:  # pragma: no cover - platform privilege policy
            pytest.skip(f"symlink creation is unavailable: {exc}")
    else:
        marker.write_text("{not-json", encoding="utf-8")

    with pytest.raises(WorkspacePathError, match="marker|JSON|regular file|symlink"):
        ctx.workspaces.inspect(handle)
    assert external.read_bytes() == before


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "marker_attr", ["outer_marker_path", "registry_binding_path", "inner_marker_path"],
)
def test_storage_root_markers_reject_wrong_posix_mode(
    tmp_path: Path, marker_attr: str,
) -> None:
    ctx = _context(tmp_path / marker_attr)
    handle = _acquire(ctx, "marker-mode")
    marker = getattr(ctx.workspaces._layout(), marker_attr)
    marker.chmod(0o644)

    with pytest.raises(WorkspacePathError, match="marker|mode|permissions"):
        ctx.workspaces.inspect(handle)


def test_missing_public_marker_repairs_only_from_private_binding(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx = _context(home)
    handle = _acquire(ctx, "marker-repair")
    layout = ctx.workspaces._layout()
    layout.inner_marker_path.unlink()

    assert ctx.workspaces.inspect(handle)["workspaceId"] == "marker-repair"
    assert layout.inner_marker_path.is_file()

    layout.inner_marker_path.unlink()
    backup = _swap_root_to_empty_directory(layout.data_dir)
    try:
        with pytest.raises(WorkspacePathError, match="binding|identit"):
            ctx.workspaces.inspect(handle)
        assert (backup / "workspaces/marker-repair").is_dir()
    finally:
        _restore_swapped_directory(layout.data_dir, backup)
    assert ctx.workspaces.inspect(handle)["workspaceId"] == "marker-repair"


def test_missing_private_binding_never_reblesses_existing_namespace(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "binding-loss")
    layout = ctx.workspaces._layout()
    layout.registry_binding_path.unlink()

    with pytest.raises(WorkspacePathError, match="binding|unbound|recovery"):
        ctx.workspaces.inspect(handle)
    assert Path(layout.workspaces_dir, "binding-loss").is_dir()
    assert layout.db_path.is_file()


def test_deleted_binding_cannot_reset_live_database_and_roots(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    handle = _acquire(ctx, "binding-reset")
    workspace = Path(ctx.workspaces.inspect(handle)["path"])
    (workspace / "unique.txt").write_text("preserve", encoding="utf-8")
    layout = ctx.workspaces._layout()
    layout.registry_binding_path.unlink()
    db_backup = layout.db_path.with_name("workspace-leases.preserved")
    layout.db_path.rename(db_backup)
    workspaces_backup = _swap_root_to_empty_directory(layout.workspaces_dir)
    quarantine_backup = _swap_root_to_empty_directory(layout.quarantine_dir)

    try:
        with pytest.raises(WorkspacePathError, match="binding|marker|recovery"):
            _acquire(ctx, "new-generation")
        assert (workspaces_backup / "binding-reset/unique.txt").read_text(
            encoding="utf-8",
        ) == "preserve"
        assert db_backup.is_file()
        assert not layout.db_path.exists()
    finally:
        _restore_swapped_directory(layout.workspaces_dir, workspaces_backup)
        _restore_swapped_directory(layout.quarantine_dir, quarantine_backup)


def test_crash_before_final_binding_resumes_only_empty_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugin_workspace_registry

    ctx = _context(tmp_path / "home")
    original = plugin_workspace_registry.StorageAnchors.commit_binding

    def crash_before_binding(_self, _database_identity):
        raise RuntimeError("crash before final binding")

    monkeypatch.setattr(
        plugin_workspace_registry.StorageAnchors, "commit_binding", crash_before_binding,
    )
    with pytest.raises(RuntimeError, match="crash before final binding"):
        _acquire(ctx, "bootstrap-retry")
    monkeypatch.setattr(
        plugin_workspace_registry.StorageAnchors, "commit_binding", original,
    )
    handle = _acquire(ctx, "bootstrap-retry")
    assert ctx.workspaces.inspect(handle)["workspaceId"] == "bootstrap-retry"


def test_concurrent_processes_create_first_markers_and_acquire(tmp_path: Path) -> None:
    home = tmp_path / "concurrent-first-marker"
    script = r"""
import json
import sys
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

print("READY", flush=True)
sys.stdin.readline()
ctx = PluginContext(PluginManifest(name="pr-review"), PluginManager())
handle = ctx.workspaces.acquire(sys.argv[1], intent=ctx.workspaces.new_intent())
print(json.dumps(handle, sort_keys=True), flush=True)
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script, f"marker-race-{index}"],
            cwd=PROJECT_ROOT, env=_child_env(home), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for index in range(4)
    ]
    try:
        for child in children:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "READY"
        for child in children:
            assert child.stdin is not None
            child.stdin.write("go\n")
            child.stdin.flush()

        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, stderr
            results.append(json.loads(stdout.strip().splitlines()[-1]))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()

    ctx = _context(home)
    assert {
        ctx.workspaces.inspect(handle)["workspaceId"] for handle in results
    } == {f"marker-race-{index}" for index in range(4)}


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_legacy_database_hardlinked_file_is_refused_without_migration_or_target_write(
    tmp_path: Path, suffix: str,
) -> None:
    from hermes_cli.plugin_workspace_registry import REGISTRY_NAME

    home = tmp_path / (suffix.removeprefix("-") or "main")
    poisoned_parent = home / "plugin-data/pr-review"
    poisoned_parent.mkdir(parents=True, mode=0o700)
    legacy_db = home / "plugin-data/pr-review/workspace-leases.db"
    poisoned = Path(f"{legacy_db}{suffix}")
    external = tmp_path / f"external{suffix or '-main'}"
    external.write_bytes(b"external database auxiliary sentinel\n")
    before = external.read_bytes()
    try:
        os.link(external, poisoned)
    except OSError as exc:  # pragma: no cover - filesystem capability policy
        pytest.skip(f"hard links are unavailable: {exc}")
    ctx = _context(home)

    with pytest.raises(WorkspacePathError, match="database|auxiliary|sidecar|hard link|unsafe"):
        _acquire(ctx, "legacy-poison")
    assert external.read_bytes() == before
    assert {path.name for path in poisoned_parent.iterdir()} == {poisoned.name}
    assert not list((home / REGISTRY_NAME).glob("plugin-*/workspace-leases.db"))


def test_fresh_registry_never_creates_database_auxiliaries_in_public_data_dir(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path / "home")
    _acquire(ctx, "private-registry")
    layout = ctx.workspaces._layout()

    assert layout.db_path.parent != layout.data_dir
    assert not any(
        (layout.data_dir / f"workspace-leases.db{suffix}").exists()
        for suffix in ("", "-wal", "-shm", "-journal")
    )


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
        paths = (
            layout.db_path, Path(str(layout.db_path) + "-wal"),
            Path(str(layout.db_path) + "-shm"),
        )
        assert paths[0].exists()
        for path in paths:
            if not path.exists():
                continue  # vulnerable system SQLite intentionally uses DELETE journaling
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        conn.rollback()
    finally:
        if conn is not None:
            conn.close()
        os.umask(previous_umask)


@pytest.mark.linux_only
def test_database_connection_remains_anchored_after_parent_swap(tmp_path: Path) -> None:
    from hermes_cli import plugin_workspaces

    home, external = tmp_path / "home", tmp_path / "outside"
    external.mkdir()
    (external / "sentinel").write_text("outside", encoding="utf-8")
    ctx = _context(home)
    layout = ctx.workspaces._layout()
    conn = plugin_workspaces._connect(layout)
    backup = _swap_root_to_symlink(layout.data_dir, external)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO workspace_lease_events
               VALUES (NULL, 'anchored', 'lease', 1, 'test', 0, 1, NULL, '{}')"""
        )
        conn.commit()
        assert (external / "sentinel").read_text(encoding="utf-8") == "outside"
        assert not (external / "workspace-leases.db").exists()
        assert not (external / "workspace-leases.db-wal").exists()
        assert not (external / "workspace-leases.db-shm").exists()
    finally:
        _restore_swapped_root(layout.data_dir, backup)
        conn.close()

    with sqlite3.connect(layout.db_path) as verify:
        assert verify.execute(
            "SELECT COUNT(*) FROM workspace_lease_events WHERE workspace_id='anchored'"
        ).fetchone()[0] == 1


@pytest.mark.linux_only
@pytest.mark.parametrize("replacement", ["symlink", "regular"])
def test_database_leaf_swap_during_open_never_touches_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str,
) -> None:
    from hermes_cli import plugin_workspaces

    home = tmp_path / "home"
    ctx = _context(home)
    layout = ctx.workspaces._layout()
    external = tmp_path / "external.db"
    with sqlite3.connect(external) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT)")
        conn.execute("INSERT INTO sentinel VALUES ('untouched')")
    before = external.read_bytes()
    original = plugin_workspaces._HeldRegularFile.open_path
    backup = layout.db_path.with_name("workspace-leases.original")

    def swap_before_sqlite_open(held_file):
        anchored_path = original(held_file)
        os.rename(layout.db_path, backup)
        if replacement == "symlink":
            layout.db_path.symlink_to(external)
        else:
            os.link(external, layout.db_path)
        return anchored_path

    monkeypatch.setattr(
        plugin_workspaces._HeldRegularFile, "open_path", swap_before_sqlite_open,
    )
    with pytest.raises(WorkspacePathError, match="leaf (?:identity changed|was replaced)"):
        plugin_workspaces._connect(layout)
    assert external.read_bytes() == before
    assert not Path(str(external) + "-wal").exists()
    assert not Path(str(external) + "-shm").exists()
    with sqlite3.connect(external) as conn:
        assert conn.execute("SELECT value FROM sentinel").fetchone()[0] == "untouched"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='workspace_leases'"
        ).fetchone()[0] == 0


def test_stdlib_sqlite_integrity_after_workspace_stress(tmp_path: Path) -> None:
    ctx = _context(tmp_path / "home")
    for index in range(20):
        handle = _acquire(ctx, f"stress-{index}")
        if index % 2:
            Path(ctx.workspaces.inspect(handle)["path"], "data.txt").write_text(
                str(index), encoding="utf-8",
            )
        ctx.workspaces.release(handle)
    with sqlite3.connect(ctx.workspaces._layout().db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_original_h1_database_upgrades_without_stranding_handle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ctx, handle, workspace = _seed_bound_original_h1_database(home)
    (workspace / "preserve.txt").write_text("legacy", encoding="utf-8")

    inspected = ctx.workspaces.inspect(handle)
    assert inspected["workspaceId"] == "legacy-run"
    assert Path(inspected["path"], "preserve.txt").read_text(encoding="utf-8") == "legacy"
    successor = _reconnect(ctx, handle)
    assert ctx.workspaces.inspect(successor)["generation"] == 2
    released = ctx.workspaces.release(successor)
    assert released["state"] == "released"
    assert released["cleanup"]["disposition"] == "quarantined"

    db = ctx.workspaces._layout().db_path
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(workspace_leases)")}
        identity, heartbeat_mono, intent_id, root_identities = conn.execute(
            """SELECT plugin_identity, heartbeat_monotonic, acquire_intent_id,
                      root_identities_json
               FROM workspace_leases WHERE workspace_id='legacy-run'"""
        ).fetchone()
    assert {
        "plugin_identity", "acquire_intent_id", "heartbeat_monotonic",
        "expires_monotonic", "expiry_observer", "expiry_observed_monotonic",
        "root_identities_json",
    } <= columns
    assert identity == ctx.workspaces._layout().plugin_identity
    assert heartbeat_mono is not None
    assert intent_id is None
    assert json.loads(root_identities)["version"] == 2


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
    ctx, handle, _workspace = _seed_bound_original_h1_database(home, **kwargs)

    with pytest.raises(WorkspacePathError, match="refusing to claim"):
        ctx.workspaces.inspect(handle)
    db = ctx.workspaces._layout().db_path
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(workspace_leases)")}
        if "plugin_identity" in columns:
            identity = conn.execute(
                "SELECT plugin_identity FROM workspace_leases",
            ).fetchone()[0]
            assert identity is None
