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

This is the Kanban checkout-provider interface, not the newer
[`ctx.workspaces` durable lease API](./plugin-workspace-leases.md). A Kanban workspace provider
coordinates user-selected task repositories and branches; `ctx.workspaces` gives a native plugin
exclusive ownership of its own host-managed work directory across restarts. The APIs have separate
ownership and lifecycle contracts and are not interchangeable.

## Create, validate, and enable a provider

A native directory plugin needs a manifest beside the Python module that registers the provider:

```text
~/.hermes/plugins/git-workspace-leases/
├── plugin.yaml
└── __init__.py
```

```yaml title="~/.hermes/plugins/git-workspace-leases/plugin.yaml"
name: git-workspace-leases
version: 0.1.0
description: Coordinate reader and writer leases for Kanban Git workspaces
kind: standalone
```

The `__init__.py` skeleton below shows the registration and callback shape; it is not a runnable
provider. Replace its `my_store = ...` sentinel with a user-supplied, durable, process-safe store
adapter before enabling it. The adapter must implement the illustrated acquire, renew, and release
operations. Its returned allocation object's `public_id` and `expires_at` attributes are placeholders
for the adapter's own stable, non-secret lease identifier and expiry representation.

`hermes plugins doctor` imports the module and calls `register()` through the real manifest,
discovery, and registration path. It does **not** call `is_available`, exercise the store, or test
acquire, renew, and release. Test those operations and their failure/retry paths separately, then use
`doctor` to check the completed plugin's import and registration wiring before enabling it:

:::warning Native plugins are fully trusted code
`hermes plugins doctor` is not a static linter: it imports and executes the plugin in-process as the
current OS user. Enabling the plugin permits the same execution when Hermes discovers it in later
sessions. Review the code and dependencies first.

Profiles scope routing, configuration, secrets, and terminal work; they are not process sandboxes or
hostile-tenant isolation, and provider lookup has a same-name global fallback. During a provider
callback, core re-enters the captured profile's home, secret, and terminal scope. Resolve
`get_hermes_home()`, `get_secret()`, and configuration at callback time, or key caches by Hermes
home. Do not freeze profile state from `os.environ` at import time or start bare background threads
that outlive the callback without its scope and lifecycle. Credentials belong only in the secret
store or profile `.env`; lease IDs, request IDs, config, and logs must remain non-secret.
:::

```bash
hermes plugins doctor git-workspace-leases --ci
hermes plugins enable git-workspace-leases
hermes config set kanban.workspace_provider git-workspace-leases
```

See [Build a Hermes Plugin](./plugins/index.md) for Git installation, packaging, dependencies, and
the rest of the native plugin contract.

There are two deliberately separate names in this setup. `plugins.enabled` contains the general
plugin's canonical ID (the ID printed by `hermes plugins list`; for this flat example it is also the
manifest `name`). `kanban.workspace_provider` is matched against the Python provider's `name`. The
two happen to be `git-workspace-leases` above, but a differently named plugin must use its plugin ID
for enablement and its `WorkspaceProvider.name` for selection.

The equivalent configuration is:

```yaml title="~/.hermes/config.yaml"
plugins:
  enabled:
    - git-workspace-leases

kanban:
  workspace_provider: git-workspace-leases
```

Profiles are isolated. Install or place the plugin in every profile home from which a dispatcher
can run, then enable and select it in that same profile. For example:

```bash
hermes -p orchestrator plugins doctor git-workspace-leases --ci
hermes -p orchestrator plugins enable git-workspace-leases
hermes -p orchestrator config set kanban.workspace_provider git-workspace-leases

hermes -p reviewer plugins doctor git-workspace-leases --ci
hermes -p reviewer plugins enable git-workspace-leases
hermes -p reviewer config set kanban.workspace_provider git-workspace-leases
```

Repeat those profile-scoped operations for every profile that can run a dispatcher. Enabling a
plugin takes effect in the next session, so restart an already-running gateway after changing its
plugin or provider configuration.

:::warning
An installed provider is inert until the current profile's `kanban.workspace_provider` names it. A
profile with that setting omitted or empty intentionally retains Hermes' built-in worktree behavior,
without provider coordination, even when another profile selects the provider. A non-empty selection
that is missing or unavailable fails closed instead of silently starting an unprotected worker.
:::

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

`read` and `write` are coordination metadata consumed only when a workspace provider is configured.
Hermes does not make a `read` checkout filesystem-read-only or itself prevent commits or pushes. A
read checkout may remain writable locally so compilers and tests can create artifacts; the provider
or a publication broker must implement any required publication fencing. Missing access defaults
conservatively to `write`.

## Provider contract

