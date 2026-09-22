"""Plugin workspace-provider lifecycle for Kanban worktree runs.

The dispatcher owns ordering: claim, resolve the built-in worktree, acquire a
provider lease, then spawn. Provider calls never run under a Kanban SQLite
transaction. Lease identity is persisted on ``task_runs`` so renew/release do
not follow a config change to another provider midway through a run.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from agent.provider_registry import is_available_safe
from agent.workspace_provider import WorkspaceLease, WorkspaceRequest

if TYPE_CHECKING:
    import sqlite3

    from hermes_cli.kanban_db import Task

logger = logging.getLogger(__name__)


class WorkspaceProviderBusy(RuntimeError):
    """Selected provider has no compatible lease available this tick."""


def _configured_provider_name() -> str:
    from hermes_cli.config import load_config

    config = load_config() or {}
    kanban = config.get("kanban") if isinstance(config, dict) else None
    raw = kanban.get("workspace_provider") if isinstance(kanban, dict) else ""
    return str(raw or "").strip().lower()


@contextmanager
def _provider_scope(scope: str):
    """Bind callbacks to the full home/secret/terminal scope that acquired them."""
    from hermes_cli.kanban_db_dispatch import _worker_profile_scope

    with _worker_profile_scope(scope):
        yield


def _provider(name: str, scope: str, *, require_available: bool):
    from agent import workspace_registry
    from hermes_cli.plugins import discover_plugins

    discover_plugins()  # Kanban CLI/daemon paths do not necessarily import model_tools.
    provider = workspace_registry.get_provider(name, scope=scope)
    if provider is None:
        raise RuntimeError(
            f"kanban.workspace_provider={name!r} is not registered in this profile; "
            "enable the plugin before dispatching worktree tasks"
        )
    if require_available and not is_available_safe(
        provider, logger, "Workspace provider %s availability check failed: %s",
        level=logging.WARNING, exc_info=True,
    ):
        raise RuntimeError(
            f"kanban.workspace_provider={name!r} is registered but unavailable; "
            "finish its setup before dispatching worktree tasks"
        )
    return provider


def _board_identity(conn: "sqlite3.Connection", board: Optional[str]) -> tuple[str, str]:
    slug = board or _kb.get_current_board()
    row = next((r for r in conn.execute("PRAGMA database_list") if r[1] == "main"), None)
    path = str(Path(row[2]).resolve()) if row is not None and row[2] else str(_kb.kanban_db_path(board=slug).resolve())
    return slug, path


def _request(
    task: "Task", run_id: int, owner_id: str, workspace_path: str,
    branch_name: Optional[str], repo_root: str, board_slug: str, board_db_path: str,
) -> WorkspaceRequest:
    return WorkspaceRequest(
        task_id=task.id,
        run_id=int(run_id),
        owner_id=str(owner_id or ""),
        board=board_slug,
        board_db_path=board_db_path,
        access=str(task.workspace_access or "write"),
        workspace_kind=task.workspace_kind,
        requested_path=workspace_path,
        branch_name=branch_name,
        project_id=task.project_id,
        repo_root=repo_root,
    )


def _validate_lease(lease: WorkspaceLease, requested_path: str) -> WorkspaceLease:
    if not isinstance(lease, WorkspaceLease):
        raise TypeError(
            "WorkspaceProvider.try_acquire() must return WorkspaceLease or None, "
            f"got {type(lease).__name__}"
        )
    if not str(lease.lease_id or "").strip():
        raise ValueError("workspace provider returned an empty lease_id")
    path = Path(str(lease.path or "")).expanduser()
    requested = Path(requested_path).expanduser().resolve(strict=False)
    if not path.is_absolute() or (
        path.resolve(strict=False) != requested and not path.is_dir()
    ):
        raise ValueError(
            "workspace provider returned a path that is neither the requested core target "
            f"nor an existing absolute directory: {lease.path!r}"
        )
    return lease


def acquire_workspace(
    conn: "sqlite3.Connection", task: "Task", workspace_path: str,
    branch_name: Optional[str], repo_root: str, *, board: Optional[str] = None,
) -> tuple[str, Optional[str], bool]:
    """Acquire configured provider lease and persist it before worker spawn.

    Returns the effective ``(path, branch)``. No configured provider is an
    exact pass-through. ``WorkspaceProviderBusy`` is a retryable scheduling
    result; every other provider error is an actionable spawn failure.
    """
    name = _configured_provider_name()
    if not name:
        return workspace_path, branch_name, False
    from hermes_constants import hermes_home_key

    provider_scope = hermes_home_key()
    board_slug, board_db_path = _board_identity(conn, board)
    run_id = task.current_run_id
    if run_id is None:
        raise RuntimeError(f"task {task.id} has no active run for workspace acquisition")
    run = conn.execute(
        "SELECT claim_lock FROM task_runs WHERE id = ? AND task_id = ? AND ended_at IS NULL",
        (int(run_id), task.id),
    ).fetchone()
    if run is None:
        raise RuntimeError(f"task {task.id} run {run_id} ended before workspace acquisition")
    request = _request(
        task, int(run_id), run["claim_lock"] or "", workspace_path,
        branch_name, repo_root, board_slug, board_db_path,
    )
    lease = None
    with _provider_scope(provider_scope):
        provider = _provider(name, provider_scope, require_available=True)
        lease = provider.try_acquire(request)
        if lease is None:
            raise WorkspaceProviderBusy(
                f"workspace provider {name!r} is busy for {task.workspace_access} access to {repo_root}"
            )
        try:
            lease = _validate_lease(lease, workspace_path)
            effective_path = str(Path(lease.path).expanduser().resolve(strict=False))
            effective_branch = lease.branch_name if lease.branch_name is not None else branch_name
            with _kb.write_txn(conn):
                bound = conn.execute(
                    "UPDATE task_runs SET workspace_provider = ?, workspace_provider_scope = ?, "
                    "workspace_lease_id = ?, workspace_lease_path = ?, workspace_lease_branch = ?, "
                    "workspace_repo_root = ?, workspace_access = ?, workspace_board = ?, "
                    "workspace_board_db_path = ?, workspace_requested_path = ?, "
                    "workspace_requested_branch = ?, workspace_lease_expires_at = ? "
                    "WHERE id = ? AND task_id = ? AND ended_at IS NULL AND workspace_provider IS NULL",
                    (
                        name, provider_scope, lease.lease_id, effective_path, effective_branch,
                        repo_root, request.access, board_slug, board_db_path, workspace_path,
                        branch_name, lease.expires_at, int(run_id), task.id,
                    ),
                ).rowcount
                if bound == 1:
                    _kb._append_event(
                        conn, task.id, "workspace_acquired",
                        {
                            "provider": name, "lease_id": lease.lease_id,
                            "path": effective_path, "access": request.access,
                        },
                        run_id=int(run_id),
                    )
            if bound != 1:
                raise RuntimeError(
                    f"task {task.id} run {run_id} changed before workspace lease was persisted"
                )
        except Exception:
            if isinstance(lease, WorkspaceLease):
                try:
                    provider.release(request, lease, outcome="acquire_failed")
                except Exception:
                    logger.warning(
                        "Workspace provider compensation release failed for task %s run %s",
                        task.id, run_id, exc_info=True,
                    )
            raise
    return effective_path, effective_branch, True


_BINDING_SQL = """
SELECT r.id AS run_id, r.task_id, r.claim_lock, r.outcome,
       r.workspace_provider, r.workspace_provider_scope, r.workspace_lease_id, r.workspace_lease_path,
       r.workspace_lease_branch, r.workspace_repo_root, r.workspace_access,
       r.workspace_lease_expires_at, r.workspace_lease_released_at,
       r.workspace_board, r.workspace_board_db_path,
       r.workspace_requested_path, r.workspace_requested_branch,
       t.workspace_kind, t.workspace_path, t.branch_name, t.project_id,
       t.workspace_access AS task_workspace_access
  FROM task_runs r
  JOIN tasks t ON t.id = r.task_id
 WHERE r.id = ?
