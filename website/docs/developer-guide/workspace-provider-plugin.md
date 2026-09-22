---
title: Workspace Provider Plugins
description: Reserve and coordinate Kanban Git workspaces before task workers start
---

# Workspace Provider Plugins

Workspace providers let an external plugin coordinate Kanban Git workspaces before a task worker
starts. Hermes still owns task claims, worktree defaults, process supervision, and run history. The
provider owns repository scheduling policy—for example, shared reader leases and an exclusive writer
lease backed by a host-global SQLite database.

This interface applies only to Kanban tasks with `workspace_kind: worktree`. It does not change
ordinary CLI sessions, cron workdirs, Desktop Projects, scratch workspaces, or `dir` workspaces.

## Enable a provider

Install and enable the plugin in every profile that can dispatch or execute these tasks, then select
it explicitly:

```yaml title="~/.hermes/config.yaml"
plugins:
  enabled:
    - git-workspace-leases

kanban:
  workspace_provider: git-workspace-leases
```

An installed provider is inert until `kanban.workspace_provider` names it. An empty value preserves
Hermes' built-in worktree behavior exactly. A configured provider that is missing or unavailable
fails closed instead of silently starting an unprotected worker.

Choose access per task:

```bash
hermes kanban create "Review API migration" \
  --assignee reviewer \
  --workspace worktree:/srv/repos/api \
  --workspace-access read

hermes kanban create "Implement API migration" \
  --assignee builder \
  --workspace worktree:/srv/repos/api \
  --workspace-access write
```

`read` and `write` are coordination and publication semantics. A read checkout may remain writable
locally so compilers and tests can create artifacts; the provider or publication broker must prevent
repository publication. Missing access defaults conservatively to `write`.

## Provider contract

```python title="~/.hermes/plugins/git-workspace-leases/__init__.py"
from agent.workspace_provider import WorkspaceLease, WorkspaceProvider


class GitWorkspaceLeases(WorkspaceProvider):
    name = "git-workspace-leases"

    @staticmethod
    def _owner(request):
        return (
            request.board_db_path,
            request.task_id,
            request.run_id,
            request.owner_id,
        )

    def is_available(self):
        # Cheap and local. Do not make a network request here.
        return True

    def try_acquire(self, request, **kwargs):
        lease = my_store.try_acquire(
            repo=request.repo_root,
            owner=self._owner(request),
            access=request.access,
        )
        if lease is None:
            return None  # contention: defer without spending task retry budget
        return WorkspaceLease(
            lease_id=lease.public_id,       # stable identifier, never a bearer secret
            path=request.requested_path,    # core materializes this target after acquire
            branch_name=request.branch_name,
            expires_at=lease.expires_at,
        )

    def renew(self, request, lease, **kwargs):
        return my_store.renew(lease.lease_id, owner=self._owner(request))

    def release(self, request, lease, *, outcome, **kwargs):
        my_store.release(
            lease.lease_id,
            owner=self._owner(request),
            outcome=outcome,
        )  # idempotent


provider = GitWorkspaceLeases()


def register(ctx):
    ctx.register_workspace_provider(provider)
```

`WorkspaceRequest` is immutable and carries:

- task, run, and claim-owner identities;
- board slug and absolute board DB path;
- `read` or `write` access;
- workspace kind, requested path, branch, project id, and repository root.

`WorkspaceLease` is immutable. `lease_id` must be non-empty and non-secret, and
`path` must be absolute. When it equals `request.requested_path`, Hermes can materialize
that planned target after acquisition. A different provider-owned path must already be an existing
Git checkout before `try_acquire` returns. Hermes persists and launches the worker at the
effective path and branch.

`try_acquire` must not wait for another task's entire lifetime. Return `None` when busy so the
dispatcher can requeue the card. Bounded work needed to materialize an already-granted checkout is
fine. `renew` returns `False` after ownership is lost. `release` must be idempotent because crash and
restart reconciliation may retry it.

## Lifecycle

For a selected provider Hermes performs this sequence:

1. Atomically claim the task and open a run.
2. Resolve the built-in worktree path.
3. Call `try_acquire` before spawning any worker process.
4. Persist provider, lease, path, branch, repository, expiry, and access on `task_runs`.
5. Materialize the planned core target, or validate an alternate provider-owned Git checkout and
   branch.
6. Spawn the worker in the effective path.
7. Renew from dispatcher reconciliation. Provider TTL should exceed several dispatch intervals.
8. Release immediately if process spawn fails.
9. For an ended run, wait until no retained worker PID can execute, then release. A failed release
   stays pending and is retried by later dispatcher ticks.

Provider callbacks run outside Kanban SQLite write transactions. The captured provider name and home
scope—not the current config value—drive renewal and release. Do not mix a scoped provider and a
directly registered global provider under the same name: current persisted bindings do not record
which registry tier originally supplied the lease.

Hermes keeps the task's requested repository/path unchanged. Provider-returned effective paths live
on the run only, so a later review or retry resolves from the original repository after the old
checkout is removed. For a provider-managed run, the provider owns its effective checkout cleanup;
core must not delete that provider path during task completion or Kanban GC.

## Provider responsibilities

Hermes supplies lifecycle ordering, not a lock algorithm. Production providers should implement:

- durable reader/writer admission with writer fairness;
- identity-safe, token-checked renewal and release;
- TTL and startup recovery for a host that stays down;
- one private checkout per lease, belonging to `request.repo_root` and the effective branch;
- an exact base-commit pin inside provider allocation when policy requires it (return a
  provider-owned checkout, because the current request does not carry a base SHA);
- checkout cleanup plus quarantine/retry when cleanup fails;
- fencing at publication time so an expired writer cannot push after a successor starts.

Git worktree locking is insufficient: `git worktree lock` prevents removal of one checkout but does
not exclude other readers or writers. Likewise, this provider only coordinates Hermes participants;
branch protection remains necessary for humans and external automation.