```python title="~/.hermes/plugins/git-workspace-leases/__init__.py"
from agent.workspace_provider import WorkspaceLease, WorkspaceProvider


# Skeleton sentinel: replace with your durable, process-safe store adapter.
my_store = ...


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
        issued = my_store.try_acquire(
            repo=request.repo_root,
            owner=self._owner(request),
            access=request.access,
        )
        if issued is None:
            return None  # contention: defer without spending task retry budget
        return WorkspaceLease(
            lease_id=issued.public_id,      # adapter-defined stable ID, never a bearer secret
            path=request.requested_path,    # core materializes this target after acquire
            branch_name=request.branch_name,
            expires_at=issued.expires_at,
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

Use `lease_id` as the durable lookup key and the request's non-secret owner identity as a fencing
check—not equality of the complete `WorkspaceLease` object. The `_owner(request)` tuple above is
only the best-effort identity available from the current runtime, not a stable durable principal:
core reconstructs `owner_id` from mutable `task_runs.claim_lock`, and dangling-run recovery can
clear that value before a later callback. On an ordinary callback, require the recorded owner to
match. Make `release` idempotent, but do not silently accept an empty or different owner. If legacy
or recovery handling must resolve such a callback, use an explicit, versioned mapping for that known
cohort or quarantine it for controlled recovery instead of releasing whichever lease shares an ID.
Persisting the complete immutable request, including its owner, is tracked in the feature spec's
[Known Implementation Gaps](https://github.com/dkropachev/hermes/blob/main/feature-specs/kanban-workspace-provider.md#known-implementation-gaps).

On the normal path, the current core persists a canonical absolute lease path and the effective
branch (including an inherited requested branch), then reconstructs a new `WorkspaceLease` with
those values for later renewals and releases. A best-effort compensation after early validation or
durable-binding failure can instead receive the original object returned by `try_acquire`. Providers
must tolerate both representations and must not require object, path-string, or full-dataclass
equality to find the durable lease.

`task_id`, `run_id`, `owner_id`, `lease_id`, board and filesystem paths, and the planned
`lease_compatibility_id` are coordination and fencing metadata, not authentication credentials or
bearer tokens. The current runtime neither accepts nor persists `lease_compatibility_id`; do not pass
it to the current registration API. When that registration keyword is introduced, it must be an
optional, additive source-compatible extension so existing plugins still import and register. An
unversioned registration must nevertheless be upgraded explicitly before it can make strict new
acquisitions. Its outstanding legacy leases must first drain under the old contract, resolve through
an explicit provider-declared versioned mapping, or remain in fail-closed quarantine; omission must
not silently claim compatibility. Paths can disclose usernames, profile layout, and repository
names. A remote provider should translate them to opaque local identifiers and avoid exporting or
logging raw paths and request identifiers. Authentication and authorization belong to the provider's
transport and credential configuration, separately from these fields.

For an alternate provider-owned path, current core validation checks only that the path is absolute,
exists as a directory, is a Git checkout, and does not report a named branch conflicting with the
effective branch. It does not prove canonical repository identity, an admitted base SHA, containment
in provider-owned storage, symlink/private-ownership policy, or the authenticity and lease binding
of a remote coordinator response. A detached HEAD reports no current branch, so it is accepted.
Before returning an alternate path, the provider must canonicalize it, enforce its containment and
symlink policy, prove that it is a private checkout of `request.repo_root`, verify the effective
named branch and its admitted base, and authenticate and validate any remote response. The current
request has no base SHA, so a provider that requires exact-base admission must select, pin, and
verify that base within acquisition. Core-side fixes are tracked in the feature spec's
[Known Implementation Gaps](https://github.com/dkropachev/hermes/blob/main/feature-specs/kanban-workspace-provider.md#known-implementation-gaps).

`try_acquire` must not wait for another task's entire lifetime. Return `None` when busy so the
dispatcher can requeue the card. Bounded work needed to materialize an already-granted checkout is
fine. `renew` returns `False` after ownership is lost. `release` must be idempotent because crash and
restart reconciliation may retry it.

The current runtime invokes `is_available`, `try_acquire`, `renew`, and `release` inline, without
core-enforced deadlines or isolated callback capacity. Keep `is_available` cheap and local-only, and
put finite timeouts around every provider and transport operation in all four callbacks. Each
callback must finish its bounded side effects before returning rather than launch detached work that
continues afterward. A hung callback can delay dispatch and time-sensitive renewal of unrelated
leases. Core deadlines and callback isolation are tracked in the feature spec's
[Known Implementation Gaps](https://github.com/dkropachev/hermes/blob/main/feature-specs/kanban-workspace-provider.md#known-implementation-gaps).

## Lifecycle

For a selected provider Hermes performs this sequence:

1. Atomically claim the task and open a run.
2. Resolve the built-in worktree path.
3. Call `is_available()` immediately before acquisition. A false result or exception fails closed.
4. Call `try_acquire` before spawning any worker process.
5. Persist provider, lease, path, branch, repository, expiry, and access on `task_runs`.
6. Materialize the planned core target, or validate an alternate provider-owned Git checkout and
   branch.
7. Spawn the worker in the effective path.
8. Renew from dispatcher reconciliation while the run remains unended. Provider TTL should exceed
   several dispatch intervals.
9. If process spawn fails before a child PID is retained, release immediately.
10. For an ended run, release once its persisted worker PID is `NULL`. A failed release stays pending
    and is retried by later dispatcher ticks.

The availability check gates acquisition only. Renewal and release follow the persisted binding and
do not consult `is_available()` again, so Hermes still attempts renewal and delivers the release
cleanup callback even if the provider would report a degraded state. Providers must handle those
bounded callbacks in degraded mode rather than use availability as a reason to suppress cleanup.

The current reconciliation queries leave safety gaps around those last two steps. An ended run with
a retained live worker stops renewing even though release is still deferred. Conversely, spawn/PID
persistence races and dangling-run recovery can leave an ended provider-bound run with a `NULL`
persisted PID while a worker may still execute, making it eligible for release without authoritative
proof of death. Providers should retain their own expiry and publication fencing; do not interpret a
release callback as proof that the old worker stopped. These defects are tracked in the feature
spec's [Known Implementation Gaps](https://github.com/dkropachev/hermes/blob/main/feature-specs/kanban-workspace-provider.md#known-implementation-gaps).

Release outcomes identify where startup stopped. If `try_acquire` returned a `WorkspaceLease` but
lease validation or durable binding fails, Hermes makes a best-effort compensating `release` call
with `outcome="acquire_failed"`. Once the binding is persisted, a materialization or checkout
validation failure releases it with `outcome="workspace_failed"`. A process launch failure with no
retained child releases it with `outcome="spawn_failed"`. These startup values are examples, not an
exhaustive enum: normal terminal outcomes and future core versions may supply other strings. Treat
`outcome` as advisory context, and accept unknown values rather than refusing or delaying release.

The current runtime also does not keep that advisory outcome stable across delivery attempts. If an
immediate release with `outcome="workspace_failed"` fails, ended-run reconciliation can retry the
same lease with `outcome="spawn_failed"`. Key idempotence on the lease ID and fenced owner, and do
not reject a retry solely because its advisory outcome differs. The required future contract is a
workspace-specific release outcome selected and persisted once, then reused for every delivery; the
current drift is tracked in the feature spec's
[Known Implementation Gaps](https://github.com/dkropachev/hermes/blob/main/feature-specs/kanban-workspace-provider.md#known-implementation-gaps).

Provider callbacks run outside Kanban SQLite write transactions. The persisted provider name and
home scope—not the current config value—drive renewal and release, but they do **not** capture provider
identity. Each callback resolves whichever provider currently occupies that name and scope. Do not
mix a scoped provider and a directly registered global provider under the same name: unloading a
scoped provider can redirect an old lease to the global fallback, while adding one can shadow the
global provider that acquired it. Unload/reload or same-slot replacement can likewise redirect old
leases. A provider with outstanding leases must not be replaced unless its successor explicitly
accepts leases created by the previous implementation. Exact registry-tier and generation binding
is tracked in the feature spec's [Known Implementation Gaps](https://github.com/dkropachev/hermes/blob/main/feature-specs/kanban-workspace-provider.md#known-implementation-gaps).

Hermes keeps the task's requested repository/path unchanged. Provider-returned effective paths live
on the run only, so a later review or retry resolves from the original repository after the old
checkout is removed. For a provider-managed run, the provider owns its effective checkout cleanup;
core must not delete that provider path during task completion or Kanban GC.

Current cleanup suppression is coarser than that per-run contract: any historical provider-bound run
causes completion and GC to skip worktree cleanup for the entire task. If a later run uses built-in
workspace handling, its core-managed worktree may therefore be retained even though no provider owns
that path. Per-run/path cleanup ownership is tracked in the feature spec's
[Known Implementation Gaps](https://github.com/dkropachev/hermes/blob/main/feature-specs/kanban-workspace-provider.md#known-implementation-gaps).

A pending provider lease refuses hard task deletion and either form of board removal. Archiving the
task itself remains allowed; wait for the worker to stop and for a successful provider release (or a
later dispatcher retry) before hard-deleting the task or removing its board. The current board
removal check is not yet serialized with an in-flight acquisition, so it can race a lease being
persisted; do not treat the refusal as a complete lifecycle fence. That race is tracked in the
feature spec's [Known Implementation Gaps](https://github.com/dkropachev/hermes/blob/main/feature-specs/kanban-workspace-provider.md#known-implementation-gaps).

## Provider responsibilities

Hermes supplies lifecycle ordering, not a lock algorithm. Production providers should implement:

- durable reader/writer admission with writer fairness;
- identity-safe renewal and release checked against both lease ID and owner identity;
- TTL and startup recovery for a host that stays down;
- one private checkout per lease, belonging to `request.repo_root` and the effective branch;
- an exact base-commit pin inside provider allocation when policy requires it (return a
  provider-owned checkout, because the current request does not carry a base SHA);
- checkout cleanup plus quarantine/retry when cleanup fails;
- fencing at publication time so an expired writer cannot push after a successor starts.

Git worktree locking is insufficient: `git worktree lock` prevents removal of one checkout but does
not exclude other readers or writers. Likewise, this provider only coordinates Hermes participants;
branch protection remains necessary for humans and external automation.