"""


def _binding(conn: "sqlite3.Connection", run_id: int):
    return conn.execute(_BINDING_SQL, (int(run_id),)).fetchone()


def _request_and_lease(row) -> tuple[WorkspaceRequest, WorkspaceLease]:
    task = _kb.Task(
        id=row["task_id"], title="", body=None, assignee=None, status="",
        priority=0, created_by=None, created_at=0, started_at=None, completed_at=None,
        workspace_kind=row["workspace_kind"], workspace_path=row["workspace_path"],
        claim_lock=None, claim_expires=None, tenant=None,
        branch_name=row["branch_name"], project_id=row["project_id"],
        workspace_access=row["workspace_access"] or row["task_workspace_access"] or "write",
    )
    request = _request(
        task, int(row["run_id"]), row["claim_lock"] or "",
        row["workspace_requested_path"], row["workspace_requested_branch"],
        row["workspace_repo_root"], row["workspace_board"], row["workspace_board_db_path"],
    )
    lease = WorkspaceLease(
        lease_id=row["workspace_lease_id"],
        path=row["workspace_lease_path"],
        branch_name=row["workspace_lease_branch"],
        expires_at=row["workspace_lease_expires_at"],
    )
    return request, lease


def renew_workspace_lease(
    conn: "sqlite3.Connection", run_id: int, *, board: Optional[str] = None,
) -> bool:
    """Renew one persisted active lease; false when absent or ownership was lost."""
    row = _binding(conn, run_id)
    if row is None or not row["workspace_provider"] or row["workspace_lease_released_at"] is not None:
        return True
    scope = row["workspace_provider_scope"]
    with _provider_scope(scope):
        provider = _provider(row["workspace_provider"], scope, require_available=False)
        request, lease = _request_and_lease(row)
        return bool(provider.renew(request, lease))


def renew_active_workspace_leases(
    conn: "sqlite3.Connection", *, board: Optional[str] = None,
) -> list[int]:
    """Best-effort dispatcher renewal; return run ids that lost renewal."""
    rows = conn.execute(
        "SELECT id FROM task_runs WHERE ended_at IS NULL "
        "AND workspace_provider IS NOT NULL AND workspace_lease_released_at IS NULL"
    ).fetchall()
    lost: list[int] = []
    for row in rows:
        run_id = int(row["id"])
        try:
            if not renew_workspace_lease(conn, run_id, board=board):
                lost.append(run_id)
        except Exception:
            lost.append(run_id)
            logger.warning("Workspace lease renewal failed for run %s", run_id, exc_info=True)
    return lost


def release_workspace_lease(
    conn: "sqlite3.Connection", run_id: int, *, board: Optional[str] = None,
    outcome: Optional[str] = None,
) -> bool:
    """Release one persisted lease and mark it released after provider success."""
    row = _binding(conn, run_id)
    if row is None or not row["workspace_provider"] or row["workspace_lease_released_at"] is not None:
        return True
    scope = row["workspace_provider_scope"]
    request, lease = _request_and_lease(row)
    release_outcome = str(outcome or row["outcome"] or "released")
    with _provider_scope(scope):
        provider = _provider(row["workspace_provider"], scope, require_available=False)
        provider.release(request, lease, outcome=release_outcome)
    released_at = int(time.time())
    with _kb.write_txn(conn):
        marked = conn.execute(
            "UPDATE task_runs SET workspace_lease_released_at = ? "
            "WHERE id = ? AND workspace_lease_released_at IS NULL",
            (released_at, int(run_id)),
        ).rowcount
        if marked:
            _kb._append_event(
                conn, row["task_id"], "workspace_released",
                {
                    "provider": row["workspace_provider"],
                    "lease_id": row["workspace_lease_id"],
                    "outcome": release_outcome,
                },
                run_id=int(run_id),
            )
    return bool(marked)


def release_finished_workspace_leases(
    conn: "sqlite3.Connection", *, board: Optional[str] = None,
) -> list[int]:
    """Release ended runs only after no worker PID remains able to execute."""
    rows = conn.execute(
        "SELECT id FROM task_runs WHERE ended_at IS NOT NULL AND worker_pid IS NULL "
        "AND workspace_provider IS NOT NULL AND workspace_lease_released_at IS NULL"
    ).fetchall()
    released: list[int] = []
    for row in rows:
        run_id = int(row["id"])
        try:
            if release_workspace_lease(conn, run_id, board=board):
                released.append(run_id)
        except Exception:
            logger.warning("Workspace lease release failed for run %s", run_id, exc_info=True)
    return released


def task_has_provider_workspace(conn: "sqlite3.Connection", task_id: str) -> bool:
    """Whether any run delegated this task's checkout lifecycle to a provider."""
    return conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? AND workspace_provider IS NOT NULL LIMIT 1",
        (task_id,),
    ).fetchone() is not None


def task_has_pending_workspace_lease(conn: "sqlite3.Connection", task_id: str) -> bool:
    """Whether deleting task history would orphan provider release authority."""
    return conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? AND workspace_provider IS NOT NULL "
        "AND workspace_lease_released_at IS NULL LIMIT 1",
        (task_id,),
    ).fetchone() is not None


# Late-bound facade avoids the kanban_db <-> topical-sibling import cycle and
# preserves its established monkeypatch seam.
from hermes_cli import kanban_db as _kb  # noqa: E402
