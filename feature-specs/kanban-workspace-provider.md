# Kanban Workspace Provider Lifecycle

## Fork Metadata

- **Status:** Fork-only
- **Tracking:** [dkropachev/hermes#1](https://github.com/dkropachev/hermes/issues/1)
- **First implementation:** [dkropachev/hermes#4](https://github.com/dkropachev/hermes/pull/4),
  squash commit
  [`fcf48e44dafba0a804e3652f5a0344daa7face3c`](https://github.com/dkropachev/hermes/commit/fcf48e44dafba0a804e3652f5a0344daa7face3c)
- **Implementation base:**
  [`NousResearch/hermes-agent@10e7de79a9ba602c5c4c42f81560511e5de8d2b1`](https://github.com/NousResearch/hermes-agent/commit/10e7de79a9ba602c5c4c42f81560511e5de8d2b1)
- **Latest upstream assessment:** 2026-09-22 at
  [`NousResearch/hermes-agent@4094ab610dc7f8554461e7a9b3ffe5e419220949`](https://github.com/NousResearch/hermes-agent/commit/4094ab610dc7f8554461e7a9b3ffe5e419220949);
  no equivalent blocking workspace-provider contract was present, so the complete fork delta remains
  required.

## Summary

The Kanban workspace-provider lifecycle lets an explicitly selected plugin reserve a repository and
select an effective Git workspace before Hermes mutates a worktree or starts a task worker. Hermes owns task claims,
ordering, process supervision, persisted lease authority, reconciliation, and release timing. The
plugin owns repository admission policy, lease durability, and effective-coordinate selection. It
may allocate an alternate checkout; otherwise Hermes materializes the planned core target after
admission. The provider owns cleanup of every checkout it manages.

The seam exists so an external provider can implement repository-wide reader/writer coordination
without putting a GitHub-specific lock service or publication policy in Hermes core.

## Why This Fork Carries It

Hermes creates a worktree per Kanban task, but upstream has no extension point that can atomically
reserve a repository before workspace creation and process spawn:

- `kanban_task_claimed` is an observer hook; its return value and exceptions cannot defer
  dispatch.
- `on_kanban_worker_spawned` runs after the worker already exists.
- terminal-environment providers allocate lazily on first tool use, after session setup may inspect
  the workspace.
- `git worktree lock` prevents removal of one checkout; it is not a repository-wide
  reader/writer lock.

The core therefore needs a small blocking seam at the dispatch boundary. The seam stays generic so
coordination products and Git hosting policy remain external plugins.

### Goals

- Let a profile-scoped plugin admit or defer a Kanban worktree task before Git mutation and worker
  spawn.
- Preserve enough durable identity to renew and release the same lease after config changes,
  process restarts, or a shared-board tick from another profile.
- Treat ordinary lock contention as scheduling pressure rather than task failure.
- Stop a worker that loses lease ownership before another worker can safely proceed.
- Release only after Hermes can prove the worker no longer executes.
- Keep the no-provider path and non-worktree workspaces compatible with upstream behavior.
- Express `read` versus `write` coordination intent without pretending to enforce
  filesystem permissions.

### Non-Goals

- A lock database, reader/writer admission algorithm, writer fairness, or checkout pool in core.
  Those belong to the external provider tracked by
  [#2](https://github.com/dkropachev/hermes/issues/2).
- GitHub API integration, branch publication, push credentials, or stale-writer fencing. Publication
  fencing is tracked by [#3](https://github.com/dkropachev/hermes/issues/3).
- Coordination of ordinary CLI sessions, cron working directories, Desktop Projects, `scratch`
  workspaces, or `dir` workspaces.
- Protection from humans, CI, or other automation that bypasses the provider. Branch protection is
  still required.

## Behavior

### Terminology

- **Provider:** A `WorkspaceProvider` implementation registered by a plugin.
- **Request:** Immutable task/run context supplied on acquire, renew, and release.
- **Lease:** Immutable provider-issued authority and effective workspace coordinates.
- **Requested coordinates:** The core-planned worktree path and branch derived from the task.
- **Effective coordinates:** The path and optional branch returned by the provider and used for this
  run.
- **Provider scope:** The `hermes_home_key()` captured when the lease is acquired.
- **Access:** `read` or `write` coordination and publication intent.
- **Busy:** `try_acquire()` returned `None` because admission is temporarily
  unavailable. Busy is not a provider error.

### Applicability And Selection

The feature applies only when both conditions are true:

1. the task uses `workspace_kind: worktree`; and
2. `kanban.workspace_provider` contains a non-empty provider name.

An empty or absent setting preserves built-in workspace behavior. Registration alone is inert, so
installing a plugin cannot silently change dispatch semantics. `scratch` and `dir`
tasks never call the provider, even when one is selected.

Provider names are trimmed and normalized to lowercase. Plugin discovery runs before lookup because
Kanban CLI and daemon paths do not necessarily import the normal model-tool discovery path. A
configured name that resolves neither in the current profile nor in the registry's global fallback,
or whose cheap local `is_available()` check fails, is an actionable dispatch failure.
Hermes must fail closed; it must never fall back to an unprotected worker.

The real plugin API registers providers under the plugin's Hermes-home scope. The lower-level
registry also supports an explicitly global registration that is visible as a fallback in every
profile; a scoped same-name registration wins. Acquisition captures the active scope on the run.
Renewal and release re-enter the captured home, secret, and terminal scope and resolve the captured
provider name there, regardless of the profile currently ticking a shared board.

The durable binding must also preserve which registration tier supplied the lease. Unloading a
scoped provider must not redirect its lease to a same-name global fallback, and adding a scoped
provider must not shadow a lease acquired from the global fallback. Hot reload within the same
logical registration slot is allowed only when the replacement remains lease-compatible.

### Provider Contract

The public contract is:

```python
class WorkspaceProvider(ProviderBase):
    def is_available(self) -> bool: ...

    def try_acquire(
        self, request: WorkspaceRequest, **kwargs
    ) -> WorkspaceLease | None: ...

    def renew(
        self, request: WorkspaceRequest, lease: WorkspaceLease, **kwargs
    ) -> bool: ...

    def release(
        self,
        request: WorkspaceRequest,
        lease: WorkspaceLease,
        *,
        outcome: str,
        **kwargs,
    ) -> None: ...
```

`WorkspaceRequest` is frozen and contains:

| Field            | Meaning                                                            |
| ---------------- | ------------------------------------------------------------------ |
| `task_id`        | Stable Kanban task ID.                                             |
| `run_id`         | Current `task_runs.id` opened by the atomic claim.                 |
| `owner_id`       | Current claim token/owner identity.                                |
| `board`          | Board slug.                                                        |
| `board_db_path`  | Absolute path to the board database.                               |
| `access`         | `read` or `write` coordination intent.                             |
| `workspace_kind` | The task workspace kind; currently always `worktree` on this path. |
| `requested_path` | Absolute mutation-free core worktree target.                       |
| `branch_name`    | Planned branch, including the default `wt/<task-id>` when needed.  |
| `project_id`     | Optional linked Hermes Project.                                    |
| `repo_root`      | Resolved main repository root used for coordination identity.      |

`WorkspaceLease` is frozen and contains a non-empty opaque `lease_id`, an absolute
`path`, an optional `branch_name`, and an optional `expires_at` timestamp.
The lease ID is persisted and exposed in diagnostics, so it must be a stable identifier rather than a
bearer secret.

The returned path has two valid shapes:

- It may equal `request.requested_path`. The path need not exist yet; Hermes materializes
  the planned worktree only after the lease is persisted.
- It may point to a provider-owned checkout. In that case it must already be an existing absolute
  directory, must be a Git checkout, and must match the effective branch when Git reports a branch.

`try_acquire()` must return promptly. It may do bounded work needed to prepare an already
granted checkout, but must return `None` instead of waiting for another task's lifetime.
`renew()` returns `False` when ownership is lost. `release()` must be
idempotent because reconciliation retries it after crashes and restarts.

Provider callbacks must never execute while a Kanban SQLite write transaction is open.

### Access Semantics

`workspace_access` accepts `read` and `write` and defaults
conservatively to `write`. `read` is valid only for worktree tasks. Low-level task
graph insertion inherits the root task's access and accepts an optional child override. The automatic
LLM decomposer currently supplies neither workspace kind nor access, so its children inherit the
root's values. Every creation path must enforce the access/kind invariant.

Access is coordination and publication intent, not `chmod`:

- a provider may allow multiple readers for one repository;
- a writer is expected to exclude readers and other writers;
- a read checkout may remain writable locally so compilers and tests can create artifacts; and
- the external publication boundary must prevent a reader from publishing.

Core transports and persists the intent but does not implement the lock algorithm.

### Acquire, Materialize, And Spawn Ordering

For a selected provider, the required order is:

```text
atomic task claim + run creation
  -> build mutation-free WorktreePlan
  -> resolve selected provider in the acquiring profile
  -> try_acquire outside a Kanban write transaction
  -> validate lease
  -> persist lease identity and requested/effective coordinates
  -> materialize or validate the Git checkout
  -> spawn worker in the effective path
  -> persist PID and restart-safe process fingerprint
```

The plan may inspect Git to resolve the repository and branch, but no worktree creation or other
filesystem mutation may occur before acquisition succeeds.

Lease persistence and the `workspace_acquired` event are one Kanban write transaction.
If the active run changed or ended before the conditional update, acquisition fails and Hermes
compensates by releasing the provider lease.

Provider-managed effective paths and branches live on the run only. Hermes must not overwrite the
task's requested repository/path/branch with an ephemeral checkout. A retry or later review must
start again from the original task request after the provider removes the old checkout.

### Contention And Acquisition Failures

| Condition                                                            | Required behavior                                                                                                                                                                                                                                                          |
| -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Provider returns `None`                                              | End the claimed run as `workspace_deferred`, restore its source lane, clear the claim, emit a deferral event/result, and retry on a later tick. Do not materialize, spawn, increment `consecutive_failures`, set `last_failure_error`, or trigger infrastructure cooldown. |
| Provider is missing, unavailable, or raises before returning a lease | Fail closed through the normal actionable workspace/spawn-failure path. Never start an unprotected worker.                                                                                                                                                                 |
| Provider returns a value that is not `WorkspaceLease`                | Fail the run with an actionable type error. Core has no valid lease contract to compensate.                                                                                                                                                                                |
| A returned `WorkspaceLease` has an invalid ID or path                | Call `release(..., outcome="acquire_failed")`, then fail the run.                                                                                                                                                                                                          |
| Lease validation or persistence fails after provider acquisition     | Attempt the same `acquire_failed` compensation outside the write transaction. If compensation fails before authority was persisted, provider TTL/recovery is the final backstop.                                                                                           |
| Checkout materialization/validation fails after persistence          | End the run through existing failure accounting and call `release(..., outcome="workspace_failed")`. A failed release remains pending for dispatcher retry.                                                                                                                |
| Process spawn raises before a PID exists                             | End the run through existing spawn-failure policy and release immediately with `outcome="spawn_failed"`.                                                                                                                                                                   |

Only contention has special no-failure semantics. Other errors retain the existing Kanban failure
breaker and infrastructure-deferral rules for their failure class.

### Renewal And Lost Ownership

The dispatcher renews active persisted leases during reconciliation. Renewal must not depend only on
worker hooks or heartbeats, because a worker can stall or Hermes can restart while the external lease
still needs supervision.

Renewal uses the provider identity, request coordinates, and lease fields captured on the run. The
current `kanban.workspace_provider` value is irrelevant to an existing lease. Availability
is checked on initial acquisition; renew and release still call a registered captured provider when
its availability probe later changes. The current implementation reconstructs identity from name
and scope only; the scoped/global resolution-tier gap is recorded below.

`False` from `renew()`, a missing captured provider, or a renewal exception means
ownership is lost. The required fail-closed sequence is:

1. route termination to the supervisor that can authoritatively identify the worker from its
   persisted PID/fingerprint and claim owner;
2. keep the task claimed and the lease pending while execution is alive or cannot be proven stopped,
   retrying termination/reconciliation without spawning a successor;
3. once execution is proven stopped, clears the claim, ends and requeues the run as
   `workspace_lease_lost` without spending the task failure budget; and
4. calls idempotent release with `outcome="workspace_lease_lost"`.

For a verified host-local worker, the current termination helper can perform this sequence. A
non-local claim or unprovable process identity must not be treated as proof of death merely because
the current dispatcher cannot signal it. This ordering prevents an expired writer from continuing
beside a successor admitted by the provider. Publication fencing is still required for a process
that escapes local supervision.

### Terminal Paths, Restart, And Release

Normal completion and every abnormal or handoff path that closes a run must preserve release
authority until process death is proven. Relevant paths include completion, block, request-review,
request-changes, schedule/park, archive, descendant invalidation after a parent reopens, crash,
timeout, stale-claim reclaim, orphan reconciliation, spawn failure, renewal loss, and restart
reconciliation.

Ending a run does not by itself release the lease. The ended run retains its worker PID/fingerprint.
The dispatcher terminal reaper first proves that the process exited or was terminated and clears the
PID. A later ended-run sweep releases rows where:

- `ended_at IS NOT NULL`;
- `worker_pid IS NULL`;
- `workspace_provider IS NOT NULL`; and
- `workspace_lease_released_at IS NULL`.

Core marks `workspace_lease_released_at` and emits `workspace_released` only after
the provider call succeeds. A release error leaves the row pending and is retried on future
dispatcher ticks or after restart. This is why provider release must be idempotent.

If the captured provider is missing after reload or restart, Hermes does not redirect the callback
to the currently configured provider and does not pretend the lease was released. It fails closed,
retains the pending row, and relies on provider TTL/fencing until the original provider is available.
Unloading a plugin must not blanket-release live leases.

### Checkout Ownership And Destructive Operations

For a provider-managed run, provider release owns cleanup of its effective checkout:

- core completion, deferred-parent worktree cleanup, and Kanban garbage collection must not remove
  that checkout; and
- the provider must clean or quarantine its checkout without keeping the repository locked forever.

A later provider-disabled run can create a new core-managed worktree for the same task. That later
path is core-owned and must remain eligible for normal safe cleanup; ownership must follow the
run/path rather than the fact that some historical run once used a provider.

A pending lease blocks hard task deletion so the row holding release authority cannot disappear.
The dashboard reports this as HTTP 409. A board with any pending provider lease cannot be removed.
Archiving remains possible, but release is reconciled only after the worker is gone.

Board exports and imports are machine-local snapshots, not transfers of live lease authority. The
snapshot scrubber clears provider, scope, lease, effective/requested coordinates, expiry, and release
state. It does not modify the live source board, whose dispatcher remains responsible for release.

### Provider Responsibilities

A production provider should supply:

- durable reader/writer admission with writer fairness;
- token- and owner-checked renew/release;
- TTL and startup recovery for a host that stays down;
- a private checkout for every lease, belonging to `request.repo_root` and the effective branch;
- checkout cleanup with quarantine and retry on failure; and
- publication fencing so an expired writer cannot push after a successor starts.

Provider TTL should exceed several dispatcher intervals. Neither Git worktree locking nor this
cooperative seam protects against actors that bypass the provider. Core verifies that an alternate
path is some Git checkout and checks its branch when Git reports one; the trusted provider remains
responsible for proving repository identity. The current request lacks an immutable base SHA. A
provider that requires exact-base admission must therefore return its own checkout pinned during
acquisition until the API gap below is resolved.

### User And Integration Surfaces

- Config: `kanban.workspace_provider`.
- CLI: `hermes kanban create --workspace-access read|write`.
- Agent tool: `kanban_create.workspace_access` with the same enum and default.
- Dashboard create API: `workspace_access`.
- Task graph: low-level child override or inherited root access; automatic decomposition currently
  uses inheritance.
- CLI, tool, and run output: requested access plus provider lease diagnostics.
- Plugin API: `ctx.register_workspace_provider(provider)`.

Existing task rows migrate to `write`. Existing run rows have no provider binding and remain
ordinary core-managed runs.

## Entry Points

- [`agent/workspace_provider.py`](../agent/workspace_provider.py)
- [`agent/workspace_registry.py`](../agent/workspace_registry.py)
- [`hermes_cli/kanban_workspace_provider.py`](../hermes_cli/kanban_workspace_provider.py)
- [`hermes_cli/kanban_db_dispatch.py`](../hermes_cli/kanban_db_dispatch.py)
- [`hermes_cli/kanban_db_workspace.py`](../hermes_cli/kanban_db_workspace.py)
- [`hermes_cli/kanban_db.py`](../hermes_cli/kanban_db.py)
- [`hermes_cli/kanban_db_connect.py`](../hermes_cli/kanban_db_connect.py)
- [`hermes_cli/plugins.py`](../hermes_cli/plugins.py)
- [`website/docs/developer-guide/workspace-provider-plugin.md`](../website/docs/developer-guide/workspace-provider-plugin.md)
- [`website/docs/reference/cli-commands.md`](../website/docs/reference/cli-commands.md)

## Subfeatures

### Contract, Discovery, And Profile Scope

#### Entry Points

- [`agent/workspace_provider.py`](../agent/workspace_provider.py)
- [`agent/workspace_registry.py`](../agent/workspace_registry.py)
- [`hermes_cli/plugins.py`](../hermes_cli/plugins.py)
- [`hermes_cli/kanban_workspace_provider.py`](../hermes_cli/kanban_workspace_provider.py)

#### Invariants

- Plugin registration is profile-scoped and case-normalized; an explicit lower-level global
  registration is the fallback only when the scope has no same-name provider.
- The acquisition scope, not the profile performing later reconciliation, owns renew and release.
- A missing or unavailable explicitly selected provider fails closed.
- Unload unregisters only the owning profile's provider slot and does not release live leases.

### Worktree Admission And Dispatch

#### Entry Points

- [`hermes_cli/kanban_db_workspace.py`](../hermes_cli/kanban_db_workspace.py)
- [`hermes_cli/kanban_db_dispatch.py`](../hermes_cli/kanban_db_dispatch.py)
- [`hermes_cli/kanban_workspace_provider.py`](../hermes_cli/kanban_workspace_provider.py)

#### Invariants

- Acquisition follows atomic claim/run creation but precedes worktree mutation and process spawn.
- Busy defers without failure accounting, worktree creation, or spawn.
- Lease identity is persisted before materialization and spawn.
- Provider callbacks run outside Kanban SQLite write transactions.

### Reconciliation And Release

#### Entry Points

- [`hermes_cli/kanban_workspace_provider.py`](../hermes_cli/kanban_workspace_provider.py)
- [`hermes_cli/kanban_db_dispatch.py`](../hermes_cli/kanban_db_dispatch.py)
- [`hermes_cli/kanban_db_workspace.py`](../hermes_cli/kanban_db_workspace.py)
- [`hermes_cli/kanban_db.py`](../hermes_cli/kanban_db.py)
- [`hermes_cli/kanban_ops.py`](../hermes_cli/kanban_ops.py)

#### Invariants

- Active leases renew from dispatcher reconciliation.
- Lost ownership stops proven workers before requeue and release.
- Ended runs release only after no worker PID remains capable of execution.
- Failed release remains durable and retryable.
- Core cleanup and deletion cannot erase provider-owned checkout or release authority.

### Access And User Surfaces

#### Entry Points

- [`hermes_cli/kanban_parser.py`](../hermes_cli/kanban_parser.py)
- [`hermes_cli/kanban.py`](../hermes_cli/kanban.py)
- [`hermes_cli/kanban_db_graph.py`](../hermes_cli/kanban_db_graph.py)
- [`hermes_cli/kanban_decompose.py`](../hermes_cli/kanban_decompose.py)
- [`hermes_cli/kanban_output.py`](../hermes_cli/kanban_output.py)
- [`tools/kanban_tools.py`](../tools/kanban_tools.py)
- [`tools/kanban_tools_schemas.py`](../tools/kanban_tools_schemas.py)
- [`plugins/kanban/dashboard/plugin_api.py`](../plugins/kanban/dashboard/plugin_api.py)

#### Invariants

- Missing access defaults to conservative `write`.
- `read` is valid only for worktree tasks and is never described as a filesystem mode.
- CLI, tool, dashboard, task decomposition, context, and output preserve the same access value.

## Persisted State And Compatibility

The task carries requested coordination intent:

| Table   | Column             | Contract                                                 |
| ------- | ------------------ | -------------------------------------------------------- |
| `tasks` | `workspace_access` | Non-null `read` or `write`; old rows migrate to `write`. |

Each acquired run captures all authority needed to reconstruct callbacks without current config:

| Column                        | Contract                                                 |
| ----------------------------- | -------------------------------------------------------- |
| `workspace_provider`          | Normalized selected provider name.                       |
| `workspace_provider_scope`    | Acquiring `hermes_home_key()`.                           |
| `workspace_lease_id`          | Opaque provider lease identifier.                        |
| `workspace_lease_path`        | Effective absolute checkout path.                        |
| `workspace_lease_branch`      | Effective branch.                                        |
| `workspace_repo_root`         | Original resolved repository root.                       |
| `workspace_access`            | Access captured for this run.                            |
| `workspace_board`             | Board slug.                                              |
| `workspace_board_db_path`     | Absolute board DB identity.                              |
| `workspace_requested_path`    | Mutation-free core target supplied on acquire.           |
| `workspace_requested_branch`  | Core-planned branch supplied on acquire.                 |
| `workspace_lease_expires_at`  | Optional provider expiry for diagnostics/reconstruction. |
| `workspace_lease_released_at` | Set only after successful provider release.              |

Name plus home scope do not yet capture whether acquisition resolved a scoped registration or the
global fallback, and requested coordinates do not yet include an immutable base SHA. Those missing
identity fields are explicit implementation gaps below; a schema/API fix must migrate old rows
conservatively and fail closed when their original identity cannot be reconstructed.

The feature also depends on pre-existing Kanban lifecycle fields. They are not part of the fork's
schema delta, but changing their meaning can break lease safety:

| Existing fields                                                 | Dependency                                                                       |
| --------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `tasks.current_run_id`, `status`, `claim_lock`, `claim_expires` | Fence conditional acquire/reclaim updates to the active claimed run.             |
| `tasks.worker_pid`, `worker_started_at`                         | Drive active-worker supervision before the task leaves `running`.                |
| `task_runs.claim_lock`, `claim_expires`                         | Reconstruct the immutable request owner and identify the owning host/supervisor. |
| `task_runs.worker_pid`, `worker_started_at`                     | Retain restart-safe process evidence after a task status transition.             |
| `task_runs.ended_at`, `outcome`                                 | Gate ended-run release and supply its idempotent release outcome.                |

Fresh-schema DDL in `hermes_cli/kanban_db.py`, additive migration columns, and table-rebuild
specs in `hermes_cli/kanban_db_connect.py` must stay synchronized. Old task/run rows remain
valid. Board export/import scrubs machine-local lease authority while retaining the task's access
intent.

Changing or removing these columns requires a migration plan for databases that may contain active
or unreleased leases. Never discard the only persisted provider/scope/lease identity during an
upstream rebase.

## Invariants

- No selected provider means built-in scratch, dir, and worktree behavior remains unchanged.
- Registering a provider has no effect until `kanban.workspace_provider` selects it.
- Explicit selection is fail-closed when the provider is missing, unavailable, or raises.
- Plugin-origin provider registration and callbacks are bound to the owning Hermes profile;
  explicitly global registry providers remain global fallbacks by design.
- Acquisition happens after claim/run creation and before Git mutation or worker spawn.
- Provider callbacks never run inside a Kanban SQLite write transaction.
- Busy is a deferral: it neither starts a worker nor consumes task failure budget.
- A validated lease is persisted before its checkout is materialized or used.
- Task requested coordinates are never replaced with a provider's ephemeral coordinates.
- Renew and release use the exact persisted provider registration slot, not current config or a
  newly shadowing/fallback same-name provider.
- Renewal loss stops the worker before clearing its claim or admitting a retry.
- Completion and failure never release while a worker can still execute.
- Release success is marked only after the idempotent provider call succeeds.
- Failed release remains durable and retryable across dispatcher restarts.
- Cleanup ownership follows the run/path: provider-managed checkouts belong to provider release;
  later core-managed worktrees remain eligible for core cleanup and GC.
- Hard deletion and board removal cannot erase pending release authority.
- `read` and `write` express coordination/publication intent, not filesystem
  permissions.
- Existing rows and omitted access values default conservatively to `write`.

## Known Implementation Gaps

These are contract violations in the implementation first delivered by
[`fcf48e44`](https://github.com/dkropachev/hermes/commit/fcf48e44dafba0a804e3652f5a0344daa7face3c).
They must remain visible during a rebase; a clean textual merge does not resolve them.

- [`hermes_cli/kanban_db_graph.py`](../hermes_cli/kanban_db_graph.py)
  `_insert_decomposed_child()` validates the access enum but does not reject
  `workspace_access="read"` when a decomposed child selects or inherits
  `scratch`/`dir`. Route child creation through the same access/kind invariant as
  `create_task()`.
  `missing:decomposed-non-worktree-read-rejected`
- [`hermes_cli/kanban_db_dispatch.py`](../hermes_cli/kanban_db_dispatch.py)
  `_reclaim_lost_workspace_leases()` holds the claim for a surviving host-local process, but
  `_worker_survived_termination()` deliberately falls through for a non-local claim or a
  termination attempt with no local signaling authority. The current path can then clear/requeue and
  release without proving remote death. Reconciliation must defer to the owning supervisor or
  otherwise retain the claim/release authority until death is authoritative.
  `missing:nonlocal-renewal-loss-holds-claim`
- [`hermes_cli/kanban_db_dispatch.py`](../hermes_cli/kanban_db_dispatch.py)
  `_dispatch_lane_task()` stores the returned PID in a local variable before
  `_set_worker_pid()` persists it. If persistence raises after the process started, the
  exception path neither terminates that PID nor releases immediately, while the ended run may still
  have a null durable PID and become eligible for release on the next sweep. The compensation path
  must persist or terminate the spawned process and prove death before release.
  `missing:spawned-pid-persistence-failure-is-fenced`
- [`hermes_cli/kanban_db.py`](../hermes_cli/kanban_db.py)
  `_reclaim_dangling_run()`, used by unblock and review-reopen recovery, closes a leaked run
  and clears its claim-owner identity and durable PID without checking liveness or terminating it. A
  provider-bound row can then satisfy the ended-run release query with no process proof and reconstruct
  the release request with an empty owner. Recovery must retain callback/process identity for the
  terminal reaper or authoritatively stop the worker before clearing it.
  `missing:dangling-run-recovery-proves-death`
- [`hermes_cli/kanban_db_workspace.py`](../hermes_cli/kanban_db_workspace.py) and
  [`hermes_cli/kanban_ops.py`](../hermes_cli/kanban_ops.py) use
  `task_has_provider_workspace()`, which remains true forever after any provider-bound run.
  If a later run executes with the provider disabled and creates a core-owned worktree, completion and
  GC still skip it, while no provider release owns that new path. Cleanup ownership must be keyed to
  the effective run/path rather than historical provider use.
  `missing:provider-to-core-cleanup-ownership`
- [`agent/provider_registry.py`](../agent/provider_registry.py) resolves a scoped provider
  first and then a same-name global fallback on every lookup, while
  [`hermes_cli/kanban_workspace_provider.py`](../hermes_cli/kanban_workspace_provider.py)
  persists only provider name plus home scope. Scoped unload can therefore redirect callbacks to a
  global implementation, and later scoped registration can shadow a lease acquired globally.
  Persist the resolution tier/registration slot or disallow global fallback for workspace leases.
  `missing:provider-resolution-tier-binding`
- [`agent/workspace_provider.py`](../agent/workspace_provider.py) does not carry an immutable
  base ref/SHA in `WorkspaceRequest`, and
  [`hermes_cli/kanban_db_workspace.py`](../hermes_cli/kanban_db_workspace.py) may create the
  planned branch from mutable repository `HEAD` after acquisition. A provider returning the
  core target cannot guarantee the exact base it admitted. Capture and persist the planned base
  commit, then materialize from that pin; until then a provider requiring an exact base must return
  its own already-pinned checkout.
  `missing:workspace-request-pins-base-ref`

## Rebase Assessment

### Latest Upstream Assessment

The feature was applied to upstream `10e7de79a9`. At the 2026-09-22 assessment,
`NousResearch/hermes-agent` was at `4094ab610d`: 307 upstream commits had landed,
while the fork-side implementation remained one squash commit. Searches of the upstream tree found no
`WorkspaceProvider`, `workspace_provider`, or `workspace_access`
equivalent.

A three-way merge assessment auto-merged the implementation and found one textual conflict in
`website/sidebars.ts`. That result is not sufficient proof: upstream changed plugin loading,
Kanban parser/GC code, and adjacent documentation, so the semantic hotspots below still require
manual review. Upstream did not change the Kanban schema in this interval; schema remains listed
because every rebase must reconcile the feature's own canonical DDL, migrations, and rebuild specs.

### Delta Inventory

| Area                                | Current entry points                                                                                                                                                                                                                                                                                                          | Responsibility to preserve                                                                                           |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Contract and registry               | [`agent/workspace_provider.py`](../agent/workspace_provider.py), [`agent/workspace_registry.py`](../agent/workspace_registry.py)                                                                                                                                                                                              | Frozen request/lease values, provider ABC, normalized scoped registry.                                               |
| Plugin registration                 | [`hermes_cli/plugins.py`](../hermes_cli/plugins.py)                                                                                                                                                                                                                                                                           | `ctx.register_workspace_provider` through the current scoped-provider registration and unload machinery.             |
| Selection and defaults              | [`hermes_cli/config_defaults.py`](../hermes_cli/config_defaults.py), [`hermes_cli/kanban_workspace_provider.py`](../hermes_cli/kanban_workspace_provider.py)                                                                                                                                                                  | Explicit inert default, profile-scoped lookup, actionable fail-closed errors.                                        |
| Task/run model and schema           | [`hermes_cli/kanban_db.py`](../hermes_cli/kanban_db.py), [`hermes_cli/kanban_db_connect.py`](../hermes_cli/kanban_db_connect.py)                                                                                                                                                                                              | Access validation/default, run binding fields, fresh DDL, additive migration, rebuild specs, deletion/board guards.  |
| Worktree planning and ownership     | [`hermes_cli/kanban_db_workspace.py`](../hermes_cli/kanban_db_workspace.py)                                                                                                                                                                                                                                                   | Mutation-free plan, post-acquire materialization/validation, requested/effective separation, provider-owned cleanup. |
| Dispatch and reconciliation         | [`hermes_cli/kanban_db_dispatch.py`](../hermes_cli/kanban_db_dispatch.py), [`hermes_cli/kanban_workspace_provider.py`](../hermes_cli/kanban_workspace_provider.py)                                                                                                                                                            | Acquire/materialize/spawn ordering, busy state, compensation, renewal, process-safe release, restart retry.          |
| Destructive and portable operations | [`hermes_cli/kanban_ops.py`](../hermes_cli/kanban_ops.py), [`hermes_cli/kanban_transfer.py`](../hermes_cli/kanban_transfer.py)                                                                                                                                                                                                | GC skip and machine-local lease scrubbing.                                                                           |
| CLI surface                         | [`hermes_cli/kanban_parser.py`](../hermes_cli/kanban_parser.py), [`hermes_cli/kanban.py`](../hermes_cli/kanban.py), [`hermes_cli/kanban_output.py`](../hermes_cli/kanban_output.py)                                                                                                                                           | Access input, display, task/run diagnostics.                                                                         |
| Task graph                          | [`hermes_cli/kanban_db_graph.py`](../hermes_cli/kanban_db_graph.py), [`hermes_cli/kanban_decompose.py`](../hermes_cli/kanban_decompose.py)                                                                                                                                                                                    | Child access inheritance/validation and automatic-decomposer input normalization.                                    |
| Agent tool                          | [`tools/kanban_tools.py`](../tools/kanban_tools.py), [`tools/kanban_tools_schemas.py`](../tools/kanban_tools_schemas.py)                                                                                                                                                                                                      | Access schema, create propagation, task/run output.                                                                  |
| Dashboard API                       | [`plugins/kanban/dashboard/plugin_api.py`](../plugins/kanban/dashboard/plugin_api.py)                                                                                                                                                                                                                                         | Create access and conflict response when deletion would orphan a lease.                                              |
| Developer docs                      | [`website/docs/developer-guide/workspace-provider-plugin.md`](../website/docs/developer-guide/workspace-provider-plugin.md), [`website/docs/developer-guide/plugins/index.md`](../website/docs/developer-guide/plugins/index.md), [`website/sidebars.ts`](../website/sidebars.ts)                                             | Public provider contract, implementation responsibilities, discoverability.                                          |
| User docs                           | [`website/docs/user-guide/features/kanban.md`](../website/docs/user-guide/features/kanban.md), [`website/docs/user-guide/features/plugins.md`](../website/docs/user-guide/features/plugins.md), [`website/docs/reference/cli-commands.md`](../website/docs/reference/cli-commands.md)                                         | Opt-in config, access semantics, registration surface, CLI flag reference.                                           |
| Direct tests                        | [`tests/agent/test_workspace_registry.py`](../tests/agent/test_workspace_registry.py), [`tests/hermes_cli/test_plugins_workspace_registration.py`](../tests/hermes_cli/test_plugins_workspace_registration.py), [`tests/hermes_cli/test_kanban_workspace_provider.py`](../tests/hermes_cli/test_kanban_workspace_provider.py) | Contract, scoped registration, real discovery, lifecycle, contention, and compensation evidence.                     |

At the latest assessment, upstream had changed these feature-touched files since the implementation
base: `hermes_cli/config_defaults.py`, `hermes_cli/kanban_db.py`,
`hermes_cli/kanban_ops.py`, `hermes_cli/kanban_parser.py`,
`hermes_cli/plugins.py`, and adjacent website navigation/docs. The config-default overlap is
outside the Kanban block; the Kanban DB overlap is GC retention rather than schema. Treat the exact
overlaps according to their semantics, not merely because the filenames match.

Also inspect rebase-adjacent upstream paths that were not part of the original fork diff:

- `hermes_cli/plugins_loader.py` and
  `tests/hermes_cli/test_plugin_manifest_v2.py`: upstream added plugin-load deadlines and
  ignores registration attempted by abandoned loader threads. The generated workspace registrar must
  receive the same wrapper and cleanup behavior.
- `tests/hermes_cli/test_kanban_gc_retention.py`: upstream added GC retention ordering tests.
  Preserve those guarantees while continuing to skip provider-managed worktrees.

### Conflict-Resolution Rules

- Follow moved symbols and responsibilities rather than recreating old facade shapes or internal
  compatibility shims.
- Keep workspace registration in upstream's current scoped-provider registration table. In
  particular, preserve any upstream plugin-load timeout/abandonment wrappers generated around that
  table.
- Adapt the split between planning and materialization to upstream's current worktree resolver, but
  never move acquisition after worktree mutation.
- Update every schema representation together: canonical DDL, migration columns, rebuild specs,
  row models, output serializers, and transfer scrubbers.
- Audit every new or changed terminal/reclaim path. It must retain lease authority, prove process
  death, and eventually enter idempotent release.
- Preserve upstream GC/retention improvements while keeping provider-managed worktrees out of core
  deletion.
- Do not collapse task requested coordinates and run effective coordinates even if upstream changes
  workspace persistence.
- Resolve documentation/navigation conflicts according to the current sidebar structure; the
  provider contract page must remain discoverable without restoring obsolete ordering. At the
  assessed upstream conflict, keep both `developer-guide/plugins/application-declarations`
  and `developer-guide/workspace-provider-plugin`.
- If upstream adds a similar hook, compare blocking behavior, transaction boundaries, profile scope,
  persistence, restart handling, and death-before-release semantics before replacing this feature.

### Upstream Equivalence Checklist

- [ ] Selection is explicit, registration alone is inert, and no selection preserves legacy
      behavior.
- [ ] Provider discovery/registration is real, profile-scoped, and A→B→A safe on shared boards.
- [ ] Missing/unavailable selection fails closed with an actionable error.
- [ ] `read`/`write` intent is validated and propagated across CLI, tool,
      dashboard, decomposition, task context, and output.
- [ ] Claim/run creation precedes acquisition; acquisition precedes Git mutation and spawn.
- [ ] No provider callback runs while a Kanban SQLite write transaction is open.
- [ ] Busy defers without spawn, Git mutation, failure budget, last-failure state, or cooldown.
- [ ] Invalid acquisition and post-acquire failures compensate with the correct outcome.
- [ ] Persisted provider registration tier/scope/lease/request/base/effective data survives config
      changes, registry shadowing/unload, and restart.
- [ ] Renewal occurs from dispatcher reconciliation and treats false, exceptions, and missing
      providers as ownership loss.
- [ ] A worker that survives termination keeps its claim; no successor starts beside it.
- [ ] Completion, block, request-review, request-changes, schedule/park, archive, descendant
      invalidation, dangling-run recovery, crash, timeout, stale reclaim, orphan reconciliation,
      spawn failure, renewal loss, and restart all release only after process death.
- [ ] Failed release remains pending and retries idempotently.
- [ ] Core cleanup, GC, hard delete, and board removal cannot orphan provider checkout/lease
      authority.
- [ ] Fresh databases, old-database migration, and rebuild paths agree on every persisted field.
- [ ] Board transfer strips machine-local lease authority without altering the live source board.
- [ ] Direct tests and every `missing:*` item below are reconciled.

### Retirement Criteria

The fork implementation may be retired only when upstream provides the complete contract above or
the external workspace provider no longer depends on it. Matching class/config names are not enough.
Retirement must:

- compare every invariant and terminal path;
- migrate or safely drain databases with active/unreleased fork leases;
- preserve compatibility for installed external providers or provide an explicit migration;
- remove duplicate fork behavior across every delta-inventory entry; and
- pass the direct tests plus tests that close the required-coverage backlog.

If upstream covers only part of the lifecycle, mark the spec **Partially upstreamed** and retain a
smaller, explicitly described delta.

## Test Coverage

### Direct Coverage

- Frozen request/lease values, case-normalized lookup, and scoped-provider precedence:
  `tests/agent/test_workspace_registry.py:test_request_lease_and_registry_preserve_profile_scoped_provider_identity`
- Real plugin registration in two home scopes and scope-local unload:
  `tests/hermes_cli/test_plugins_workspace_registration.py:test_plugin_registrar_normalizes_scopes_and_unload_restores_each_slot`
- Real discovery, successful acquire/path handoff, access persistence, requested-path preservation,
  dispatcher renewal, persisted home-scope routing while another home is ambient, delayed release
  after completion, hard-delete guard, and host-local renewal-loss requeue/release:
  `tests/hermes_cli/test_kanban_workspace_provider.py:test_provider_gates_spawn_persists_access_and_releases_only_after_run_exit`
- Busy deferral without spawn/failure/worktree creation, invalid override compensation, and
  immediate release after spawn exception:
  `tests/hermes_cli/test_kanban_workspace_provider.py:test_busy_defers_without_failure_and_spawn_error_releases_acquired_lease`

### Required Coverage Backlog

- No-provider worktree dispatch is behaviorally identical after the plan/materialize split:
  `missing:no-provider-worktree-compatibility`
- Selected providers are never called for scratch or dir workspaces:
  `missing:non-worktree-provider-inert`
- Missing and unavailable configured providers fail closed before Git mutation/spawn:
  `missing:missing-unavailable-provider-fails-closed`
- Acquire, renew, and release callbacks are proven to execute outside write transactions:
  `missing:callbacks-outside-write-transactions`
- Home, secret, and terminal runtime scopes are all bound to the acquiring profile during callbacks:
  `missing:callback-profile-runtime-scope`
- On the successful path, acquisition and durable binding precede both worktree mutation and the
  spawn callback:
  `missing:successful-acquire-precedes-materialize-and-spawn`
- A provider-name config switch mid-run cannot redirect renew or release:
  `missing:config-switch-preserves-provider-binding`
- Scoped/global shadowing or unload cannot redirect a lease to another same-name registration slot:
  `missing:provider-resolution-tier-binding`
- Acquisition and core materialization use one immutable base ref/SHA:
  `missing:workspace-request-pins-base-ref`
- Lease return type, non-empty ID, absolute/core-target path, alternate Git checkout, and branch
  matching are covered:
  `missing:workspace-lease-validation`
- Conditional binding plus the `workspace_acquired` event are atomic, and a persistence
  failure compensates exactly once:
  `missing:acquire-persistence-compensation`
- Busy deferral restores both ready and review lanes without changing last-failure or cooldown state:
  `missing:busy-restores-source-lane-without-failure-state`
- Renewal exceptions/missing providers count as loss, while a later false availability probe does
  not prevent callback delivery to a still-registered captured provider:
  `missing:renewal-provider-failure-semantics`
- A workspace provider that attempts late registration after its plugin load timed out is ignored,
  and every registration completed before timeout is cleaned up:
  `missing:workspace-provider-load-timeout-abandonment`
- A worker that survives termination after renewal loss retains its claim and blocks a successor:
  `missing:renewal-loss-survivor-holds-claim`
- A non-local or otherwise unsignalable worker with a lost lease stays claimed until its owning
  supervisor proves death:
  `missing:nonlocal-renewal-loss-holds-claim`
- Failure to persist a PID after successful spawn terminates/fences the process before release:
  `missing:spawned-pid-persistence-failure-is-fenced`
- Provider release failure remains pending and succeeds on a later tick/restart:
  `missing:release-failure-retries`
- Completion, block, request-review, request-changes, schedule/park, archive, descendant
  invalidation, crash, timeout, stale-claim, orphan, and restart paths each prove death before release:
  `missing:terminal-paths-release-after-death`
- Unblock/review-reopen dangling-run recovery retains process evidence until death is proven:
  `missing:dangling-run-recovery-proves-death`
- Completion/deferred-parent cleanup, GC, and dashboard HTTP 409 all preserve provider-owned
  checkout or pending release authority:
  `missing:provider-cleanup-and-delete-guards`
- A provider-bound run followed by a provider-disabled run leaves the new core-owned worktree
  eligible for normal safe cleanup:
  `missing:provider-to-core-cleanup-ownership`
- Pending lease rows block board removal, and board transfer scrubs only snapshot authority:
  `missing:board-removal-transfer-authority`
- Fresh, migrated, and rebuilt databases preserve all task/run fields and conservative defaults:
  `missing:workspace-lease-schema-migration`
- A decomposed child cannot retain or select `read` access for a scratch/dir workspace:
  `missing:decomposed-non-worktree-read-rejected`
- CLI, agent tool, dashboard API, output, and valid decomposed-child access propagation agree:
  `missing:workspace-access-surface-propagation`

### Verification Commands

```bash
scripts/run_tests.sh \
  tests/agent/test_workspace_registry.py \
  tests/hermes_cli/test_plugins_workspace_registration.py \
  tests/hermes_cli/test_kanban_workspace_provider.py
python3 website/scripts/check_doc_links.py
git diff --check HEAD
```

After rebasing onto an upstream that contains the assessed plugin-loader and GC-retention changes,
also run:

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_plugin_manifest_v2.py \
  tests/hermes_cli/test_kanban_gc_retention.py
```

## Test Generation Notes

Use a real temporary Git repository, a temporary `HERMES_HOME`, real plugin discovery, and
a real Kanban database. For profile behavior, use two homes and exercise A→B→A so a same-named
provider in B cannot receive A's callback. Record call order in the provider fixture and assert on
observable task/run/event/process state rather than source layout.

Generate tests around these boundaries:

- no provider, missing provider, unavailable provider, provider exception, and ordinary contention;
- requested core target versus an existing provider-owned checkout, including wrong branch and
  non-Git/wrong-repository paths, detached HEAD, and exact-base pinning;
- failure before persistence, after persistence, during materialization, and during spawn;
- completion with a still-retained PID, release failure/retry, restart, and missing-provider
  recovery;
- renewal false/exception with a dead host-local process, a survivor, and a non-local owner;
- successful spawn followed by PID/fingerprint persistence failure;
- every generic Kanban completion/block/review-handoff/schedule/archive/descendant-invalidation/
  crash/timeout/reclaim path with a provider-bound run, including dangling-run recovery;
- old-schema migration and table rebuild, not only fresh database creation;
- cleanup, GC, deletion, board removal, export/import, and config switch behavior;
- scoped/global provider shadowing, unload, and lease-compatible hot reload;
- provider→no-provider retries on the same task, with ownership/cleanup assessed per run/path; and
- all access-input/output surfaces, including invalid `read` on non-worktree tasks.

Avoid tests that read source text or freeze column/file counts. Assert relationships: persisted
binding reconstructs the same request, no callback overlaps a write transaction, no spawn precedes
acquisition, and no release precedes proven process death.

## History

- 2026-09-22 —
  [#1](https://github.com/dkropachev/hermes/issues/1) /
  [#4](https://github.com/dkropachev/hermes/pull/4) /
  [`fcf48e44`](https://github.com/dkropachev/hermes/commit/fcf48e44dafba0a804e3652f5a0344daa7face3c)
  — initial workspace-provider lifecycle implementation.
- 2026-09-22 —
  [`upstream 4094ab610d`](https://github.com/NousResearch/hermes-agent/commit/4094ab610dc7f8554461e7a9b3ffe5e419220949)
  — no native equivalent found; retain the full fork feature and document current semantic rebase
  hotspots.
