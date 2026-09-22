"""End-to-end invariants for the plugin-owned Kanban workspace seam.

Regression/MVP for dkropachev/hermes#1: provider acquisition must gate process
spawn, contention must not poison the task retry budget, and a persisted lease
must survive until core can prove the run no longer has a worker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from agent import workspace_registry
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_workspace_provider as kwp
from hermes_cli.plugins import _reset_plugin_managers_for_tests, discover_plugins


_PLUGIN_SOURCE = '''
from agent.workspace_provider import WorkspaceLease, WorkspaceProvider

class FixtureProvider(WorkspaceProvider):
    name = "fixture-workspace"

    def __init__(self):
        self.busy = False
        self.renew_ok = True
        self.path_override = None
        self.calls = []

    def is_available(self):
        return True

    def try_acquire(self, request, **kwargs):
        self.calls.append(("acquire", request.task_id, request.run_id, request.access, request.requested_path))
        if self.busy:
            return None
        return WorkspaceLease(
            lease_id=f"lease-{request.run_id}",
            path=self.path_override or request.requested_path,
            branch_name=request.branch_name,
        )

    def renew(self, request, lease, **kwargs):
        self.calls.append(("renew", request.task_id, request.run_id, lease.lease_id))
        return self.renew_ok

    def release(self, request, lease, *, outcome, **kwargs):
        self.calls.append(("release", request.task_id, request.run_id, lease.lease_id, outcome))

provider = FixtureProvider()

def register(ctx):
    ctx.register_workspace_provider(provider)
'''


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(cwd), "-c", "user.name=Test User",
            "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _install_fixture_plugin(home: Path) -> None:
    plugin = home / "plugins" / "workspace-fixture"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        yaml.safe_dump({
            "name": "workspace-fixture",
            "version": "0.1.0",
            "description": "Workspace provider test fixture",
        }),
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "plugins": {"enabled": ["workspace-fixture"]},
            "kanban": {"workspace_provider": "fixture-workspace"},
        }),
        encoding="utf-8",
    )


def _setup(tmp_path: Path):
    home = Path(__import__("os").environ["HERMES_HOME"])
    _install_fixture_plugin(home)
    workspace_registry._reset_for_tests()
    _reset_plugin_managers_for_tests()
    discover_plugins(force=True)
    provider = workspace_registry.get_provider("fixture-workspace")
    assert provider is not None
    kb.init_db()
    return provider, _repo(tmp_path)


def _teardown() -> None:
    _reset_plugin_managers_for_tests()
    workspace_registry._reset_for_tests()


def test_provider_gates_spawn_persists_access_and_releases_only_after_run_exit(tmp_path, monkeypatch):
    provider, repo = _setup(tmp_path)
    spawned: list[str] = []
    try:
        with kbc.connect() as conn:
            task_id = kb.create_task(
                conn,
                title="inspect safely",
                assignee="default",
                workspace_kind="worktree",
                workspace_path=str(repo),
                workspace_access="read",
            )
            result = kbd.dispatch_once(
                conn,
                spawn_fn=lambda _task, workspace: (spawned.append(workspace), 2_000_000_000)[1],
                max_spawn=1,
            )
            assert [task for task, _assignee, _path in result.spawned] == [task_id]
            run = kb.latest_run(conn, task_id)
            assert run is not None
            assert provider.calls[0][:4] == ("acquire", task_id, run.id, "read")
            assert spawned == [provider.calls[0][4]]
            assert (run.workspace_provider, run.workspace_lease_id, run.workspace_access) == (
                "fixture-workspace", f"lease-{run.id}", "read",
            )
            assert kb.get_task(conn, task_id).workspace_path == str(repo)
            assert kbd.heartbeat_worker(conn, task_id, expected_run_id=run.id) is True
            assert provider.calls[-1][0] == "acquire"
            assert kbd.dispatch_once(conn, spawn_fn=lambda *_args: None, max_spawn=0).workspace_lease_lost == []
            assert provider.calls[-1] == ("renew", task_id, run.id, f"lease-{run.id}")

            # A shared-board tick may arrive while another profile is bound.
            # Persisted provider scope must route A's lease back to A, never to
            # B's different same-named plugin instance.
            from hermes_constants import reset_hermes_home_override, set_hermes_home_override

            home_b = tmp_path / "profile-b"
            home_b.mkdir()
            _install_fixture_plugin(home_b)
            home_token = set_hermes_home_override(home_b)
            try:
                discover_plugins(force=True)
                provider_b = workspace_registry.get_provider("fixture-workspace")
                assert provider_b is not None and provider_b is not provider
                assert kwp.renew_workspace_lease(conn, run.id) is True
                assert provider.calls[-1] == ("renew", task_id, run.id, f"lease-{run.id}")
                assert provider_b.calls == []
            finally:
                reset_hermes_home_override(home_token)

            assert kb.complete_task(conn, task_id, result="reviewed", expected_run_id=run.id)
            assert not [call for call in provider.calls if call[0] == "release"]
            assert Path(run.workspace_lease_path).is_dir()
            assert kb.delete_task(conn, task_id) is False
            assert kwp.release_finished_workspace_leases(conn) == []
            monkeypatch.setattr(kbd, "TERMINAL_WORKER_REAP_GRACE_SECONDS", 0)
            kbd.dispatch_once(conn, spawn_fn=lambda *_args: None, max_spawn=0)
            assert provider.calls[-1] == (
                "release", task_id, run.id, f"lease-{run.id}", "completed",
            )
            assert kb.latest_run(conn, task_id).workspace_lease_released_at is not None

            lost_id = kb.create_task(
                conn,
                title="lose lease",
                assignee="default",
                workspace_kind="worktree",
                workspace_path=str(repo),
            )
            provider.renew_ok = False
            kbd.dispatch_once(
                conn, spawn_fn=lambda *_args: 2_000_000_000, max_spawn=1,
            )
            lost_run = kb.latest_run(conn, lost_id)
            assert lost_run is not None and lost_run.ended_at is None
            reconciled = kbd.dispatch_once(conn, spawn_fn=lambda *_args: None, max_spawn=0)
            assert reconciled.workspace_lease_lost == [lost_run.id]
            assert kb.get_task(conn, lost_id).status == "ready"
            assert kb.latest_run(conn, lost_id).outcome == "workspace_lease_lost"
            assert provider.calls[-1] == (
                "release", lost_id, lost_run.id, f"lease-{lost_run.id}", "workspace_lease_lost",
            )
    finally:
        _teardown()


def test_busy_defers_without_failure_and_spawn_error_releases_acquired_lease(tmp_path):
    provider, repo = _setup(tmp_path)
    spawn_calls: list[str] = []
    try:
        with kbc.connect() as conn:
            busy_id = kb.create_task(
                conn,
                title="wait for writer",
                assignee="default",
                workspace_kind="worktree",
                workspace_path=str(repo),
            )
            provider.busy = True
            deferred = kbd.dispatch_once(
                conn, spawn_fn=lambda _task, workspace: spawn_calls.append(workspace), max_spawn=1,
            )
            assert deferred.workspace_deferred == [busy_id]
            assert spawn_calls == []
            busy_task = kb.get_task(conn, busy_id)
            assert (busy_task.status, busy_task.consecutive_failures) == ("ready", 0)
            assert not (repo / ".worktrees" / busy_id).exists()
            assert kb.archive_task(conn, busy_id)

            invalid_id = kb.create_task(
                conn,
                title="invalid provider checkout",
                assignee="default",
                workspace_kind="worktree",
                workspace_path=str(repo),
            )
            provider.busy = False
            provider.path_override = str(tmp_path / "missing-provider-checkout")
            invalid = kbd.dispatch_once(
                conn, spawn_fn=lambda _task, workspace: spawn_calls.append(workspace), max_spawn=1,
            )
            assert invalid.spawned == [] and spawn_calls == []
            invalid_run = kb.latest_run(conn, invalid_id)
            assert invalid_run is not None and invalid_run.outcome == "spawn_failed"
            assert provider.calls[-1] == (
                "release", invalid_id, invalid_run.id, f"lease-{invalid_run.id}", "acquire_failed",
            )
            assert kb.archive_task(conn, invalid_id)

            failing_id = kb.create_task(
                conn,
                title="spawn fails",
                assignee="default",
                workspace_kind="worktree",
                workspace_path=str(repo),
            )
            provider.path_override = None

            def fail_spawn(_task, _workspace):
                raise RuntimeError("launcher unavailable")

            failed = kbd.dispatch_once(conn, spawn_fn=fail_spawn, max_spawn=1, failure_limit=2)
            assert failed.spawned == []
            failed_task = kb.get_task(conn, failing_id)
            assert (failed_task.status, failed_task.consecutive_failures) == ("ready", 1)
            run = kb.latest_run(conn, failing_id)
            assert run is not None and run.outcome == "spawn_failed"
            assert run.workspace_lease_released_at is not None
            assert provider.calls[-1] == (
                "release", failing_id, run.id, f"lease-{run.id}", "spawn_failed",
            )
    finally:
        _teardown()
