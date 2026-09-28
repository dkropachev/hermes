# Kanban Workspace Provider Lifecycle

## Fork Metadata

- **Status:** Fork-only
- **Tracking:** [dkropachev/hermes#1](https://github.com/dkropachev/hermes/issues/1)
- **First implementation:** [dkropachev/hermes#4](https://github.com/dkropachev/hermes/pull/4),
  squash commit
  [`fcf48e44dafba0a804e3652f5a0344daa7face3c`](https://github.com/dkropachev/hermes/commit/fcf48e44dafba0a804e3652f5a0344daa7face3c)
- **Implementation base:**
  [`NousResearch/hermes-agent@10e7de79a9ba602c5c4c42f81560511e5de8d2b1`](https://github.com/NousResearch/hermes-agent/commit/10e7de79a9ba602c5c4c42f81560511e5de8d2b1)
- **Latest upstream assessment:** 2026-09-28 at
  [`NousResearch/hermes-agent@e408d363393ccb72267e67bcccf4f8954b438cd9`](https://github.com/NousResearch/hermes-agent/commit/e408d363393ccb72267e67bcccf4f8954b438cd9);
  no equivalent blocking workspace-provider contract was present, so the complete fork delta remains
  required.

## Contents

- [Summary](#summary)
- [Conformance And Reference Design](#conformance-and-reference-design)
- [Why This Fork Carries It](#why-this-fork-carries-it)
- [Behavior](#behavior)
- [Entry Points](#entry-points)
- [Subfeatures](#subfeatures)
- [Requirement Status Ledger](#requirement-status-ledger)
- [Persisted State And Compatibility](#persisted-state-and-compatibility)
- [Invariants](#invariants)
- [Known Implementation Gaps](#known-implementation-gaps)
- [Rebase Assessment](#rebase-assessment)
- [Test Coverage](#test-coverage)
- [Test Generation Notes](#test-generation-notes)
- [History](#history)

## Summary

The Kanban workspace-provider lifecycle lets an explicitly selected plugin reserve a repository and
select an effective Git workspace before Hermes mutates a worktree or starts a task worker. Hermes
owns task claims, ordering, process supervision, persisted lease authority, reconciliation, and
release timing. The plugin owns repository admission policy, lease durability, and
effective-coordinate selection. It may allocate an alternate checkout; otherwise Hermes
materializes the planned core target after admission. The provider owns cleanup of every checkout
it manages.

The seam exists so an external provider can implement repository-wide reader/writer coordination
without putting a GitHub-specific lock service or publication policy in Hermes core.

This document specifies the **required contract**. Statements explicitly labeled **Current
implementation** describe the fork code first delivered by `fcf48e44`; they are evidence for rebase
and repair work, not relaxations of the contract.

## Conformance And Reference Design

Observable outcomes, safety proofs, persisted compatibility, and public API compatibility in this
document are normative. Named internal columns, fields, reservation-state labels, compare-and-swap
boundaries, lock placement, and algorithms are a non-exclusive reference design unless a section
explicitly makes a name part of a public or persisted contract. An implementation may use different
internals when it preserves the same invariants, migration and restart behavior, diagnostics and
events, failure visibility, and direct/conformance-test results. The [requirement status
ledger](#requirement-status-ledger) is the stable index; prose and the [delta
inventory](#delta-inventory) provide the contract and current ownership details.

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
- **Acquisition attempt:** A durable, globally unique ID created for one run-owned acquire attempt
  before the callback starts and supplied on every replay or compensation for that attempt.
- **Request revision:** A task-owned, monotonically increasing version of the requested repository,
  path, named branch, and base commit. A run captures one revision and never reconstructs it from
  later task state.
- **Lease:** Immutable provider-issued authority and effective workspace coordinates.
- **Requested coordinates:** The core-planned worktree path and branch derived from the task.
- **Effective coordinates:** The path and named branch used for this run after applying lease-branch
  inheritance.
- **Canonical lease:** The single validated lease value that core persists and supplies to every
  later callback; its path is canonicalized and its branch is always the effective named branch.
- **Provider scope:** The `hermes_home_key()` captured when the lease is acquired.
- **Launch reservation:** Durable run-owned authority for materialization and process launch,
  including a stable launch ID and owning supervisor recorded before spawn.
- **Registry epoch:** A process/registry-instance UUID. A registration generation is exact only
  together with this epoch and its scoped/global slot.
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

The compatibility declaration is an additive public API. To preserve source compatibility for
installed providers, `lease_compatibility_id` is optional and keyword-only:

```python
def register_workspace_provider(
    self, provider, *, lease_compatibility_id=None
): ...

registration = ctx.register_workspace_provider(
    provider, lease_compatibility_id="acme.repo-coordinator/v1"
)
```

The lower-level global registration surface adds the same optional keyword-only argument. Omitting
it remains a valid registration and unload operation and marks the record
**legacy-unversioned**. It does not silently make the provider compatible across reload or restart.

Every strict new acquisition must resolve either a non-empty explicit compatibility ID or a
provider-declared, documented, versioned legacy policy that names the compatibility lineage and
the exact old binding cohort it accepts. Without one, selection fails before Git mutation or spawn
with an actionable setup error telling the operator to update the provider registration. Core never
derives compatibility from Python object identity, package/module version, display name, provider
name, or a numeric generation. An installed provider migrates by adding the keyword with a stable
provider-owned value before accepting new leases; existing source that omits it continues to load,
register, and unload. Any outstanding legacy lease follows the drain, declared-mapping, or
fail-closed quarantine rules in [Persisted State And Compatibility](#persisted-state-and-compatibility).

The compatibility ID is public routing metadata, not a credential or bearer secret, and must be
safe to expose in diagnostics and exports after authority is scrubbed. The registry creates a UUID
epoch when its in-memory registry starts and assigns a monotonically increasing generation to each
exact scoped/global slot within that epoch. `(slot, registry_epoch, generation)` identifies one
live installation only while that epoch exists; a numeric generation alone is never meaningful
after restart. A compatible restart or reload reuses the compatibility ID and thereby promises to
accept every outstanding lease and its persisted request, canonical lease, renewal, and
release-outcome contract until those leases drain. An incompatible implementation must use a new
compatibility ID.

The returned registry record/handle exposes the normalized name, exact scoped/global slot, scope,
compatibility ID or legacy-unversioned marker, registry epoch, and assigned generation; unload
targets that handle.

Every new complete durable binding preserves the normalized name, exact scoped/global slot,
non-empty compatibility ID (explicit or supplied by the declared versioned legacy policy), registry
epoch, and generation that supplied the lease. Unloading a scoped provider must not redirect its
lease to a same-name global fallback, adding a scoped provider must not shadow a lease acquired from
the global fallback, and same-slot replacement/restoration must not redirect callbacks merely
because the lookup coordinates still match. During the same registry epoch a callback may target
the exact epoch/generation. After a process restart, or whenever that exact installation is gone, it
may resolve only a registration in the persisted slot with the same non-empty compatibility ID;
matching a reused numeric generation is insufficient. Otherwise resolution fails closed.

Every callback participates in registration lifetime. Resolution takes a refcounted pin on the
exact or compatibility-matched registration for the complete `is_available()`/`try_acquire()`,
`renew()`, `release()`, or compensation callback. Unload first prevents new resolution, then waits
for those callback references to drain before plugin teardown. Acquisition additionally performs a
post-`try_acquire()` compare-and-swap proving the same slot, epoch, and generation are still current
before binding; a pin prevents destruction, but does not make a replaced slot current. If that proof
loses, Hermes calls `release(..., outcome="acquire_failed")` on the pinned issuing instance and does
not persist or use the lease. The current workspace-provider registration API exposes neither a
compatibility ID nor these pin/exact-installation CAS semantics.

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

Under the required contract, `WorkspaceRequest` is frozen and contains:

| Field                    | Meaning                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------ |
| `task_id`                | Stable Kanban task ID.                                                               |
| `run_id`                 | Current `task_runs.id` opened by the atomic claim.                                   |
| `owner_id`               | Immutable claim-owner identity; public fencing metadata, not a transport credential. |
| `board`                  | Board slug.                                                                          |
| `board_db_path`          | Absolute path to the board database.                                                 |
| `access`                 | `read` or `write` coordination intent.                                               |
| `workspace_kind`         | The task workspace kind; currently always `worktree` on this path.                   |
| `acquisition_attempt_id` | Durable ID that makes one acquisition replay deduplicable.                           |
| `request_revision`       | Task workspace-request revision captured by this run.                                |
| `requested_path`         | Absolute mutation-free core worktree target.                                         |
| `branch_name`            | Planned named branch, including the default `wt/<task-id>` when needed.              |
| `base_commit`            | Immutable full SHA checked out when this task request is first planned.              |
| `project_id`             | Optional linked Hermes Project.                                                      |
| `repo_root`              | Resolved main repository root used for coordination identity.                        |

**Current implementation:** the public dataclass and persisted reconstruction omit
`request_revision` and `base_commit`; reconstruction also joins mutable task state for
`workspace_kind` and `project_id`. The explicit gaps are recorded below.

`WorkspaceLease` is frozen and contains a non-empty opaque `lease_id`, an absolute
`path`, an optional `branch_name`, and an optional `expires_at` timestamp.
The lease ID is persisted and exposed in diagnostics, so it must be a stable identifier rather than a
bearer secret.

`WorkspaceLease.branch_name is None` means “inherit `request.branch_name`”; it never means “any
branch” or detached HEAD. After validating the provider value, core constructs one canonical lease:
the path uses core's deterministic absolute path canonicalization, the branch contains the resolved
effective named branch, and the other validated provider fields retain their contract meaning. Core
persists that canonical value and passes its reconstruction to renew, ordinary release, and every
post-validation compensation callback. Raw provider fields need not be persisted unless a future
provider contract explicitly gives them meaning.

If validation fails before a canonical lease exists, compensation passes the original returned
`WorkspaceLease` to the provider that issued it. A value of another type has no lease contract and
cannot be compensated through `release()`. Thus a provider never sees a mixture of raw and
normalized leases after canonicalization succeeds.

The returned path has two valid shapes under the required contract:

- It may equal `request.requested_path`. The path need not exist yet; Hermes materializes
  the planned worktree from `request.base_commit` on the effective branch only after the lease and
  complete binding are persisted.
- It may point to a provider-owned checkout. In that case it must already be an existing absolute
  directory, must be a Git checkout, and must be attached to the effective branch. Detached HEAD is
  invalid even when it resolves to `request.base_commit`; the provider remains responsible for
  repository identity while core validates the attached branch and persisted requested/effective
  separation.

`base_commit` is the full SHA checked out when a task request revision is initially formed, not a
mutable branch name or repository `HEAD` re-resolved after admission. Planning may resolve the SHA
outside a transaction, but before the first provider callback Hermes must compare-and-swap
`tasks.workspace_base_commit` and the task request revision while matching the original task,
current run, claim token, requested repository/path/branch, and an unset base. A concurrent winner is
re-read and used only if all those coordinates still match. Thus a busy result, provider failure, or
later retry cannot drift to a newer repository `HEAD`.

The revision is the immutable tuple `(repo_root, requested_path, branch_name, base_commit,
revision_number)`. Every claimed run copies that complete revision before its first callback. If the
requested checkout or branch already exists, its `HEAD` must equal the captured `base_commit` at
admission/materialization. An alternate provider checkout must likewise have
`HEAD == request.base_commit` when core admits it. The worker may commit after launch; that expected
advancement does not mutate the captured revision.

An ordinary retry creates or adopts a fresh checkout starting at the same revision base rather than
reusing or force-resetting the prior worker's advanced checkout. A request-changes, review-reopen, or
other lifecycle operation that intentionally continues the prior work must first wait for its run to
be quiescent, then explicitly create the next task revision by advancing the base to the accepted
worker tip, or select a new attempt branch rooted at that tip. It must never silently reset a worker
branch to an older base or discard commits. Retargeting the repository, path, or named branch also
creates a new revision only after old reservation and lease authority drain; historical runs retain
their original revision.

**Current implementation:** `None` already inherits the requested branch, but the returned object is
not replaced with one canonical lease before every callback, materialization uses mutable repository
`HEAD`, and checkout validation neither proves `HEAD == base_commit` nor rejects detached HEAD when
Git reports no branch. These are contract gaps below.

`try_acquire()` must return promptly. It may do bounded work needed to prepare an already
granted checkout, but must return `None` instead of waiting for another task's lifetime. Acquisition
is deduplicated by `request.acquisition_attempt_id`: a provider must treat a replay of the same ID as
idempotent and return the same lease or terminal disposition instead of granting independent
authority. This is separate from transport authentication; the attempt ID is correlation and
deduplication data, not a bearer secret.
`renew()` returns `False` when ownership is lost. `release()` must be
idempotent because reconciliation retries it after crashes and restarts.

Core enforces a configured finite deadline around every availability, acquire, renew, release, and
compensation callback; a provider promise to return promptly is not the scheduler's only defense.
Callbacks wait outside the board write and lifecycle locks and run in isolated capacity so one hung
provider or lease cannot consume the workers needed to renew unrelated active leases. A timeout is
fail-closed: availability/acquire becomes an actionable acquisition failure, renewal becomes lost
ownership, and release remains pending for retry. The timed-out invocation retains its registration
reference and per-lease in-flight marker until it really exits, so core does not overlap retries of
the same callback or unload code underneath it. Reconciliation renews all possibly live leases
before starting potentially slow releases for already quiescent runs, and release capacity cannot
starve that renewal pass.

An acquire deadline does not revoke an invocation or provider authority. Its durable attempt and
pre-binding reservation remain an admission and board-lifecycle blocker until the invocation exits.
If it returns a lease late, that lease is never usable for launch: core revalidates the original
run/claim/reservation and board tombstone, then records and completes idempotent
`outcome="acquire_failed"` compensation through the still-pinned issuing registration. Until that
compensation succeeds, the returned lease remains durable pending authority; timeout handling may
not forget it, admit a Hermes successor, or let board removal erase the attempt.

Provider callbacks must never execute while a Kanban SQLite write transaction is open.

### Access Semantics

`workspace_access` accepts normalized `read` and `write` and defaults conservatively to
`write`. `read` is valid only for worktree tasks. Low-level task graph insertion must normalize and
validate an optional child override, validate `workspace_kind`, and enforce the same cross-field
invariant as ordinary task creation. The automatic LLM decomposer normally supplies neither
workspace kind nor access, so those children inherit the root's values.

**Current implementation:** decomposed-child insertion accepts an arbitrary workspace kind, does not
trim or lowercase access, and checks only the raw access enum. It therefore misses the cross-field
`read`/non-worktree rejection.

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
  -> build mutation-free WorktreePlan and resolve a candidate full base SHA
  -> conditionally initialize/adopt the task request revision and copy its complete request to run
  -> durably reserve one acquisition attempt ID for that run/claim/revision
  -> resolve and pin the selected registration in the acquiring profile
  -> bounded is_available outside a Kanban write transaction
  -> try_acquire outside a Kanban write transaction
  -> validate and canonicalize the lease
  -> prove that the acquired registration slot/epoch/generation is still current
  -> conditionally persist the exact binding, immutable request revision, acquisition attempt,
     canonical lease, requested/effective coordinates, and initial materialization reservation
  -> materialize or validate the checkout under that durable reservation
  -> persist a launch reservation, stable launch ID, and owning supervisor identity
  -> recheck that exact reservation, then spawn in the effective path
  -> conditionally attach PID and restart-safe process fingerprint to that launch/run/claim
```

The plan may inspect Git to resolve the repository and branch, but no worktree creation or other
filesystem mutation may occur before acquisition succeeds. Initializing an unset task base and
request revision is a conditional metadata write, not workspace materialization. It occurs before
the first provider callback and is never rolled back for busy or failure, which makes later attempts
use the same admitted base.

Lease persistence, the initial side-effect reservation, and the `workspace_acquired` event are one
Kanban write transaction. That transaction itself conditionally matches the same task's
`current_run_id`, task and run claim tokens, captured request revision, expected dispatch
lane/status, unended run, planned reservation, and empty binding. There is no unconditional binding
followed by a separate active-run check. If the condition loses, acquisition fails and Hermes
compensates through the pinned issuing provider without materializing a checkout.

Every pre-binding disposition after a callback returns is fenced the same way. Busy restoration,
provider/type/validation failure, compensation bookkeeping, and callback-timeout handling
compare-and-swap the original task ID, run ID, claim tokens, request revision, acquisition attempt,
and reservation owner/epoch. A stale callback may settle or compensate only its original run; it
cannot clear a successor's claim, move the successor's lane, overwrite its failure state, or append
an event to it. A deadline keeps the attempt durable and callback pinned. A late lease return is
persisted as compensation-pending authority, revalidated against the board tombstone, and released
idempotently through its issuing registration before the attempt stops blocking admission or board
removal.

A check at binding time alone cannot protect external side effects. The run therefore owns a
durable materialization/launch reservation with explicit phase and cancellation/terminal-request
state. Before each external mutation, its executor conditionally enters the next phase and re-reads
the exact run/claim/reservation immediately before acting. Every completion, reclaim, reassign,
recovery, and operator lifecycle writer must honor the reservation. Such a writer may record a
cancellation or terminal request and select the release outcome, but it must not erase the binding,
claim, reservation, process evidence, or release authority, and it must not admit a successor while
the side effect is in flight or its result is unknown. The executor or reconciler then settles the
reservation and applies the pending lifecycle transition.

Before invoking spawn, Hermes durably records a stable launch ID, the responsible process
supervisor/launcher, and the reserved run/claim. Spawn must go through a supervisor that can recover
whether that launch exists independently of the later PID attachment. A crash after process creation
but before PID/fingerprint persistence therefore leaves an unknown, possibly live launch, not a
dead worker. Reconciliation must ask the recorded supervisor to locate and authoritatively stop that
launch or prove it never started/exited before clearing its claim or releasing its lease. Likewise,
if the run/claim/reservation is lost after spawn may have occurred, core must authoritatively stop
the exact process and prove death before release. A null PID is never itself proof that no child
exists. Provider publication fencing is a separate defense at the publish boundary, not a substitute
for process supervision.

Provider-managed effective paths and branches live on the run only. Hermes must not overwrite the
task's requested repository/path/branch with an ephemeral checkout. A retry or later review must
start again from the original task request after the provider removes the old checkout.

#### Canonical Reservation State Machine

The reference design persists reservation state on the run. Its transitions are the complete
forward graph for that design; a conforming alternative may encode them differently, but recovery
must not skip the same proof obligations or move authority backward:

| State                           | Meaning and only legal next states                                                                                                                                                                                                                                         | Crash/takeover action                                                                                                |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `planned`                       | Claim, complete request revision, and acquisition attempt ID are durable; no returned lease is known. Next: `acquired-materialize-reserved`, compensation-pending authority for a late result, or `quiescent` only after the callback has exited and no authority remains. | Reconcile/deduplicate only this attempt, or settle its recorded terminal intent after callback exit.                 |
| `acquired-materialize-reserved` | Canonical lease/binding exists; no filesystem mutation has begun. Next: `materializing` or `quiescent`.                                                                                                                                                                    | Honor cancellation before entering mutation; otherwise CAS into `materializing`.                                     |
| `materializing`                 | A tagged checkout mutation may be in flight. Next: `materialized`, or `quiescent` only after inspecting/undoing the exact mutation.                                                                                                                                        | Reconcile the tagged target; never blindly create or delete by path alone.                                           |
| `materialized`                  | Effective checkout was validated at the captured base and branch. Next: `launch-reserved` or `quiescent`.                                                                                                                                                                  | Revalidate ownership and coordinates before reserving launch.                                                        |
| `launch-reserved`               | Stable launch ID and supervisor are durable; spawn has not been invoked. Next: `launch-unknown` or proven `quiescent`.                                                                                                                                                     | Ask that supervisor to prove non-start before cancelling.                                                            |
| `launch-unknown`                | Spawn may have created a process, but identity attachment is incomplete. Next: `running` or proven `quiescent`.                                                                                                                                                            | Resolve the stable launch; attach the exact fingerprint or stop it and prove death.                                  |
| `running`                       | PID/fingerprint is attached to the stable launch. Next: proven `quiescent`.                                                                                                                                                                                                | Route stop/liveness through the recorded supervisor.                                                                 |
| `quiescent`                     | Core has authoritative proof that no workspace mutation or worker can still execute. Next: `release-pending` when a lease exists, otherwise `released`.                                                                                                                    | Select the already recorded outcome and schedule release; this state alone does not mean provider release succeeded. |
| `release-pending`               | The write-once outcome and release authority remain durable. Next: `released` only after callback success.                                                                                                                                                                 | Retry the same idempotent release under a compatible pinned registration.                                            |
| `released`                      | No core executor or provider lease authority remains. Terminal.                                                                                                                                                                                                            | None.                                                                                                                |

Each external-action transition is a CAS over task/run/claim identity, request revision, current
state, `reservation_executor_owner`, and a monotonically increasing reservation executor epoch. An
executor records a finite ownership deadline. After that deadline a reconciler may atomically take
over by changing the owner and incrementing the epoch; every completion from the old executor then
loses its CAS and can only run the phase-specific compensation path. Registry epoch and reservation
executor epoch are unrelated values.

Cancellation or terminal intent is monotonic and takes precedence over every not-yet-entered forward
action: a phase-entry CAS requires no such intent. The transaction that first accepts terminal
intent also selects `workspace_release_outcome` when a lease exists. That outcome is write-once;
later cancellation, lease-loss, or operator requests may add audit events but cannot replace it.
`task_runs.ended_at` means only that the logical attempt has an outcome and will do no new forward
work. It is neither a reservation state nor evidence of quiescence, process death, or release.

Task admission queries all historical runs, not only `tasks.current_run_id`. Any earlier
provider-selected run whose reservation has not reached `quiescent`, or whose lease has not reached
`released`, is a task-level admission blocker. Therefore clearing a historical run claim cannot by
itself fence a successor beside an unknown launch or pending lease.

### Contention And Acquisition Failures

| Condition                                                            | Required behavior                                                                                                                                                                                                                                                                                                                |
| -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Provider returns `None`                                              | Through the original-run CAS, end the claimed run as `workspace_deferred`, restore its source lane, clear the claim, settle its reservation, emit a deferral event/result, and retry on a later tick. Do not materialize, spawn, increment `consecutive_failures`, set `last_failure_error`, or trigger infrastructure cooldown. |
| Provider is missing, unavailable, or raises before returning a lease | Fail closed through the normal actionable workspace/spawn-failure path. Never start an unprotected worker.                                                                                                                                                                                                                       |
| Provider returns a value that is not `WorkspaceLease`                | Fail the run with an actionable type error. Core has no valid lease contract to compensate.                                                                                                                                                                                                                                      |
| A returned `WorkspaceLease` fails validation                         | Pass the original provider lease to `release(..., outcome="acquire_failed")`, then fail the run.                                                                                                                                                                                                                                 |
| Persistence fails after canonicalization                             | Pass the canonical lease to the same `acquire_failed` compensation outside the write transaction. If compensation fails before authority was persisted, provider TTL/recovery is the final backstop.                                                                                                                             |
| Registration pin/CAS is lost before binding                          | Compensate through the pinned issuing provider instance and do not materialize or bind the lease.                                                                                                                                                                                                                                |
| Any core callback deadline expires                                   | Apply the fail-closed timeout semantics without holding the board lock; retain the durable attempt, pin, and in-flight marker until invocation exit. A late acquisition result becomes persisted compensation-pending authority and is idempotently released through the pinned issuer before admission/removal unblocks.        |
| Checkout materialization/validation fails after persistence          | Persist `workspace_release_outcome="workspace_failed"`, settle the materialization reservation, end the run through existing failure accounting, and release when no side effect or process can still execute. A failed release remains pending for dispatcher retry.                                                            |
| Spawn reports failure                                                | Persist `workspace_release_outcome="spawn_failed"`. Release immediately only when the recorded supervisor proves that the stable launch never produced a live process; otherwise retain the reservation, claim, process authority, and lease until the supervisor authoritatively stops the launch and proves death.             |

Only contention has special no-failure semantics. Other errors retain the existing Kanban failure
breaker and infrastructure-deferral rules for their failure class. Every disposition is conditional
on the original run/reservation identity; losing that CAS never authorizes mutation of whichever run
is now current.

### Renewal And Lost Ownership

The dispatcher renews every persisted, unreleased lease whose worker or launch may still execute
during reconciliation. This includes an ended run that retains PID/fingerprint evidence, an
unsettled launch reservation, an unknown launch result, or any other unproven process death. Renewal
must not depend only on worker hooks, task status, `current_run_id`, `ended_at`, or heartbeats,
because a worker can stall, outlive its run, cease to be the task's current run, or survive a Hermes
restart while the external lease still needs supervision.

Each tick schedules those active renewals before release callbacks for finished runs. Both classes
have core deadlines and independent execution capacity, so a hung release cannot delay the next
renewal window for another lease.

Renewal uses the provider identity, request coordinates, and lease fields captured on the run. The
current `kanban.workspace_provider` value is irrelevant to an existing lease. Availability
is checked on initial acquisition; renew and release still call a registered captured provider when
its availability probe later changes. Resolution must target the exact persisted registry
slot/epoch/generation while that installation remains live, or a registration in that slot carrying
the same persisted compatibility ID; a normal live lookup by name and scope or a generation number
from another registry epoch is insufficient.

**Current implementation:** renewal selects only rows with `ended_at IS NULL` and reconstructs the
provider from name and home scope. It therefore stops renewing an ended-but-potentially-live worker
and can redirect callbacks across scoped/global shadowing or same-slot hot reload. Its subsequent
lease-loss handler joins only the task's current unended run, so merely widening renewal selection
would still drop an ended or superseded live run instead of routing it to that run's supervisor.

`False` from `renew()`, a missing captured provider, or a renewal exception means
ownership is lost. The required fail-closed sequence is:

1. route termination by persisted run/launch identity to the owning supervisor, whether the run is
   current and unended or already ended/superseded, so it can authoritatively identify the worker;
2. retain that run's claim/reservation and lease authority while execution is alive or cannot be
   proven stopped, retrying termination/reconciliation without spawning a successor;
3. once execution is proven stopped, end and requeue a still-active run as
   `workspace_lease_lost` without spending the task failure budget; for an already ended run, record
   lease loss against that run without rewriting an unrelated current run; and
4. select `workspace_release_outcome="workspace_lease_lost"` only if it is still unset, otherwise
   preserve the earlier write-once terminal outcome, then call idempotent release with the persisted
   value.

For a verified host-local worker, the current termination helper can perform this sequence. A
non-local claim or unprovable process identity must not be treated as proof of death merely because
the current dispatcher cannot signal it. This ordering prevents **Hermes** from starting its own
retry while termination remains unproved; it cannot prevent a provider from admitting some other
successor after the expired lease. Publication fencing must remain effective throughout expiry
detection, termination, and authoritative death proof so the expired worker cannot publish beside a
provider-admitted successor.

### Terminal Paths, Restart, And Release

Normal completion and every abnormal, recovery, reclaim, or handoff path that closes a run must
preserve release authority until process death is proven. Relevant paths include completion, block,
request-review, request-changes, schedule/park, archive, descendant invalidation after a parent
reopens, crash, timeout, automatic stale-claim reclaim, explicit/manual reclaim, reassign with
reclaim, ordinary ready-task dangling-run recovery, unblock/review-reopen dangling-run recovery,
orphan reconciliation, spawn failure, renewal loss, and restart reconciliation.

When a bound run first becomes terminal or workspace setup/launch fails, the same conditional
lifecycle transition selects a non-null `workspace_release_outcome` if one has not already been
selected. That value is durable and write-once for the lease: both an immediate first release and
every retry/restart callback read it from the binding, rather than deriving it again from a later run
state or accepting a different call-site override. Outcomes such as `completed`,
`workspace_failed`, `spawn_failed`, and `workspace_lease_lost` are advisory, open-ended strings;
providers must not treat that example set as an exhaustive enum.

Ending a run does not by itself release the lease. The ended run retains its launch reservation,
stable launch/supervisor identity, and any worker PID/fingerprint. The dispatcher terminal reaper
first proves that the launch never produced a process, or that the exact process exited or was
authoritatively stopped, and only then marks the launch quiescent and clears process authority. Only
that proof may make a run release-eligible; a missing PID caused by a crash, failed persistence,
race, recovery shortcut, or non-local process is not proof of death.

The current ended-run sweep releases rows where:

- `ended_at IS NOT NULL`;
- `worker_pid IS NULL`;
- `workspace_provider IS NOT NULL`; and
- `workspace_lease_released_at IS NULL`.

That query satisfies the required contract only when every path that clears or fails to attach the
PID has already established authoritative death and no durable launch reservation remains unknown.
The known launch, dangling-recovery, and reclaim gaps below violate that precondition.

**Current implementation:** `release_workspace_lease()` uses an explicit call-site outcome when
provided and otherwise falls back to `task_runs.outcome`. For example, a materialization failure may
first attempt `workspace_failed`, while a later retry derives `spawn_failed` from the run. The row
has no durable workspace-specific release outcome, so idempotent retries can report a different
outcome for the same lease.

Core marks `workspace_lease_released_at` and emits `workspace_released` only after
the provider call succeeds. A release error leaves the row and its selected release outcome pending
and is retried on future dispatcher ticks or after restart. This is why provider release must be
idempotent.

If the exact captured generation (or a registration with the persisted compatibility ID) is missing after
reload or restart, Hermes does not redirect the callback to the currently configured or merely
same-named provider and does not pretend the lease was released. It fails closed, retains the
pending row, and relies on provider TTL plus provider-owned publication fencing until a compatible
registration is available.
Unloading a plugin must not blanket-release live leases.

### Checkout Ownership And Destructive Operations

For a provider-managed run, provider release owns cleanup of its effective checkout:

- core completion, deferred-parent worktree cleanup, and Kanban garbage collection must not remove
  that checkout; and
- the provider must clean or quarantine its checkout without keeping the repository locked forever.

A later provider-disabled run can create a new core-managed worktree for the same task. That later
path is core-owned and must remain eligible for normal safe cleanup; ownership must follow the
run/path rather than the fact that some historical run once used a provider.

A pending lease or unresolved acquisition attempt blocks hard task deletion so the row holding
authority cannot disappear.
The dashboard reports this as HTTP 409. A board with any in-flight acquisition attempt or pending
provider lease cannot be removed. Archiving the task remains possible, but release is reconciled
only after the worker is gone. The board's pending-authority check and archive/delete operation must
be serialized against acquisition.

That serialization uses a stable board-lifecycle reservation outside the directory that can be
moved or deleted. Dispatcher reserves the lifecycle before starting or resuming a pre-binding
acquisition; removal and import acquire the same reservation before opening or publishing the board
database. Under it they reject a tombstoned slug and revalidate immutable board identity and the
resolved database path before acting. Removal retains the reservation across the pending-authority
query, tombstone, cache invalidation, and atomic move/delete. An acquisition callback that finishes
after cancellation/tombstoning revalidates the tombstone and may only durably compensate its late
lease through the pinned issuer; it cannot publish a binding or recreate the board. A waiter holding
an old path may not recreate an empty board after removal. The concrete lock/CAS design is
replaceable under the conformance rule above. In the reference design, the inner dispatch-tick lock
is derived from the `main` file returned by `PRAGMA database_list` on the already-open connection,
not from an ambient board argument, so it cannot accidentally serialize a different database.

Board exports and imports are portable snapshots, not transfers of live lease authority. Export
first snapshots the live DB, then sanitizes that private copy. The sanitizer clears task workspace
paths and every current or future run authority field: provider slot/name/scope, compatibility ID,
registry epoch/generation, binding version/completeness, acquisition-attempt and request-revision
identity, requested/effective coordinates, lease data, reservation/executor state,
launch/supervisor/process identity, terminal intent, release outcome, and released timestamp. It
also removes or rewrites workspace lifecycle events whose payload contains those values.
Sanitization is schema-versioned and fails closed on an unclassified authority column; it never
updates the live source board, whose dispatcher remains responsible for release.

By default an import treats repository and path identity as relocated: it clears the task's
workspace path, branch/base tuple, and request revision, parks dispatchable dir/worktree tasks, and
requires an explicit retarget before a later claim resolves a fresh revision. An importer may retain
a logical branch/base revision only when it verifies and records an explicit mapping to the same
repository identity; it never assumes that a full SHA alone makes paths or repositories portable.

An archive is untrusted SQLite input. Import opens it read-only only to validate and copy allowlisted
portable table/column values into a separately created, current canonical DB; it does not migrate,
update, or publish the archive database, import its schema objects, or execute its triggers. Runtime
and workspace sanitization happens while that fresh DB is still in a private staging directory.
Under the stable lifecycle lock, import atomically reserves one slug, rechecks its tombstone/board
identity state, and publishes the complete staged board directory with one atomic rename. Failure
removes the private staging tree and reservation and cannot leave a discoverable partial board.

### Provider Responsibilities

A production provider should supply:

- durable reader/writer admission with writer fairness;
- renew/release checked against the lease ID and immutable request owner identity; transport
  authentication and authorization are separate provider concerns;
- TTL and startup recovery for a host that stays down;
- a private checkout for every lease, belonging to `request.repo_root` and the effective branch;
- checkout cleanup with quarantine and retry on failure; and
- publication fencing so an expired writer cannot push after a successor starts.

Provider TTL should exceed several dispatcher intervals. Neither Git worktree locking nor this
cooperative seam protects against actors that bypass the provider. Core verifies that an alternate
path is some Git checkout and, under the required contract, requires it to be attached to the
effective named branch; the trusted provider remains responsible for proving repository identity.

**Current implementation:** branch validation is conditional on Git reporting a branch and the
request lacks an immutable base SHA. A provider that requires exact-base admission must therefore
return its own checkout pinned during acquisition, but even that workaround does not satisfy the
required core validation until the detached-HEAD and base-identity gaps below are resolved.

### User And Integration Surfaces

- Config: `kanban.workspace_provider`.
- CLI: `hermes kanban create --workspace-access read|write`.
- Agent tool: `kanban_create.workspace_access` with the same enum and default.
- Dashboard create API: `workspace_access`.
- Task graph: low-level child override or inherited root access; automatic decomposition currently
  uses inheritance.
- CLI, tool, and run output: requested access plus provider lease diagnostics.
- Plugin API: `ctx.register_workspace_provider(provider, lease_compatibility_id=...)`, returning the
  exact registration record/handle.

Existing task rows migrate to `write`. Historical run rows are classified as unbound, already
released, or legacy-unreleased; they do not all lack provider bindings. The compatibility rules
below govern each cohort without inventing authority.

## Entry Points

- Public provider types: `WorkspaceProvider`, `WorkspaceRequest`, and `WorkspaceLease` in
  [`agent/workspace_provider.py`](../agent/workspace_provider.py).
- Plugin registration API: `ctx.register_workspace_provider(...)`, documented in
  [`workspace-provider-plugin.md`](../website/docs/developer-guide/workspace-provider-plugin.md).
- Config: `kanban.workspace_provider`, documented in
  [`kanban.md`](../website/docs/user-guide/features/kanban.md).
- CLI: `hermes kanban create --workspace-access read|write`, documented in
  [`cli-commands.md`](../website/docs/reference/cli-commands.md).
- Agent tool: `kanban_create.workspace_access`; dashboard create API: `workspace_access`.
- CLI/tool/run output: requested access and provider-lease diagnostics.

The current fork's
[`plugin-workspace-leases` page](https://github.com/dkropachev/hermes/blob/8e9fc477c9e0e99bceee7a3797ab764d17a0498b/website/docs/developer-guide/plugin-workspace-leases.md)
is an adjacent surface, not an implementation of this feature. It documents the distinct
`ctx.workspaces`/`ctx.workspace_tools` host lifecycle APIs and must keep its explicit cross-link and
boundary from the Kanban provider contract.

## Subfeatures

### Contract, Discovery, And Profile Scope

The canonical requirements are [req:selection-scope](#req-selection-scope) and
[req:registration-lifetime](#req-registration-lifetime). Their implementation boundary spans
[delta:contract-registry](#delta-contract-registry),
[delta:plugin-registration](#delta-plugin-registration), and
[delta:selection-defaults](#delta-selection-defaults); review those rows together whenever provider
discovery, registration, or unload moves.

### Worktree Admission And Dispatch

[req:request-base](#req-request-base), [req:canonical-lease](#req-canonical-lease),
[req:ordering-reservations](#req-ordering-reservations), and
[req:contention-failure](#req-contention-failure) meet across
[delta:task-run-schema](#delta-task-run-schema),
[delta:worktree-planning](#delta-worktree-planning),
[delta:dispatch-reconciliation](#delta-dispatch-reconciliation), and
[delta:process-supervision](#delta-process-supervision). The shared review boundary is the handoff
from an immutable request through binding, materialization, and launch.

### Reconciliation And Release

[req:process-renewal-release](#req-process-renewal-release) and
[req:cleanup-destructive](#req-cleanup-destructive) share ownership across
[delta:dispatch-reconciliation](#delta-dispatch-reconciliation),
[delta:process-supervision](#delta-process-supervision), and
[delta:destructive-transfer](#delta-destructive-transfer). Review their relationship whenever a
terminal, reclaim, cleanup, or board-lifecycle path changes.

### Access And User Surfaces

[req:access-surfaces](#req-access-surfaces) and
[req:migration-transfer](#req-migration-transfer) connect
[delta:cli-surface](#delta-cli-surface), [delta:task-graph](#delta-task-graph),
[delta:agent-tool](#delta-agent-tool), [delta:dashboard-api](#delta-dashboard-api),
[delta:developer-docs](#delta-developer-docs), and [delta:user-docs](#delta-user-docs).
The relationship to preserve is agreement between stored intent, public input/output, and portable
data.

## Requirement Status Ledger

The implementation column records defects independently from direct-test coverage. `Conforming`
therefore does not claim that every desired direct test already exists.

| Requirement                                                            | Normative contract/reference                                                                                                                                             | Implementation status/evidence                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Upstream disposition/evidence                                                                                                                   | Coverage (direct/missing)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| <a id="req-selection-scope"></a> `req:selection-scope`                 | [Applicability And Selection](#applicability-and-selection)                                                                                                              | Conforming — [delta:selection-defaults](#delta-selection-defaults) supplies opt-in and fail-closed routing, while [delta:plugin-registration](#delta-plugin-registration) supplies scoped discovery.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Retain — [the assessed upstream](#latest-upstream-assessment) has no blocking provider-selection equivalent.                                    | `tests/hermes_cli/test_plugins_workspace_registration.py:test_plugin_registrar_normalizes_scopes_and_unload_restores_each_slot`; <a id="missing-no-provider-worktree-compatibility"></a> `missing:no-provider-worktree-compatibility`; <a id="missing-non-worktree-provider-inert"></a> `missing:non-worktree-provider-inert`; <a id="missing-missing-unavailable-provider-fails-closed"></a> `missing:missing-unavailable-provider-fails-closed`; <a id="missing-callback-profile-runtime-scope"></a> `missing:callback-profile-runtime-scope`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| <a id="req-registration-lifetime"></a> `req:registration-lifetime`     | [Applicability And Selection](#applicability-and-selection), [Provider Contract](#provider-contract)                                                                     | <a id="gap-provider-resolution-tier-binding"></a> `gap:provider-resolution-tier-binding` — [delta:contract-registry](#delta-contract-registry), [delta:plugin-registration](#delta-plugin-registration), and [delta:task-run-schema](#delta-task-run-schema) do not persist a compatibility-qualified exact registration; <a id="gap-provider-acquisition-generation-race"></a> `gap:provider-acquisition-generation-race` — [delta:contract-registry](#delta-contract-registry) and [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) do not pin/CAS the issuer through binding; <a id="gap-provider-callback-registration-pinning"></a> `gap:provider-callback-registration-pinning` — [delta:contract-registry](#delta-contract-registry) and [delta:plugin-registration](#delta-plugin-registration) do not drain callback references before unload.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Retain — [the assessed upstream](#latest-upstream-assessment) has adjacent registry machinery but no durable lease-compatible callback binding. | <a id="missing-provider-resolution-tier-binding"></a> `missing:provider-resolution-tier-binding`; <a id="missing-provider-acquisition-generation-race"></a> `missing:provider-acquisition-generation-race`; <a id="missing-provider-callback-registration-pinning"></a> `missing:provider-callback-registration-pinning`; <a id="missing-config-switch-preserves-provider-binding"></a> `missing:config-switch-preserves-provider-binding`; <a id="missing-workspace-provider-load-timeout-abandonment"></a> `missing:workspace-provider-load-timeout-abandonment`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| <a id="req-request-base"></a> `req:request-base`                       | [Provider Contract](#provider-contract), [Persisted State And Compatibility](#persisted-state-and-compatibility)                                                         | <a id="gap-workspace-request-pins-base-ref"></a> `gap:workspace-request-pins-base-ref` — [delta:worktree-planning](#delta-worktree-planning), [delta:task-run-schema](#delta-task-run-schema), and [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) do not pin one revision base; <a id="gap-run-persists-complete-workspace-request"></a> `gap:run-persists-complete-workspace-request` — [delta:task-run-schema](#delta-task-run-schema) and [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) reconstruct immutable callback input from mutable task state.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Retain — [the assessed upstream](#latest-upstream-assessment) has no equivalent immutable workspace request/binding.                            | <a id="missing-workspace-request-pins-base-ref"></a> `missing:workspace-request-pins-base-ref`; <a id="missing-run-persists-complete-workspace-request"></a> `missing:run-persists-complete-workspace-request`; <a id="missing-persisted-request-lease-round-trip"></a> `missing:persisted-request-lease-round-trip`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| <a id="req-canonical-lease"></a> `req:canonical-lease`                 | [Provider Contract](#provider-contract)                                                                                                                                  | <a id="gap-canonical-workspace-lease-callbacks"></a> `gap:canonical-workspace-lease-callbacks` — [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) does not construct one callback representation after validation; <a id="gap-detached-head-lease-rejected"></a> `gap:detached-head-lease-rejected` — [delta:worktree-planning](#delta-worktree-planning) admits a detached checkout.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Retain — [the assessed upstream](#latest-upstream-assessment) has no provider lease contract.                                                   | <a id="missing-workspace-lease-validation"></a> `missing:workspace-lease-validation`; <a id="missing-canonical-workspace-lease-callbacks"></a> `missing:canonical-workspace-lease-callbacks`; <a id="missing-detached-head-lease-rejected"></a> `missing:detached-head-lease-rejected`; <a id="missing-alternate-checkout-preserves-requested-coordinates"></a> `missing:alternate-checkout-preserves-requested-coordinates`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| <a id="req-ordering-reservations"></a> `req:ordering-reservations`     | [Acquire, Materialize, And Spawn Ordering](#acquire-materialize-and-spawn-ordering), [Canonical Reservation State Machine](#canonical-reservation-state-machine)         | <a id="gap-acquired-run-remains-active-through-spawn"></a> `gap:acquired-run-remains-active-through-spawn` — [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) and [delta:process-supervision](#delta-process-supervision) do not fence the complete handoff; <a id="gap-workspace-reservation-state-machine"></a> `gap:workspace-reservation-state-machine` — [delta:task-run-schema](#delta-task-run-schema) and [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) lack durable phases/attempt ownership; <a id="gap-prebind-dispositions-fence-original-claim"></a> `gap:prebind-dispositions-fence-original-claim` — [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) does not fence callback disposition to its attempt; <a id="gap-spawned-process-persistence-failure-proves-death"></a> `gap:spawned-process-persistence-failure-proves-death` — [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) and [delta:process-supervision](#delta-process-supervision) can lose a started process; <a id="gap-durable-launch-intent-before-spawn"></a> `gap:durable-launch-intent-before-spawn` — [delta:task-run-schema](#delta-task-run-schema) and [delta:process-supervision](#delta-process-supervision) lack pre-spawn launch authority.                                                                                                                                                                                                                                                                                                | Retain — [the assessed upstream](#latest-upstream-assessment) has no blocking acquire/reservation lifecycle.                                    | <a id="missing-callbacks-outside-write-transactions"></a> `missing:callbacks-outside-write-transactions`; <a id="missing-successful-acquire-precedes-materialize-and-spawn"></a> `missing:successful-acquire-precedes-materialize-and-spawn`; <a id="missing-acquire-persistence-compensation"></a> `missing:acquire-persistence-compensation`; <a id="missing-acquired-run-remains-active-through-spawn"></a> `missing:acquired-run-remains-active-through-spawn`; <a id="missing-workspace-reservation-state-machine"></a> `missing:workspace-reservation-state-machine`; <a id="missing-prebind-dispositions-fence-original-claim"></a> `missing:prebind-dispositions-fence-original-claim`; <a id="missing-spawned-process-persistence-failure-proves-death"></a> `missing:spawned-process-persistence-failure-proves-death`; <a id="missing-durable-launch-intent-before-spawn"></a> `missing:durable-launch-intent-before-spawn`                                                                                                                                                                                                                                                                                                                                                                                    |
| <a id="req-contention-failure"></a> `req:contention-failure`           | [Contention And Acquisition Failures](#contention-and-acquisition-failures)                                                                                              | Conforming — [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) implements the ordinary busy/error/compensation paths; reservation-specific defects remain owned by [req:ordering-reservations](#req-ordering-reservations), not this row.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Retain — [the assessed upstream](#latest-upstream-assessment) has no provider-contention outcome.                                               | `tests/hermes_cli/test_kanban_workspace_provider.py:test_busy_defers_without_failure_and_spawn_error_releases_acquired_lease`; <a id="missing-busy-restores-source-lane-without-failure-state"></a> `missing:busy-restores-source-lane-without-failure-state`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| <a id="req-process-renewal-release"></a> `req:process-renewal-release` | [Renewal And Lost Ownership](#renewal-and-lost-ownership), [Terminal Paths, Restart, And Release](#terminal-paths-restart-and-release)                                   | <a id="gap-ended-live-leases-renew-until-death"></a> `gap:ended-live-leases-renew-until-death` — [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) and [delta:process-supervision](#delta-process-supervision) omit ended-but-possibly-live runs; <a id="gap-workspace-callback-deadlines-and-isolation"></a> `gap:workspace-callback-deadlines-and-isolation` — [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) lacks bounded isolated calls; <a id="gap-nonlocal-renewal-loss-holds-claim"></a> `gap:nonlocal-renewal-loss-holds-claim` — [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) and [delta:process-supervision](#delta-process-supervision) can release without remote death proof; <a id="gap-all-reclaim-paths-require-authoritative-death"></a> `gap:all-reclaim-paths-require-authoritative-death` — [delta:task-run-schema](#delta-task-run-schema), [delta:dispatch-reconciliation](#delta-dispatch-reconciliation), and [delta:process-supervision](#delta-process-supervision) do not preserve authority on every reclaim path; <a id="gap-dangling-run-recovery-proves-death"></a> `gap:dangling-run-recovery-proves-death` — [delta:task-run-schema](#delta-task-run-schema) and [delta:process-supervision](#delta-process-supervision) can erase evidence during recovery; <a id="gap-durable-workspace-release-outcome"></a> `gap:durable-workspace-release-outcome` — [delta:task-run-schema](#delta-task-run-schema) and [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) do not persist one callback outcome. | Retain — [the assessed upstream](#latest-upstream-assessment) has process/reclaim adjacency but no provider renewal/release authority.          | `tests/hermes_cli/test_kanban_workspace_provider.py:test_provider_gates_spawn_persists_access_and_releases_only_after_run_exit`; <a id="missing-ended-live-leases-renew-until-death"></a> `missing:ended-live-leases-renew-until-death`; <a id="missing-workspace-callback-deadlines-and-isolation"></a> `missing:workspace-callback-deadlines-and-isolation`; <a id="missing-nonlocal-renewal-loss-holds-claim"></a> `missing:nonlocal-renewal-loss-holds-claim`; <a id="missing-all-reclaim-paths-require-authoritative-death"></a> `missing:all-reclaim-paths-require-authoritative-death`; <a id="missing-dangling-run-recovery-proves-death"></a> `missing:dangling-run-recovery-proves-death`; <a id="missing-durable-workspace-release-outcome"></a> `missing:durable-workspace-release-outcome`; <a id="missing-renewal-provider-failure-semantics"></a> `missing:renewal-provider-failure-semantics`; <a id="missing-renewal-loss-survivor-holds-claim"></a> `missing:renewal-loss-survivor-holds-claim`; <a id="missing-lease-loss-does-not-spend-failure-budget"></a> `missing:lease-loss-does-not-spend-failure-budget`; <a id="missing-release-failure-retries"></a> `missing:release-failure-retries`; <a id="missing-terminal-paths-release-after-death"></a> `missing:terminal-paths-release-after-death` |
| <a id="req-cleanup-destructive"></a> `req:cleanup-destructive`         | [Checkout Ownership And Destructive Operations](#checkout-ownership-and-destructive-operations)                                                                          | <a id="gap-provider-to-core-cleanup-ownership"></a> `gap:provider-to-core-cleanup-ownership` — [delta:worktree-planning](#delta-worktree-planning) and [delta:destructive-transfer](#delta-destructive-transfer) use task history instead of run/path ownership; <a id="gap-board-removal-serialized-with-acquire"></a> `gap:board-removal-serialized-with-acquire` — [delta:destructive-transfer](#delta-destructive-transfer), [delta:dispatch-reconciliation](#delta-dispatch-reconciliation), and [delta:task-run-schema](#delta-task-run-schema) lack a stable shared lifecycle fence; <a id="gap-dispatch-lock-follows-open-connection"></a> `gap:dispatch-lock-follows-open-connection` — [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) derives exclusion from ambient selection.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Retain — [the assessed upstream](#latest-upstream-assessment) has adjacent cleanup/GC behavior but no provider authority guard.                 | <a id="missing-provider-to-core-cleanup-ownership"></a> `missing:provider-to-core-cleanup-ownership`; <a id="missing-board-removal-serialized-with-acquire"></a> `missing:board-removal-serialized-with-acquire`; <a id="missing-dispatch-lock-follows-open-connection"></a> `missing:dispatch-lock-follows-open-connection`; <a id="missing-provider-cleanup-and-delete-guards"></a> `missing:provider-cleanup-and-delete-guards`; <a id="missing-board-removal-transfer-authority"></a> `missing:board-removal-transfer-authority`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| <a id="req-migration-transfer"></a> `req:migration-transfer`           | [Persisted State And Compatibility](#persisted-state-and-compatibility), [Checkout Ownership And Destructive Operations](#checkout-ownership-and-destructive-operations) | <a id="gap-legacy-unreleased-binding-quarantine"></a> `gap:legacy-unreleased-binding-quarantine` — [delta:task-run-schema](#delta-task-run-schema) and [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) do not classify legacy authority; <a id="gap-workspace-binding-stable-ids-on-rebuild"></a> `gap:workspace-binding-stable-ids-on-rebuild` — [delta:task-run-schema](#delta-task-run-schema) can reassign provider-visible IDs; <a id="gap-transfer-scrubs-all-workspace-authority"></a> `gap:transfer-scrubs-all-workspace-authority` — [delta:destructive-transfer](#delta-destructive-transfer) has an incomplete authority scrub; <a id="gap-import-private-sanitize-before-publish"></a> `gap:import-private-sanitize-before-publish` — [delta:destructive-transfer](#delta-destructive-transfer) publishes before sanitization completes; <a id="gap-import-allowlisted-canonical-schema"></a> `gap:import-allowlisted-canonical-schema` — [delta:destructive-transfer](#delta-destructive-transfer) executes writes against archive-supplied schema.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Retain — [the assessed upstream](#latest-upstream-assessment) has no migration/transfer treatment for these bindings.                           | <a id="missing-legacy-unreleased-binding-quarantine"></a> `missing:legacy-unreleased-binding-quarantine`; <a id="missing-workspace-binding-stable-ids-on-rebuild"></a> `missing:workspace-binding-stable-ids-on-rebuild`; <a id="missing-transfer-scrubs-all-workspace-authority"></a> `missing:transfer-scrubs-all-workspace-authority`; <a id="missing-import-private-sanitize-before-publish"></a> `missing:import-private-sanitize-before-publish`; <a id="missing-import-allowlisted-canonical-schema"></a> `missing:import-allowlisted-canonical-schema`; <a id="missing-workspace-lease-schema-migration"></a> `missing:workspace-lease-schema-migration`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| <a id="req-access-surfaces"></a> `req:access-surfaces`                 | [Access Semantics](#access-semantics), [User And Integration Surfaces](#user-and-integration-surfaces)                                                                   | <a id="gap-decomposed-non-worktree-read-rejected"></a> `gap:decomposed-non-worktree-read-rejected` — [delta:task-graph](#delta-task-graph) bypasses shared normalization and cross-field validation; the other public owners are mapped by [delta:cli-surface](#delta-cli-surface), [delta:agent-tool](#delta-agent-tool), and [delta:dashboard-api](#delta-dashboard-api).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Retain — [the assessed upstream](#latest-upstream-assessment) has no workspace-provider access surface.                                         | <a id="missing-decomposed-non-worktree-read-rejected"></a> `missing:decomposed-non-worktree-read-rejected`; <a id="missing-workspace-access-surface-propagation"></a> `missing:workspace-access-surface-propagation`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |

## Persisted State And Compatibility

The task carries requested coordination intent and the revision shared by ordinary retries:

| Table   | Column                          | Contract                                                                                                                         |
| ------- | ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `tasks` | `workspace_access`              | Non-null `read` or `write`; old rows migrate to `write`.                                                                         |
| `tasks` | `workspace_request_revision`    | Monotonic revision number; initialized with the first base and incremented only by a supported retarget/continuation transition. |
| `tasks` | `workspace_request_repo_root`   | Canonical repository identity for this revision; nullable before initial planning and after a relocating import.                 |
| `tasks` | `workspace_path`, `branch_name` | Requested path and named branch in the revision, never provider-effective coordinates.                                           |
| `tasks` | `workspace_base_commit`         | Full SHA conditionally initialized before the first callback; changes only with an explicit new revision.                        |

The required run binding captures enough authority and immutable input to reconstruct the exact
callbacks without current config or mutable Git state. The logical names for missing fields below
are illustrative; a schema change may choose different concrete names while preserving the same
identity:

| Binding element                                           | Required contract                                                                                                                                  | Current implementation                                        |
| --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `workspace_binding_version`, `workspace_binding_complete` | Non-null supported version and true completeness marker for every new acquisition; nullable means a legacy cohort, not permission to infer fields. | Missing.                                                      |
| `task_runs.task_id`, `task_runs.id`                       | Exact provider-visible `task_id` and `run_id`; immutable while any callback/release authority survives.                                            | Used, but legacy table rebuild can reassign run IDs.          |
| `workspace_owner_id`                                      | Claim owner captured as request data, independent of later claim clearing.                                                                         | Missing; reconstruction reads mutable `task_runs.claim_lock`. |
| `workspace_acquisition_attempt_id`                        | Stable public request ID and durable deduplication/compensation identity created before acquire.                                                   | Missing.                                                      |
| `workspace_request_revision`                              | Exact task request revision captured before the first callback.                                                                                    | Missing.                                                      |
| `workspace_provider`                                      | Normalized selected provider name.                                                                                                                 | Persisted.                                                    |
| `workspace_provider_scope`                                | Acquiring `hermes_home_key()`.                                                                                                                     | Persisted.                                                    |
| `workspace_provider_slot`                                 | Exact scoped/global tier plus scope and normalized slot.                                                                                           | Missing; live lookup recomputes scoped-first/global-fallback. |
| `workspace_provider_compatibility_id`                     | Non-empty public identity shared only by lease-compatible registrations.                                                                           | Missing.                                                      |
| `workspace_provider_registry_epoch`                       | UUID of the registry instance that supplied the lease.                                                                                             | Missing.                                                      |
| `workspace_provider_generation`                           | Per-slot generation, exact only with the persisted registry epoch.                                                                                 | Missing; same-slot replacement can receive old callbacks.     |
| `workspace_lease_id`                                      | Validated opaque provider lease identifier in the canonical lease.                                                                                 | Persisted.                                                    |
| `workspace_lease_path`                                    | Canonical effective absolute checkout path.                                                                                                        | Persisted after path normalization.                           |
| `workspace_lease_branch`                                  | Canonical effective named branch after `None` inheritance.                                                                                         | Persisted, but detached HEAD is not rejected.                 |
| `workspace_repo_root`                                     | Original resolved repository root captured for the run.                                                                                            | Persisted.                                                    |
| `workspace_access`                                        | Access captured for this run.                                                                                                                      | Persisted.                                                    |
| `workspace_kind`                                          | Workspace kind captured for this run.                                                                                                              | Missing; reconstruction joins mutable task state.             |
| `workspace_project_id`                                    | Optional project ID captured for this run.                                                                                                         | Missing; reconstruction joins mutable task state.             |
| `workspace_board`                                         | Board slug.                                                                                                                                        | Persisted.                                                    |
| `workspace_board_db_path`                                 | Absolute board DB identity.                                                                                                                        | Persisted.                                                    |
| `workspace_requested_path`                                | Mutation-free core target supplied on acquire.                                                                                                     | Persisted.                                                    |
| `workspace_requested_branch`                              | Core-planned named branch supplied on acquire.                                                                                                     | Persisted.                                                    |
| `workspace_requested_base_commit`                         | Full SHA supplied on acquire and used for admission/materialization/retries.                                                                       | Missing; core may branch from mutable `HEAD`.                 |
| `workspace_lease_expires_at`                              | Optional provider expiry for diagnostics/reconstruction.                                                                                           | Persisted.                                                    |
| `workspace_dispatch_reservation`                          | Canonical phase plus executor owner/epoch/deadline and cancellation/terminal intent.                                                               | Missing.                                                      |
| `workspace_worker_launch_id`                              | Stable process-launch identity persisted before spawn.                                                                                             | Missing.                                                      |
| `workspace_worker_supervisor`                             | Restart-recoverable supervisor/launcher identity that can resolve the stable launch.                                                               | Missing.                                                      |
| `workspace_release_outcome`                               | Advisory, open-ended, write-once outcome used by the first release and every retry.                                                                | Missing; release can drift between call-site and run outcome. |
| `workspace_lease_released_at`                             | Set only after successful provider release.                                                                                                        | Persisted.                                                    |

The current rows therefore capture most callback arguments, but not all authority needed for exact
resolution: name plus home scope do not identify the scoped/global slot, compatibility lineage, or
an epoch-scoped generation; requested coordinates do not pin the immutable revision; mutable task
joins can change request fields; and acquisition-attempt, launch, and release authority do not all
survive every crash path. Persisted lease fields reconstruct the canonical callback value, not
necessarily the provider's pre-validation representation. A schema/API fix must migrate old rows
conservatively and fail closed when original attempt, registration, base, launch state, or release
authority cannot be reconstructed.

The feature also depends on pre-existing Kanban lifecycle fields. They are not part of the fork's
schema delta, but changing their meaning can break lease safety:

| Existing fields                                                 | Dependency                                                                                                                             |
| --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `tasks.id`, `task_runs.id`, `task_runs.task_id`                 | Provider-visible request identity; rebuilds preserve exact values while bound or atomically remap all portable references after drain. |
| `tasks.current_run_id`, `status`, `claim_lock`, `claim_expires` | Fence conditional acquire/reclaim updates to the active claimed run.                                                                   |
| `tasks.worker_pid`, `worker_started_at`                         | Drive active-worker supervision before the task leaves `running`.                                                                      |
| `task_runs.claim_lock`, `claim_expires`                         | Reconstruct the immutable request owner and identify the owning host/supervisor.                                                       |
| `task_runs.worker_pid`, `worker_started_at`                     | Retain restart-safe process evidence after a task status transition.                                                                   |
| `task_runs.ended_at`, `outcome`                                 | Trigger terminal reconciliation; neither field proves process death or replaces the durable workspace-specific release outcome.        |

Migration classifies every pre-version row without synthesizing missing values:

| Legacy cohort                                                                          | Required treatment                                                                                                                                                                                                                                                                                                      |
| -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Unbound (`workspace_provider IS NULL`)                                                 | Keep as ordinary historical/core-managed data. New acquisitions alone write a complete, versioned binding.                                                                                                                                                                                                              |
| Released historical (`workspace_provider IS NOT NULL` and release is durably complete) | Preserve as inert diagnostics or scrub it during a portable export; never call the provider again. Nullable version/completeness fields continue to identify its legacy origin.                                                                                                                                         |
| Unreleased legacy                                                                      | Do not invent a slot, compatibility ID, registry epoch, base, request revision, launch state, or outcome. Before upgrade, drain it on old code; otherwise require a provider-declared, versioned legacy mapping that supplies and validates the exact missing callback contract, or place it in fail-closed quarantine. |

Quarantine retains the original row and every known claim/process/lease field, blocks task
admission and task/board deletion, and routes any known worker to authoritative death proof. It does
not mark release successful; without a declared compatible mapping, provider TTL/publication fencing
remains the only external cleanup backstop. An operator-visible repair may attach a provider mapping
but may not fabricate evidence that a worker died or a callback succeeded.

[delta:task-run-schema](#delta-task-run-schema) owns synchronization of fresh DDL, additive
migration, and rebuild representations. Rebuilds preserve exact task/run IDs for active,
unreleased, or quarantined bindings. If a legacy representation cannot preserve them, all bindings
must first be drained; for other rows the rebuild maps every task/run/event/current-run reference
atomically in the same transaction. [delta:destructive-transfer](#delta-destructive-transfer) owns
portable access intent, machine-local authority scrubbing, and relocated request revisions.

Changing or removing these columns requires a migration plan for databases that may contain active
or unreleased leases. Never discard the only persisted provider/scope/lease identity during an
upstream rebase.

## Invariants

The normative definitions linked by the ledger are canonical. Rebase and refactor review uses these
cross-requirement groupings instead of a second copy of their prose:

- [req:selection-scope](#req-selection-scope) and
  [req:registration-lifetime](#req-registration-lifetime) are reviewed together across
  [delta:selection-defaults](#delta-selection-defaults),
  [delta:contract-registry](#delta-contract-registry), and
  [delta:plugin-registration](#delta-plugin-registration).
- [req:request-base](#req-request-base), [req:canonical-lease](#req-canonical-lease),
  [req:ordering-reservations](#req-ordering-reservations), and
  [req:contention-failure](#req-contention-failure) meet at the schema/planning/dispatch/supervision
  handoff mapped by their linked Delta Inventory rows.
- [req:process-renewal-release](#req-process-renewal-release) and
  [req:cleanup-destructive](#req-cleanup-destructive) share the terminal ownership boundary across
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation),
  [delta:process-supervision](#delta-process-supervision), and
  [delta:destructive-transfer](#delta-destructive-transfer).
- [req:migration-transfer](#req-migration-transfer) and
  [req:access-surfaces](#req-access-surfaces) are reviewed together wherever persisted intent crosses
  a public or portable boundary.

## Known Implementation Gaps

These are contract violations in the implementation first delivered by
[`fcf48e44`](https://github.com/dkropachev/hermes/commit/fcf48e44dafba0a804e3652f5a0344daa7face3c).
They must remain visible during a rebase; a clean textual merge does not resolve them.

- [gap:decomposed-non-worktree-read-rejected](#gap-decomposed-non-worktree-read-rejected) affects
  [req:access-surfaces](#req-access-surfaces) in [delta:task-graph](#delta-task-graph);
  `_insert_decomposed_child()` accepts an arbitrary `workspace_kind`, does not trim or lowercase a
  supplied `workspace_access`, and validates only the raw access enum. It also does not reject
  `workspace_access="read"` when a child selects or inherits `scratch`/`dir`. Route child creation
  through the same normalization, kind validation, and cross-field invariant as `create_task()`.
- [gap:acquired-run-remains-active-through-spawn](#gap-acquired-run-remains-active-through-spawn)
  affects [req:ordering-reservations](#req-ordering-reservations) in
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) and
  [delta:process-supervision](#delta-process-supervision); conditional acquisition persistence
  checks the run row without also matching the task's
  `current_run_id` and task/run claim tokens. The active run/claim can change after that write but
  before checkout materialization, spawn, or PID attachment; `_set_worker_pid()` then updates
  whichever run is current. Make binding itself conditional on the complete task/run/claim identity,
  and use a durable run-owned materialization/launch reservation honored by every lifecycle writer.
  If a reservation is lost after spawn may have occurred, retain authority and authoritatively stop
  the exact process and prove death before release.
- [gap:workspace-reservation-state-machine](#gap-workspace-reservation-state-machine) affects
  [req:ordering-reservations](#req-ordering-reservations) in
  [delta:task-run-schema](#delta-task-run-schema) and
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation); `SCHEMA_SQL`,
  `_TASK_RUN_COLUMNS`, rebuild specifications, and dispatch state have no canonical reservation
  phase, durable acquisition-attempt ID, executor owner/epoch/deadline,
  monotonic terminal intent, or task-level query over historical reservations. Lifecycle code can
  therefore infer safety from `current_run_id`, claim clearing, `ended_at`, or a null PID and admit a
  successor while an acquire/materialization/launch remains unknown. Persist the state machine
  above, CAS every phase and timed-out takeover, deduplicate acquire by attempt ID, and block task or
  board lifecycle until historical execution and lease/attempt authority settle.
- [gap:prebind-dispositions-fence-original-claim](#gap-prebind-dispositions-fence-original-claim)
  affects [req:ordering-reservations](#req-ordering-reservations) in
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation); provider busy and acquisition
  errors are handled after callbacks without one shared disposition CAS over the original task,
  run, claim, request revision, durable acquisition-attempt ID, and reservation executor. A callback
  that returns after lifecycle state changed can clear or move whichever attempt is current. Fence
  busy/error/timeout and compensation bookkeeping to the original attempt; a stale callback may
  never mutate a successor, and a late lease must remain authority until idempotent compensation
  through its pinned issuer succeeds.
- [gap:spawned-process-persistence-failure-proves-death](#gap-spawned-process-persistence-failure-proves-death)
  affects [req:ordering-reservations](#req-ordering-reservations) in
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) and
  [delta:process-supervision](#delta-process-supervision); `_dispatch_lane_task()` stores the
  returned PID in a local variable before `_set_worker_pid()`
  persists it. If persistence raises after the process started, the exception path neither terminates
  nor proves death for that process, while the ended run may still have a null durable PID and become
  eligible for release on the next sweep. This exception path needs supervisor-routed termination
  and authoritative death-before-release compensation.
- [gap:durable-launch-intent-before-spawn](#gap-durable-launch-intent-before-spawn) affects
  [req:ordering-reservations](#req-ordering-reservations) in
  [delta:task-run-schema](#delta-task-run-schema),
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation), and
  [delta:process-supervision](#delta-process-supervision); dispatch has no durable launch intent,
  stable launch ID, or recoverable supervisor identity before `_call_spawn_fn()`. An abrupt
  dispatcher crash after the process starts but before `_set_worker_pid()` leaves no durable way to
  distinguish “never launched” from “live child with no attached PID”; the ended-run sweep can later
  treat the null PID as releasable. Persist launch authority before spawn and reconcile an unknown
  launch as possibly live until its owning supervisor proves non-start/death.
- [gap:ended-live-leases-renew-until-death](#gap-ended-live-leases-renew-until-death) affects
  [req:process-renewal-release](#req-process-renewal-release) in
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) and
  [delta:process-supervision](#delta-process-supervision); `renew_active_workspace_leases()` selects
  only `ended_at IS NULL`. A run can be ended while its
  retained PID/fingerprint or non-local ownership evidence still means execution may continue, so its
  unreleased external lease silently stops renewing before the terminal reaper proves death. In
  addition, `_reclaim_lost_workspace_leases()` joins only the task's current unended run, so renewal
  loss for an ended/superseded live run would not reach that run's recorded supervisor. Select every
  possibly live lease and route loss by persisted run/launch identity regardless of `current_run_id`
  or `ended_at`.
- [gap:workspace-callback-deadlines-and-isolation](#gap-workspace-callback-deadlines-and-isolation)
  affects [req:process-renewal-release](#req-process-renewal-release) in
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) and
  [delta:contract-registry](#delta-contract-registry); availability/acquire/renew/release are
  invoked inline with no core deadline or per-callback isolation. A hung
  invocation can retain the dispatch lock, block later active renewals, and prevent unrelated board
  work; a slow finished-run release can likewise delay time-sensitive renewals. Enforce bounded
  coordinator waits outside board locks, isolate capacity and per-lease in-flight calls, run the
  active-renewal pass before finished releases, and keep a timed-out acquire's durable attempt,
  registration pin, and late-result compensation authority until invocation exit.
- [gap:nonlocal-renewal-loss-holds-claim](#gap-nonlocal-renewal-loss-holds-claim) affects
  [req:process-renewal-release](#req-process-renewal-release) in
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation) and
  [delta:process-supervision](#delta-process-supervision); `_reclaim_lost_workspace_leases()` holds
  the claim for a surviving host-local process, but
  `_worker_survived_termination()` deliberately falls through for a non-local claim or a
  termination attempt with no local signaling authority. The current path can then clear/requeue and
  release without proving remote death. Reconciliation must defer to the owning supervisor or
  otherwise retain the claim/release authority until death is authoritative.
- [gap:all-reclaim-paths-require-authoritative-death](#gap-all-reclaim-paths-require-authoritative-death)
  affects [req:process-renewal-release](#req-process-renewal-release) in
  [delta:task-run-schema](#delta-task-run-schema),
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation), and
  [delta:process-supervision](#delta-process-supervision); automatic stale and generic reclaim use
  the host-local-only survivor predicate, while `reclaim_task()` clears the claim even when termination
  did not prove death; `reassign_task(..., reclaim_first=True)` inherits that manual-reclaim behavior.
  Every automatic and operator path must retain the claim, process evidence, and lease until the
  owning supervisor or another authoritative mechanism proves quiescence.
- [gap:dangling-run-recovery-proves-death](#gap-dangling-run-recovery-proves-death) affects
  [req:process-renewal-release](#req-process-renewal-release) in
  [delta:task-run-schema](#delta-task-run-schema) and
  [delta:process-supervision](#delta-process-supervision); `_reclaim_dangling_run()`, used by
  ordinary `ready` re-claim as well as unblock and review-reopen
  recovery, closes a leaked run and clears its claim-owner identity and durable PID without checking
  liveness or terminating it. A provider-bound row can then satisfy the ended-run release query with
  no process proof and reconstruct the release request with an empty owner. Recovery must retain
  callback/process identity for the terminal reaper or authoritatively stop the worker before
  clearing it.
- [gap:provider-to-core-cleanup-ownership](#gap-provider-to-core-cleanup-ownership) affects
  [req:cleanup-destructive](#req-cleanup-destructive) in
  [delta:worktree-planning](#delta-worktree-planning) and
  [delta:destructive-transfer](#delta-destructive-transfer); `task_has_provider_workspace()`
  remains true forever after any provider-bound run.
  If a later run executes with the provider disabled and creates a core-owned worktree, completion and
  GC still skip it, while no provider release owns that new path. Cleanup ownership must be keyed to
  the effective run/path rather than historical provider use.
- [gap:provider-resolution-tier-binding](#gap-provider-resolution-tier-binding) affects
  [req:registration-lifetime](#req-registration-lifetime) in
  [delta:contract-registry](#delta-contract-registry),
  [delta:plugin-registration](#delta-plugin-registration),
  [delta:task-run-schema](#delta-task-run-schema), and
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation); live lookup resolves scoped-first
  with a global fallback while persistence records only provider name plus home scope. Scoped unload
  can therefore redirect callbacks to a
  global implementation, later scoped registration can shadow a lease acquired globally, and
  same-slot hot reload/restore can give an old lease to an incompatible provider instance.
  `register_workspace_provider()` also lacks the additive optional keyword-only
  `lease_compatibility_id=None` field and legacy-unversioned marker. Add that source-compatible
  surface, persist exact registration identity for strict new acquisitions, fail closed with an
  actionable setup error when neither an explicit ID nor a declared versioned legacy policy exists,
  and leave outstanding legacy leases to drain/mapping/quarantine rather than inventing compatibility.
- [gap:provider-acquisition-generation-race](#gap-provider-acquisition-generation-race) affects
  [req:registration-lifetime](#req-registration-lifetime) in
  [delta:contract-registry](#delta-contract-registry) and
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation); acquisition resolves a live
  provider, calls `try_acquire()`, and later binds the lease without pinning that registration
  or proving its exact slot/generation is still current. Concurrent unload/replacement can therefore
  make the durable binding describe a different registration from the instance that issued the
  lease. Pin through binding or perform a post-acquire registry CAS; on loss, compensate through the
  original provider instance.
- [gap:provider-callback-registration-pinning](#gap-provider-callback-registration-pinning) affects
  [req:registration-lifetime](#req-registration-lifetime) in
  [delta:contract-registry](#delta-contract-registry) and
  [delta:plugin-registration](#delta-plugin-registration); unload and workspace callback lookup do
  not refcount/pin the resolved compatible registration for the full availability, acquire,
  renew, release, or compensation invocation. Unload/replacement can tear down plugin state while a
  callback is active. Pin the exact resolved record through return, make unload wait for callback
  drain, and retain the separate post-acquire current-slot CAS.
- [gap:workspace-request-pins-base-ref](#gap-workspace-request-pins-base-ref) affects
  [req:request-base](#req-request-base) in [delta:contract-registry](#delta-contract-registry),
  [delta:task-run-schema](#delta-task-run-schema),
  [delta:worktree-planning](#delta-worktree-planning), and
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation); `WorkspaceRequest` has no
  immutable base ref/SHA and planned branch creation reads mutable repository `HEAD` after
  acquisition. A provider returning the
  core target cannot guarantee the exact base it admitted, while alternate checkout validation does
  not compare its `HEAD` to an admitted base. The task also has no request revision tying repository,
  path, branch, and base together. Claim-conditionally initialize that revision before the first
  callback, copy it to every run, require both existing core and alternate checkouts to start at that
  SHA, and make retries reuse it. Review/request-changes/reopen continuation and retarget must create
  an explicit new revision (or attempt branch) after drain rather than reset away worker commits;
  relocating transfer clears and re-resolves it. Until then a provider requiring an exact base must
  return its own already-pinned checkout.
- [gap:run-persists-complete-workspace-request](#gap-run-persists-complete-workspace-request)
  affects [req:request-base](#req-request-base) in
  [delta:task-run-schema](#delta-task-run-schema) and
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation); `_request_and_lease()`
  reconstructs `workspace_kind` and `project_id` by joining the current task,
  and reconstructs owner identity from a claim field that recovery may clear. Persist every immutable
  request field on the run, including task/run IDs, owner, acquisition attempt, request revision,
  kind, project, base, and coordinates; callbacks must not consult mutable task state.
- [gap:canonical-workspace-lease-callbacks](#gap-canonical-workspace-lease-callbacks) affects
  [req:canonical-lease](#req-canonical-lease) in
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation); `_validate_lease()` returns the
  provider object unchanged while acquisition separately normalizes the
  effective path/branch for persistence, but `acquire_failed` compensation may receive a different
  representation from later reconstructed callbacks. Validate and construct one canonical lease
  for persistence and every post-validation callback; only pre-canonical validation compensation
  receives the original provider lease.
- [gap:detached-head-lease-rejected](#gap-detached-head-lease-rejected) affects
  [req:canonical-lease](#req-canonical-lease) in
  [delta:worktree-planning](#delta-worktree-planning); branch validation checks equality only when
  `_git_current_branch()` returns a value. A detached provider checkout therefore
  passes even though the required effective branch is named (including when lease branch `None`
  inherits the requested branch). Reject detached HEAD for both core-target reuse and alternate
  provider paths.
- [gap:board-removal-serialized-with-acquire](#gap-board-removal-serialized-with-acquire) affects
  [req:cleanup-destructive](#req-cleanup-destructive) in
  [delta:destructive-transfer](#delta-destructive-transfer),
  [delta:task-run-schema](#delta-task-run-schema), and
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation); board removal checks pending
  leases and then moves or deletes without a lifecycle fence. The current dispatch lock lives beside
  `kanban.db` inside the directory being moved. An in-flight dispatcher can have a pre-binding
  acquisition callback outstanding or persist a lease between the check and move, while a stale
  waiter can later reopen/recreate the old path. Reserve lifecycle state in a stable parent before
  DB open/acquire, include unresolved attempts in the authority check, hold it through tombstone and
  move/delete, and require a post-callback tombstone/identity revalidation that compensates any late
  lease through the pinned issuer.
- [gap:dispatch-lock-follows-open-connection](#gap-dispatch-lock-follows-open-connection) affects
  [req:cleanup-destructive](#req-cleanup-destructive) in
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation); dispatch derives its lock path
  from `_kb.kanban_db_path(board=board)` even though the caller has already supplied an open
  connection. If ambient board selection and that connection disagree, the lock guards one board
  while the tick mutates another. Derive the lock identity from the connection's resolved
  `PRAGMA database_list` main file and fail closed if it cannot be proven.
- [gap:durable-workspace-release-outcome](#gap-durable-workspace-release-outcome) affects
  [req:process-renewal-release](#req-process-renewal-release) in
  [delta:task-run-schema](#delta-task-run-schema) and
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation); `release_workspace_lease()`
  accepts a call-site outcome override and otherwise derives the value
  from `task_runs.outcome`; the binding has no durable workspace-specific release outcome. A failed
  immediate `workspace_failed` release can therefore retry later as the run's `spawn_failed`
  outcome. Select and persist a write-once `workspace_release_outcome` with the terminal/setup
  transition, and use it for first delivery and every retry.
- [gap:legacy-unreleased-binding-quarantine](#gap-legacy-unreleased-binding-quarantine) affects
  [req:migration-transfer](#req-migration-transfer) in
  [delta:task-run-schema](#delta-task-run-schema) and
  [delta:dispatch-reconciliation](#delta-dispatch-reconciliation); `SCHEMA_SQL`,
  `_TASK_RUN_COLUMNS`, and rebuild specifications have no nullable binding version/completeness
  marker or explicit treatment of the
  three historical cohorts. In particular, an unreleased pre-upgrade binding lacks attempt, slot,
  compatibility, base/revision, launch, and outcome authority that cannot be defaulted safely.
  Require pre-upgrade drain, a provider-declared versioned legacy mapping, or fail-closed quarantine
  that retains the row/admission blocker and proves worker death without claiming release.
- [gap:workspace-binding-stable-ids-on-rebuild](#gap-workspace-binding-stable-ids-on-rebuild)
  affects [req:migration-transfer](#req-migration-transfer) in
  [delta:task-run-schema](#delta-task-run-schema); `_rebuild_drifted_tables()` deliberately drops
  legacy TEXT primary keys and lets SQLite reassign
  `task_runs.id`, but does not map `tasks.current_run_id`, `task_events.run_id`, or provider-visible
  request IDs. Preserve exact IDs while a binding is active/unreleased/quarantined, or drain first;
  any permitted remap must update every reference atomically.
- [gap:transfer-scrubs-all-workspace-authority](#gap-transfer-scrubs-all-workspace-authority)
  affects [req:migration-transfer](#req-migration-transfer) in
  [delta:destructive-transfer](#delta-destructive-transfer); export scrubs a fixed list of current
  run columns, but its snapshot still contains task workspace paths and workspace event
  payloads and has no classification for the new registration/reservation/launch/outcome fields.
  Make export itself schema-versioned and fail closed until every task, event, and old/new authority
  field is sanitized; clear/re-resolve the task request revision on repository/path relocation.
- [gap:import-private-sanitize-before-publish](#gap-import-private-sanitize-before-publish) affects
  [req:migration-transfer](#req-migration-transfer) in
  [delta:destructive-transfer](#delta-destructive-transfer); import creates the target board
  directory and moves the archive DB into public `kanban.db` before schema upgrade and
  runtime/workspace sanitization. Slug selection is a check-then-create race, and failure can leave a
  discoverable partial board. Reserve the slug under the stable lifecycle lock, sanitize a fresh DB
  in private staging, atomically rename the complete directory, and clean reservation/staging on
  every failure.
- [gap:import-allowlisted-canonical-schema](#gap-import-allowlisted-canonical-schema) affects
  [req:migration-transfer](#req-migration-transfer) in
  [delta:destructive-transfer](#delta-destructive-transfer); import runs canonical migrations and
  `_scrub_local_state()` directly against the archive-supplied SQLite schema. A hostile trigger or
  replacement schema object can observe or
  alter those writes and bypass column scrubbing. Treat the archive as read-only data: validate and
  copy allowlisted portable rows into a freshly created trigger-free canonical DB, rejecting
  unsupported schema/types/references before publication.

## Rebase Assessment

### Latest Upstream Assessment

The feature was applied to upstream `10e7de79a9`. At the 2026-09-28 assessment,
`NousResearch/hermes-agent` was at `e408d36339`: 5,698 upstream commits had landed, while the
fork-side implementation remained one squash commit. Searches of the assessed upstream tree found no
`WorkspaceProvider`, `workspace_provider`, `workspace_access`, or equivalent blocking,
restart-safe Kanban workspace-admission lifecycle.

A three-way merge assessment auto-merged the implementation and found one textual conflict in the
navigation owner mapped by [delta:developer-docs](#delta-developer-docs). That result is not
sufficient proof: upstream changed the shared provider registry, plugin loading, Kanban
schema/lifecycle/dispatch/worktree/GC paths, profile scoping, and adjacent documentation. The
semantic hotspots below still require manual review even where Git reports a clean textual merge.

### Current Fork Adjacency Is Not Upstream Equivalence

Separately from the upstream assessment, immutable fork assessment commit `8e9fc477c9` includes
[#11](https://github.com/dkropachev/hermes/pull/11) and
[#13](https://github.com/dkropachev/hermes/pull/13). They add `ctx.workspaces` durable plugin-owned
host directories and `ctx.workspace_tools` lease-bound terminal/file dispatch, documented in
[`plugin-workspace-leases.md`](https://github.com/dkropachev/hermes/blob/8e9fc477c9e0e99bceee7a3797ab764d17a0498b/website/docs/developer-guide/plugin-workspace-leases.md).
Those APIs
are adjacent and potentially reusable infrastructure, but they are distinct from a selected Kanban
provider that admits a repository request and returns effective Git coordinates before spawn. They
do not close any of the seven gaps originally recorded in this spec, nor the additional lifecycle
and validation gaps found during this review. Preserve the documentation cross-link; do not treat
the similar word “lease” as feature equivalence.

### Delta Inventory

This is the sole map from the feature contract to internal owners. Public consumers should use
[Entry Points](#entry-points), not these implementation paths.

| Stable row ID                                                              | Area                                              | Defining owners                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Responsibility to preserve                                                                                                                                                                                       |
| -------------------------------------------------------------------------- | ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| <a id="delta-contract-registry"></a> `delta:contract-registry`             | Contract and registry                             | [`agent/workspace_provider.py`](../agent/workspace_provider.py), [`agent/workspace_registry.py`](../agent/workspace_registry.py), [`agent/provider_registry.py`](../agent/provider_registry.py), [`registration_lifecycle.py`](../registration_lifecycle.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Frozen public values; legacy-unversioned records; scoped/global slot, epoch/generation, compatibility identity, pinning, and replacement lifetime.                                                               |
| <a id="delta-plugin-registration"></a> `delta:plugin-registration`         | Plugin registration and unload                    | [`hermes_cli/plugins.py`](../hermes_cli/plugins.py), [`hermes_cli/plugins_loader.py`](../hermes_cli/plugins_loader.py), [`hermes_cli/plugins_ledger.py`](../hermes_cli/plugins_ledger.py), [`registration_lifecycle.py`](../registration_lifecycle.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Additive `ctx.register_workspace_provider(provider, *, lease_compatibility_id=None)`, installed-provider migration, load-timeout abandonment, ownership ledger, identity-conditional unload, and callback drain. |
| <a id="delta-selection-defaults"></a> `delta:selection-defaults`           | Selection and defaults                            | [`hermes_cli/config_defaults.py`](../hermes_cli/config_defaults.py), [`hermes_cli/kanban_workspace_provider.py`](../hermes_cli/kanban_workspace_provider.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Explicit inert default, profile-scoped lookup, and actionable fail-closed errors.                                                                                                                                |
| <a id="delta-task-run-schema"></a> `delta:task-run-schema`                 | Task/run model and schema                         | [`hermes_cli/kanban_db.py`](../hermes_cli/kanban_db.py) (`SCHEMA_SQL`, row models), [`hermes_cli/kanban_db_connect.py`](../hermes_cli/kanban_db_connect.py) (`_TASK_RUN_COLUMNS`, migrations, rebuilds)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | Access defaults, complete binding/attempt/reservation fields, fresh DDL, additive migration, stable-ID rebuild, and deletion guards.                                                                             |
| <a id="delta-worktree-planning"></a> `delta:worktree-planning`             | Worktree planning and ownership                   | [`hermes_cli/kanban_db_workspace.py`](../hermes_cli/kanban_db_workspace.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | Mutation-free plan, immutable-base materialization/validation, requested/effective separation, and per-run cleanup ownership.                                                                                    |
| <a id="delta-dispatch-reconciliation"></a> `delta:dispatch-reconciliation` | Dispatch, locking, and reconciliation             | [`hermes_cli/kanban_db_dispatch.py`](../hermes_cli/kanban_db_dispatch.py), [`hermes_cli/kanban_workspace_provider.py`](../hermes_cli/kanban_workspace_provider.py), [`hermes_cli/kanban_db_connect.py`](../hermes_cli/kanban_db_connect.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | Acquire/materialize/spawn ordering, connection-derived locking, attempt/reservation CAS, deadlines, compensation, renewal, process-safe release, and restart retry.                                              |
| <a id="delta-process-supervision"></a> `delta:process-supervision`         | Launch and process identity                       | [`hermes_cli/kanban_db_dispatch.py`](../hermes_cli/kanban_db_dispatch.py), [`hermes_cli/cli_single_query.py`](../hermes_cli/cli_single_query.py), [`tools/kanban_tools.py`](../tools/kanban_tools.py), [`tools/process_registry.py`](../tools/process_registry.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Stable pre-spawn launch/supervisor identity, conditional PID/fingerprint adoption, authoritative stop/death proof, and restart recovery.                                                                         |
| <a id="delta-destructive-transfer"></a> `delta:destructive-transfer`       | Cleanup, board lifecycle, and portable operations | [`hermes_cli/kanban_db.py`](../hermes_cli/kanban_db.py), [`hermes_cli/kanban_ops.py`](../hermes_cli/kanban_ops.py), [`hermes_cli/kanban_transfer.py`](../hermes_cli/kanban_transfer.py), [`hermes_cli/kanban_db_connect.py`](../hermes_cli/kanban_db_connect.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Stable lifecycle reservation/tombstone, GC ownership, deletion guards, authority scrubbing, canonical import, and atomic publication.                                                                            |
| <a id="delta-cli-surface"></a> `delta:cli-surface`                         | CLI surface                                       | [`hermes_cli/kanban_parser.py`](../hermes_cli/kanban_parser.py), [`hermes_cli/kanban.py`](../hermes_cli/kanban.py), [`hermes_cli/kanban_output.py`](../hermes_cli/kanban_output.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Access input, display, and task/run diagnostics.                                                                                                                                                                 |
| <a id="delta-task-graph"></a> `delta:task-graph`                           | Task graph                                        | [`hermes_cli/kanban_db_graph.py`](../hermes_cli/kanban_db_graph.py), [`hermes_cli/kanban_decompose.py`](../hermes_cli/kanban_decompose.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Child access inheritance, normalization, kind validation, and automatic-decomposer input.                                                                                                                        |
| <a id="delta-agent-tool"></a> `delta:agent-tool`                           | Agent tool                                        | [`tools/kanban_tools.py`](../tools/kanban_tools.py), [`tools/kanban_tools_schemas.py`](../tools/kanban_tools_schemas.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Access schema, create propagation, task/run output, and PID adoption boundary.                                                                                                                                   |
| <a id="delta-dashboard-api"></a> `delta:dashboard-api`                     | Dashboard API                                     | [`plugins/kanban/dashboard/plugin_api.py`](../plugins/kanban/dashboard/plugin_api.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Create access and HTTP 409 when deletion would orphan an attempt or lease.                                                                                                                                       |
| <a id="delta-developer-docs"></a> `delta:developer-docs`                   | Developer docs                                    | [`website/docs/developer-guide/workspace-provider-plugin.md`](../website/docs/developer-guide/workspace-provider-plugin.md), commit-pinned [`plugin-workspace-leases.md`](https://github.com/dkropachev/hermes/blob/8e9fc477c9e0e99bceee7a3797ab764d17a0498b/website/docs/developer-guide/plugin-workspace-leases.md), [`website/docs/developer-guide/plugins/index.md`](../website/docs/developer-guide/plugins/index.md), [`website/sidebars.ts`](../website/sidebars.ts)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | Public provider contract, adjacent-API boundary, implementation responsibilities, and discoverability.                                                                                                           |
| <a id="delta-user-docs"></a> `delta:user-docs`                             | User docs                                         | [`website/docs/user-guide/features/kanban.md`](../website/docs/user-guide/features/kanban.md), [`website/docs/user-guide/features/plugins.md`](../website/docs/user-guide/features/plugins.md), [`website/docs/reference/cli-commands.md`](../website/docs/reference/cli-commands.md)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Opt-in config, access semantics, registration surface, and CLI reference.                                                                                                                                        |
| <a id="delta-direct-tests"></a> `delta:direct-tests`                       | Direct feature tests                              | [`tests/agent/test_workspace_registry.py`](../tests/agent/test_workspace_registry.py), [`tests/hermes_cli/test_plugins_workspace_registration.py`](../tests/hermes_cli/test_plugins_workspace_registration.py), [`tests/hermes_cli/test_kanban_workspace_provider.py`](../tests/hermes_cli/test_kanban_workspace_provider.py)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Contract, scoped registration, real discovery, lifecycle, contention, and compensation evidence.                                                                                                                 |
| <a id="delta-upstream-adjacent-tests"></a> `delta:upstream-adjacent-tests` | Rebase-adjacent lifecycle tests                   | [`tests/hermes_cli/test_plugin_manifest_v2.py`](../tests/hermes_cli/test_plugin_manifest_v2.py), [`tests/tools/test_kanban_tools.py`](../tests/tools/test_kanban_tools.py), [`tests/hermes_cli/test_kanban_dispatch_lock.py`](../tests/hermes_cli/test_kanban_dispatch_lock.py), [`tests/hermes_cli/test_kanban_worker_pid_fingerprint.py`](../tests/hermes_cli/test_kanban_worker_pid_fingerprint.py), [`tests/hermes_cli/test_kanban_terminal_worker_reaper.py`](../tests/hermes_cli/test_kanban_terminal_worker_reaper.py), [`tests/hermes_cli/test_kanban_reclaim_claim_lock_guard.py`](../tests/hermes_cli/test_kanban_reclaim_claim_lock_guard.py), assessed-upstream [`test_kanban_dispatcher_restart.py`](https://github.com/NousResearch/hermes-agent/blob/e408d363393ccb72267e67bcccf4f8954b438cd9/tests/e2e/core/kanban/test_kanban_dispatcher_restart.py), and assessed-upstream [`test_kanban_gc_retention.py`](https://github.com/NousResearch/hermes-agent/blob/e408d363393ccb72267e67bcccf4f8954b438cd9/tests/hermes_cli/test_kanban_gc_retention.py) | Load abandonment, PID adoption/restart, connection-derived exclusion, process identity/reaping/reclaim, and GC-retention contracts with which the fork must compose.                                             |

The rows above are the complete current path map, including the rebase-adjacent owners and
immutable upstream-only evidence identified by the latest assessment. Resolve overlaps by row
responsibility rather than by whether a merge is textual; use
[delta:upstream-adjacent-tests](#delta-upstream-adjacent-tests) only as compatibility evidence, not
as a claim of direct feature coverage.

### Conflict-Resolution Rules

- Follow moved symbols and responsibilities rather than recreating old facade shapes or internal
  compatibility shims.
- Keep workspace registration in upstream's current scoped-provider registration table. In
  particular, preserve any upstream plugin-load timeout/abandonment wrappers generated around that
  table, and extend registration with an exact scoped/global slot, durable compatibility ID,
  registry epoch plus per-slot generation, full-callback refcount pinning, and post-acquire CAS
  semantics.
- Adapt the split between planning and materialization to upstream's current worktree resolver, but
  never move acquisition after worktree mutation. Claim-conditionally initialize a task request
  revision before the first callback, persist its complete run snapshot, and require every admitted
  checkout to start at that SHA on an attached effective branch.
- Make the binding transaction conditional on the complete active task/run/claim identity. Keep
  materialization, spawn, dispatcher PID persistence, and worker-side PID adoption under a durable
  run-owned reservation honored by every lifecycle writer. Persist stable launch/supervisor identity
  before spawn; if a process may have started, authoritatively stop it and prove death before lease
  release.
- Implement the canonical reservation graph and historical-run admission blocker; phase recovery and
  executor takeover are CAS-fenced, while `ended_at` never substitutes for quiescence.
- Serialize DB open/acquisition, pending-authority check, board move/delete, and import publication
  with a stable parent-level lifecycle lock plus tombstone/identity revalidation. Derive the inner
  dispatch lock from the open connection's actual main DB, never an ambient board.
- Update every schema representation together: canonical DDL, migration columns, rebuild specs,
  row models, output serializers, and transfer scrubbers. Version complete new bindings, classify
  legacy cohorts without invented values, and preserve provider-visible IDs across rebuild or drain.
- Audit every new or changed terminal/reclaim path. It must retain lease authority, prove process
  death (including non-local/unprovable workers and manual reclaim/reassign), continue renewal while
  execution may live, persist one workspace-specific release outcome, and eventually enter
  idempotent release with that same outcome on every retry.
- Preserve upstream GC/retention improvements while keeping provider-managed worktrees out of core
  deletion.
- Bound and isolate all provider callbacks, pin registrations through callback drain, and schedule
  possibly-live renewal before finished release.
- Import untrusted archives as allowlisted data into a fresh private canonical DB; scrub before one
  atomic publish and never execute archive triggers or expose a partially sanitized board.
- Do not collapse task requested coordinates and run effective coordinates even if upstream changes
  workspace persistence.
- Keep `ctx.workspaces`/`ctx.workspace_tools` and their `plugin-workspace-leases` documentation
  distinct from the Kanban provider unless a deliberate adapter proves every contract invariant.
- Resolve documentation/navigation conflicts according to the current sidebar structure; the
  provider contract page must remain discoverable without restoring obsolete ordering. At the
  assessed upstream conflict, keep both `developer-guide/plugins/application-declarations`
  and `developer-guide/workspace-provider-plugin`.
- If upstream adds a similar hook, compare blocking behavior, transaction boundaries, profile scope,
  persistence, restart handling, and death-before-release semantics before replacing this feature.

### Upstream Equivalence Review

The review is row-for-row with the ledger; the status text below references rather than restates
each contract. [delta:upstream-adjacent-tests](#delta-upstream-adjacent-tests) supplies compatibility
evidence for every row it touches, while the ledger remains canonical for coverage.

| Ledger row                                                  | Equivalence status at `e408d36339`                                                                                 | Inventory evidence to reconcile                                                                                                                                                                                                                        |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [req:selection-scope](#req-selection-scope)                 | Retain — no equivalent found; use the row's upstream disposition.                                                  | [delta:selection-defaults](#delta-selection-defaults), [delta:plugin-registration](#delta-plugin-registration)                                                                                                                                         |
| [req:registration-lifetime](#req-registration-lifetime)     | Retain — adjacent registry behavior is not lease-binding equivalence.                                              | [delta:contract-registry](#delta-contract-registry), [delta:plugin-registration](#delta-plugin-registration), [delta:task-run-schema](#delta-task-run-schema)                                                                                          |
| [req:request-base](#req-request-base)                       | Retain — no equivalent found; unresolved gap status remains in the ledger.                                         | [delta:task-run-schema](#delta-task-run-schema), [delta:worktree-planning](#delta-worktree-planning), [delta:dispatch-reconciliation](#delta-dispatch-reconciliation)                                                                                  |
| [req:canonical-lease](#req-canonical-lease)                 | Retain — no equivalent found; unresolved gap status remains in the ledger.                                         | [delta:contract-registry](#delta-contract-registry), [delta:worktree-planning](#delta-worktree-planning), [delta:dispatch-reconciliation](#delta-dispatch-reconciliation)                                                                              |
| [req:ordering-reservations](#req-ordering-reservations)     | Retain — adjacent dispatcher/process work requires adaptation, not retirement.                                     | [delta:task-run-schema](#delta-task-run-schema), [delta:dispatch-reconciliation](#delta-dispatch-reconciliation), [delta:process-supervision](#delta-process-supervision)                                                                              |
| [req:contention-failure](#req-contention-failure)           | Retain — no provider-contention equivalent found; implementation is conforming with ledger-owned coverage backlog. | [delta:dispatch-reconciliation](#delta-dispatch-reconciliation)                                                                                                                                                                                        |
| [req:process-renewal-release](#req-process-renewal-release) | Retain — upstream lifecycle adjacency does not carry provider authority; unresolved gaps remain in the ledger.     | [delta:dispatch-reconciliation](#delta-dispatch-reconciliation), [delta:process-supervision](#delta-process-supervision), [delta:upstream-adjacent-tests](#delta-upstream-adjacent-tests)                                                              |
| [req:cleanup-destructive](#req-cleanup-destructive)         | Retain — upstream cleanup/GC behavior must be composed with, not treated as equivalent.                            | [delta:worktree-planning](#delta-worktree-planning), [delta:destructive-transfer](#delta-destructive-transfer), [delta:upstream-adjacent-tests](#delta-upstream-adjacent-tests)                                                                        |
| [req:migration-transfer](#req-migration-transfer)           | Retain — no binding migration/portable-authority equivalent found.                                                 | [delta:task-run-schema](#delta-task-run-schema), [delta:destructive-transfer](#delta-destructive-transfer)                                                                                                                                             |
| [req:access-surfaces](#req-access-surfaces)                 | Retain — no workspace-provider access surface found; unresolved task-graph gap remains in the ledger.              | [delta:cli-surface](#delta-cli-surface), [delta:task-graph](#delta-task-graph), [delta:agent-tool](#delta-agent-tool), [delta:dashboard-api](#delta-dashboard-api), [delta:developer-docs](#delta-developer-docs), [delta:user-docs](#delta-user-docs) |

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

- [req:selection-scope](#req-selection-scope) uses real discovery plus two isolated home scopes; the
  fixture retains conflicting same-name providers so scope-local unload and ambient-scope changes
  are observable.
- [req:contention-failure](#req-contention-failure) uses a provider call log and spawn/worktree
  sentinels so busy, invalid-return compensation, and spawn failure can be distinguished.
- [req:process-renewal-release](#req-process-renewal-release) uses persisted run state and controlled
  liveness transitions to observe renewal, delayed release, and host-local loss handling.

### Required Coverage Backlog

- No-provider worktree dispatch is behaviorally identical after the plan/materialize split:
  [missing:no-provider-worktree-compatibility](#missing-no-provider-worktree-compatibility)
  ([req:selection-scope](#req-selection-scope))
- Selected providers are never called for scratch or dir workspaces:
  [missing:non-worktree-provider-inert](#missing-non-worktree-provider-inert)
  ([req:selection-scope](#req-selection-scope))
- Missing or unavailable configured providers and a raising `try_acquire()` fail closed before Git
  mutation/spawn with their documented failure accounting:
  [missing:missing-unavailable-provider-fails-closed](#missing-missing-unavailable-provider-fails-closed)
  ([req:selection-scope](#req-selection-scope))
- Acquire, renew, and release callbacks are proven to execute outside write transactions:
  [missing:callbacks-outside-write-transactions](#missing-callbacks-outside-write-transactions)
  ([req:ordering-reservations](#req-ordering-reservations))
- A→B→A scope tests give both profiles conflicting home/config/secret/terminal values, assert every
  acquire/renew/release callback observes the acquiring A scope, and assert the caller's outer B
  scope is restored after each callback returns:
  [missing:callback-profile-runtime-scope](#missing-callback-profile-runtime-scope)
  ([req:selection-scope](#req-selection-scope))
- On the successful path, acquisition and durable binding precede both worktree mutation and the
  spawn callback:
  [missing:successful-acquire-precedes-materialize-and-spawn](#missing-successful-acquire-precedes-materialize-and-spawn)
  ([req:ordering-reservations](#req-ordering-reservations))
- A provider-name config switch mid-run cannot redirect renew or release:
  [missing:config-switch-preserves-provider-binding](#missing-config-switch-preserves-provider-binding)
  ([req:registration-lifetime](#req-registration-lifetime))
- Scoped/global shadowing, unload/restoration, and same-slot hot reload cannot redirect a lease to an
  incompatible same-name registration; persisted slot, compatibility ID, and generation resolve
  exactly. The registration fixture also exercises the additive optional keyword: omission keeps
  installed code registerable/unloadable and marks it legacy-unversioned, strict acquisition fails
  with actionable setup guidance until an explicit ID or declared versioned legacy policy exists,
  and adding the keyword is the migration to new leases. A successor with the same explicit
  compatibility ID accepts every outstanding callback through drain:
  [missing:provider-resolution-tier-binding](#missing-provider-resolution-tier-binding)
  ([req:registration-lifetime](#req-registration-lifetime))
- An unload or replacement racing `try_acquire()` cannot make binding describe a different provider:
  the registration is pinned or a post-acquire slot/generation CAS loses and the original instance
  receives compensation:
  [missing:provider-acquisition-generation-race](#missing-provider-acquisition-generation-race)
  ([req:registration-lifetime](#req-registration-lifetime))
- Explicit compatibility IDs reject empty values and remain public diagnostics, omitted IDs retain
  the legacy-unversioned marker, epoch/generation exact lookup never crosses a restart, and every
  callback holds a refcount pin while unload waits for drain:
  [missing:provider-callback-registration-pinning](#missing-provider-callback-registration-pinning)
  ([req:registration-lifetime](#req-registration-lifetime))
- Acquisition and core materialization use the original checked-out commit; existing requested and
  alternate checkouts have that `HEAD` at admission, while retries begin from the original request
  base retained by the task and copied into each run unless a supported lifecycle operation
  deliberately updates it:
  [missing:workspace-request-pins-base-ref](#missing-workspace-request-pins-base-ref)
  ([req:request-base](#req-request-base))
- The run persists every immutable request field—including owner, acquisition attempt, revision,
  workspace kind, and project ID—and restart reconstruction never joins mutable task values:
  [missing:run-persists-complete-workspace-request](#missing-run-persists-complete-workspace-request)
  ([req:request-base](#req-request-base))
- Lease return type, non-empty ID, absolute/core-target path, alternate Git checkout, `None` branch
  inheritance, and attached-branch matching are covered:
  [missing:workspace-lease-validation](#missing-workspace-lease-validation)
  ([req:canonical-lease](#req-canonical-lease))
- Validation produces one canonical lease for persistence and every later callback; compensation
  receives the original provider lease only when validation fails before canonicalization:
  [missing:canonical-workspace-lease-callbacks](#missing-canonical-workspace-lease-callbacks)
  ([req:canonical-lease](#req-canonical-lease))
- Detached HEAD is rejected for core-target reuse and alternate provider checkouts:
  [missing:detached-head-lease-rejected](#missing-detached-head-lease-rejected)
  ([req:canonical-lease](#req-canonical-lease))
- An alternate provider checkout remains run-effective only: task/requested path and branch stay
  unchanged, persisted requested/effective values differ as expected, and a later retry starts from
  the original request:
  [missing:alternate-checkout-preserves-requested-coordinates](#missing-alternate-checkout-preserves-requested-coordinates)
  ([req:canonical-lease](#req-canonical-lease))
- Conditional binding plus the initial reservation and `workspace_acquired` event are atomic against
  the complete task/run/claim identity, and a persistence failure compensates exactly once:
  [missing:acquire-persistence-compensation](#missing-acquire-persistence-compensation)
  ([req:ordering-reservations](#req-ordering-reservations))
- After acquisition, a concurrent run end/replacement before materialization, spawn, or PID
  attachment records cancellation/terminal intent without erasing the run-owned reservation; no
  stale checkout mutation continues, and any possibly started process is authoritatively stopped
  with death proven before release:
  [missing:acquired-run-remains-active-through-spawn](#missing-acquired-run-remains-active-through-spawn)
  ([req:ordering-reservations](#req-ordering-reservations))
- Every legal reservation transition, cancellation precedence, timed-out executor takeover, crash
  recovery action, and `ended_at`/quiescence distinction is covered; a historical non-quiescent or
  unreleased reservation blocks successor admission:
  [missing:workspace-reservation-state-machine](#missing-workspace-reservation-state-machine)
  ([req:ordering-reservations](#req-ordering-reservations))
- Busy/error/timeout and compensation dispositions fence the original
  task/run/claim/revision/reservation. The acquisition-attempt ID survives restart, provider replay
  is deduplicated, and a late lease return blocks admission until it is durably recorded and
  idempotently compensated through the pinned issuer; it cannot move or clear a successor:
  [missing:prebind-dispositions-fence-original-claim](#missing-prebind-dispositions-fence-original-claim)
  ([req:ordering-reservations](#req-ordering-reservations))
- Busy deferral restores both ready and review source lanes, clears `current_run_id` and the claim,
  closes the run as `workspace_deferred`, emits the matching event/result, and leaves
  `consecutive_failures`, `last_failure_error`, and infrastructure cooldown unchanged:
  [missing:busy-restores-source-lane-without-failure-state](#missing-busy-restores-source-lane-without-failure-state)
  ([req:contention-failure](#req-contention-failure))
- Renewal exceptions/missing providers count as loss, while a later false availability probe does
  not prevent callback delivery to a still-registered captured provider:
  [missing:renewal-provider-failure-semantics](#missing-renewal-provider-failure-semantics)
  ([req:process-renewal-release](#req-process-renewal-release))
- All provider callbacks have core deadlines and isolated capacity; a hung acquire/renew/release
  holds no board lock, active renewals run before finished releases, and one hung lease cannot starve
  another. Timeout retains callback pin/in-flight identity, and late acquire completion follows the
  durable compensation path rather than becoming launch authority:
  [missing:workspace-callback-deadlines-and-isolation](#missing-workspace-callback-deadlines-and-isolation)
  ([req:process-renewal-release](#req-process-renewal-release))
- Ended but unreleased runs continue renewing while reservation, PID/fingerprint, or non-local
  evidence means execution may still be alive; renewal loss reaches that run's recorded supervisor
  even when it is ended, superseded, or no longer `current_run_id`:
  [missing:ended-live-leases-renew-until-death](#missing-ended-live-leases-renew-until-death)
  ([req:process-renewal-release](#req-process-renewal-release))
- A workspace provider that attempts late registration after its plugin load timed out is ignored,
  and every registration completed before timeout is cleaned up:
  [missing:workspace-provider-load-timeout-abandonment](#missing-workspace-provider-load-timeout-abandonment)
  ([req:registration-lifetime](#req-registration-lifetime))
- A worker that survives termination after renewal loss retains its claim and blocks a successor:
  [missing:renewal-loss-survivor-holds-claim](#missing-renewal-loss-survivor-holds-claim)
  ([req:process-renewal-release](#req-process-renewal-release))
- A non-local or otherwise unsignalable worker with a lost lease stays claimed until its owning
  supervisor proves death:
  [missing:nonlocal-renewal-loss-holds-claim](#missing-nonlocal-renewal-loss-holds-claim)
  ([req:process-renewal-release](#req-process-renewal-release))
- Generic timeout/stale/orphan paths plus explicit reclaim and reassign-with-reclaim retain claim,
  process evidence, and lease for every non-local or otherwise unprovable worker until authoritative
  death:
  [missing:all-reclaim-paths-require-authoritative-death](#missing-all-reclaim-paths-require-authoritative-death)
  ([req:process-renewal-release](#req-process-renewal-release))
- Lease-loss requeue leaves `consecutive_failures`, `last_failure_error`, maximum-retry state, and
  infrastructure cooldown untouched while emitting the lease-loss outcome/event:
  [missing:lease-loss-does-not-spend-failure-budget](#missing-lease-loss-does-not-spend-failure-budget)
  ([req:process-renewal-release](#req-process-renewal-release))
- Failure to persist a PID after successful spawn routes the stable launch to its supervisor, stops
  the exact process, and proves death before release:
  [missing:spawned-process-persistence-failure-proves-death](#missing-spawned-process-persistence-failure-proves-death)
  ([req:ordering-reservations](#req-ordering-reservations))
- An abrupt crash between spawn and PID attachment leaves a durable launch ID/reservation that is
  reconciled as possibly live; null PID alone can neither clear the claim nor permit release:
  [missing:durable-launch-intent-before-spawn](#missing-durable-launch-intent-before-spawn)
  ([req:ordering-reservations](#req-ordering-reservations))
- Provider release failure remains pending and succeeds on a later tick/restart:
  [missing:release-failure-retries](#missing-release-failure-retries)
  ([req:process-renewal-release](#req-process-renewal-release))
- First release and every retry use the same write-once `workspace_release_outcome`, including after
  a setup failure whose run outcome uses a different label:
  [missing:durable-workspace-release-outcome](#missing-durable-workspace-release-outcome)
  ([req:process-renewal-release](#req-process-renewal-release))
- Completion, block, request-review, request-changes, schedule/park, archive, descendant
  invalidation, crash, timeout, stale-claim, manual reclaim, reassign-with-reclaim, orphan, and
  restart paths each prove death before release:
  [missing:terminal-paths-release-after-death](#missing-terminal-paths-release-after-death)
  ([req:process-renewal-release](#req-process-renewal-release))
- Ordinary ready re-claim plus unblock/review-reopen dangling-run recovery retain callback and
  process evidence until death is proven:
  [missing:dangling-run-recovery-proves-death](#missing-dangling-run-recovery-proves-death)
  ([req:process-renewal-release](#req-process-renewal-release))
- Completion/deferred-parent cleanup, GC, and dashboard HTTP 409 all preserve provider-owned
  checkout or pending release authority:
  [missing:provider-cleanup-and-delete-guards](#missing-provider-cleanup-and-delete-guards)
  ([req:cleanup-destructive](#req-cleanup-destructive))
- A provider-bound run followed by a provider-disabled run leaves the new core-owned worktree
  eligible for normal safe cleanup:
  [missing:provider-to-core-cleanup-ownership](#missing-provider-to-core-cleanup-ownership)
  ([req:cleanup-destructive](#req-cleanup-destructive))
- Pending lease rows block board removal, and board transfer scrubs only snapshot authority:
  [missing:board-removal-transfer-authority](#missing-board-removal-transfer-authority)
  ([req:cleanup-destructive](#req-cleanup-destructive))
- The pending-authority check and board archive/delete share a stable lifecycle reservation with
  every pre-binding acquisition. A late callback revalidates the tombstone and compensates its
  returned lease through the pinned issuer; it cannot persist a usable binding after removal:
  [missing:board-removal-serialized-with-acquire](#missing-board-removal-serialized-with-acquire)
  ([req:cleanup-destructive](#req-cleanup-destructive))
- Dispatch locking follows the actual open connection's resolved main DB even when ambient board
  selection conflicts:
  [missing:dispatch-lock-follows-open-connection](#missing-dispatch-lock-follows-open-connection)
  ([req:cleanup-destructive](#req-cleanup-destructive))
- Fresh, migrated, and rebuilt databases preserve all task/run fields and conservative defaults:
  [missing:workspace-lease-schema-migration](#missing-workspace-lease-schema-migration)
  ([req:migration-transfer](#req-migration-transfer))
- Seeded unbound, released-historical, active legacy, and ended-unreleased legacy rows exercise
  version/completeness migration, declared mapping, pre-upgrade drain, and fail-closed quarantine:
  [missing:legacy-unreleased-binding-quarantine](#missing-legacy-unreleased-binding-quarantine)
  ([req:migration-transfer](#req-migration-transfer))
- TEXT-primary-key rebuild preserves provider-visible task/run IDs for live authority or refuses
  until drain, and atomically remaps every task/event/current-run reference where remap is permitted:
  [missing:workspace-binding-stable-ids-on-rebuild](#missing-workspace-binding-stable-ids-on-rebuild)
  ([req:migration-transfer](#req-migration-transfer))
- Exported snapshots scrub task paths, workspace events, relocated base/revision data, and every
  current/future authority field without mutating the live source:
  [missing:transfer-scrubs-all-workspace-authority](#missing-transfer-scrubs-all-workspace-authority)
  ([req:migration-transfer](#req-migration-transfer))
- Import reserves its slug under the stable lifecycle lock, sanitizes entirely in private staging,
  atomically publishes once complete, and leaves no partial board or reservation on failure:
  [missing:import-private-sanitize-before-publish](#missing-import-private-sanitize-before-publish)
  ([req:migration-transfer](#req-migration-transfer))
- A hostile archive schema/trigger cannot run during import; only validated, allowlisted portable
  rows enter a fresh canonical trigger-free DB:
  [missing:import-allowlisted-canonical-schema](#missing-import-allowlisted-canonical-schema)
  ([req:migration-transfer](#req-migration-transfer))
- A complete persisted request plus canonical lease round-trips through restart and reconstructs the
  same callback semantics, including owner, acquisition attempt, immutable base, exact registry
  slot/compatibility ID/generation, requested and effective coordinates, expiry, launch authority,
  and release outcome:
  [missing:persisted-request-lease-round-trip](#missing-persisted-request-lease-round-trip)
  ([req:request-base](#req-request-base))
- A decomposed child normalizes/validates access, rejects arbitrary workspace kinds, and cannot
  retain or select `read` access for a scratch/dir workspace:
  [missing:decomposed-non-worktree-read-rejected](#missing-decomposed-non-worktree-read-rejected)
  ([req:access-surfaces](#req-access-surfaces))
- CLI, agent tool, dashboard API, output, and valid decomposed-child access propagation agree:
  [missing:workspace-access-surface-propagation](#missing-workspace-access-surface-propagation)
  ([req:access-surfaces](#req-access-surfaces))

### Verification Commands

```bash
scripts/run_tests.sh \
  tests/agent/test_workspace_registry.py \
  tests/hermes_cli/test_plugins_workspace_registration.py \
  tests/hermes_cli/test_kanban_workspace_provider.py
```

## Test Generation Notes

Use a real temporary Git repository, a temporary `HERMES_HOME`, real plugin discovery, and
a real Kanban database. For profile behavior, use two homes and exercise A→B→A so a same-named
provider in B cannot receive A's callback. Record call order in the provider fixture and assert on
observable task/run/event/process state rather than source layout.

Generate tests around these boundaries:

- [req:selection-scope](#req-selection-scope) and
  [req:contention-failure](#req-contention-failure): no provider, missing provider, unavailable
  provider, `try_acquire()` exception, and ordinary
  contention from both ready and review lanes with complete run/event/failure/cooldown assertions;
- [req:request-base](#req-request-base) and [req:canonical-lease](#req-canonical-lease): requested
  core target versus an existing provider-owned checkout, including wrong branch,
  non-Git paths, inherited lease branch, detached HEAD, requested/effective separation, and exact
  original-base pinning; wrong-repository rejection belongs in the trusted provider's conformance
  suite because core does not validate repository identity;
- [req:ordering-reservations](#req-ordering-reservations): failure before persistence, after
  persistence, during materialization, and during spawn, plus a concurrent run end/replacement
  before materialization, spawn, and conditional PID attachment that preserves and cancels the
  durable run reservation;
- [req:process-renewal-release](#req-process-renewal-release): completion with a still-retained PID,
  continued renewal of that ended live run, release
  failure/retry, restart, and missing-exact-provider recovery;
- [gap:nonlocal-renewal-loss-holds-claim](#gap-nonlocal-renewal-loss-holds-claim): renewal
  false/exception with a dead host-local process, a survivor, and a non-local owner, including
  assertions that lease loss never consumes failure budget or cooldown;
- [gap:durable-launch-intent-before-spawn](#gap-durable-launch-intent-before-spawn): successful
  spawn followed by PID/fingerprint persistence exception or lost conditional update, plus abrupt
  dispatcher death in that window, with the stable launch recovered and the exact process
  authoritatively stopped/dead before release;
- [gap:all-reclaim-paths-require-authoritative-death](#gap-all-reclaim-paths-require-authoritative-death):
  every generic Kanban completion/block/review-handoff/schedule/archive/descendant-invalidation/
  crash/timeout/automatic reclaim/manual reclaim/reassign path with a provider-bound run, including
  ordinary ready re-claim and unblock/review-reopen dangling-run recovery;
- [req:migration-transfer](#req-migration-transfer) and [req:request-base](#req-request-base):
  old-schema migration and table rebuild, not only fresh database creation, followed by a restart
  round-trip that reconstructs every request, canonical lease, exact slot/compatibility/generation,
  original base, launch reservation, and write-once release-outcome field;
- [req:cleanup-destructive](#req-cleanup-destructive) and
  [req:migration-transfer](#req-migration-transfer): cleanup, GC, deletion, board removal racing
  acquisition under the dispatch lock, export/import, and config switch behavior;
- [req:registration-lifetime](#req-registration-lifetime): scoped/global provider shadowing,
  unload/restore, incompatible same-slot replacement, and declared lease-compatible hot reload
  through complete lease drain;
- [gap:provider-to-core-cleanup-ownership](#gap-provider-to-core-cleanup-ownership):
  provider→no-provider retries on the same task, with ownership/cleanup assessed per run/path; and
- [req:access-surfaces](#req-access-surfaces): all access-input/output surfaces, including
  whitespace/case normalization, invalid workspace kind, and invalid `read` on non-worktree
  decomposed children.

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
- 2026-09-28 —
  [`upstream e408d36339`](https://github.com/NousResearch/hermes-agent/commit/e408d363393ccb72267e67bcccf4f8954b438cd9)
  — 5,698 commits after the implementation base, no equivalent lifecycle found, and the merge-tree
  assessment still has one [delta:developer-docs](#delta-developer-docs) navigation conflict;
  refresh semantic hotspots and expand the contract/gap inventory.
- 2026-09-28 — fork assessment commit
  [`8e9fc477c9`](https://github.com/dkropachev/hermes/commit/8e9fc477c9e0e99bceee7a3797ab764d17a0498b)
  includes [#11](https://github.com/dkropachev/hermes/pull/11) and
  [#13](https://github.com/dkropachev/hermes/pull/13), whose distinct
  `ctx.workspaces`/`ctx.workspace_tools` APIs remain adjacent rather than equivalent to Kanban
  provider admission.
