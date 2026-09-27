---
title: Durable Plugin Workspace Leases
description: Reserve private, profile-scoped work directories across plugin worker restarts
---

# Durable Plugin Workspace Leases

Trusted native plugins can use `ctx.workspaces` to reserve a private work directory across
process and dashboard restarts. Hermes records lease ownership durably, fences stale handles,
and conservatively preserves work that it cannot prove safe to delete.

This is a host lifecycle service, not the Kanban
[workspace-provider interface](./workspace-provider-plugin.md). A workspace provider coordinates
Kanban task checkouts chosen by the user. `ctx.workspaces` gives one plugin exclusive ownership of
its own host-managed directory, which is useful for durable workflows such as an issue-to-PR run.

## Check host support

`workspace_leases.v1` is an additive host feature. Probe it before accessing `ctx.workspaces`, and
use `getattr` so the same plugin still loads on Hermes versions that predate host-feature probes:

```python
def workspace_lifecycle(ctx):
    has_feature = getattr(ctx, "has_host_feature", None)
    if not callable(has_feature) or not has_feature("workspace_leases.v1"):
        return None
    return getattr(ctx, "workspaces", None)


def register(ctx):
    workspaces = workspace_lifecycle(ctx)
    if workspaces is None:
        register_review_only_surfaces(ctx)
        return
    # Lease-only imports also belong after the probe. Older hosts do not ship
    # this module but must still load the plugin's review-only surfaces.
    from hermes_cli.plugin_workspaces import WorkspaceLeaseError

    register_development_surfaces(ctx, workspaces)
```

The `hermes plugins validate` registration probe reports host features as unavailable. Keep the
fallback path importable and functional; do not fail plugin registration merely because the
validator or an older host does not offer leases. Never import `hermes_cli.plugin_workspaces` at
module scope: facade access and lease exception/type imports both belong after the successful
`getattr`-based probe.

:::caution Lifecycle is not workspace-bound execution
`workspace_leases.v1` covers only acquisition, ownership fencing, heartbeat/reconnect, inspection,
release, and conservative cleanup. It does **not** bind `terminal`, file tools, or
`ctx.dispatch_tool()` to a lease handle, and it does not prevent a caller from supplying another
working directory.

A consumer that requires both durable leases and host-enforced terminal/file confinement must
probe **both** `workspace_leases.v1` and the separate bound-dispatch host feature documented by the
Hermes version that provides it. Keep that consumer disabled until both probes succeed. Do not
invent or infer the bound-dispatch feature name from `workspace_leases.v1`.
:::

## Lifecycle API

The facade exposes the five lifecycle methods plus `new_intent()`, which creates the durable
idempotency material required by `acquire` and `reconnect`:

```python
acquire_intent = ctx.workspaces.new_intent()
persist_before_call("run-01.acquire", acquire_intent)
handle = ctx.workspaces.acquire("run-01", intent=acquire_intent, ttl_seconds=300)
snapshot = ctx.workspaces.inspect(handle)
snapshot = ctx.workspaces.renew(handle, ttl_seconds=300)

reconnect_intent = ctx.workspaces.new_intent()
persist_before_call("run-01.reconnect", reconnect_intent)
successor = ctx.workspaces.reconnect(
    handle, intent=reconnect_intent, ttl_seconds=300,
)
released = ctx.workspaces.release(successor)
```

| Method | Contract |
|---|---|
| `new_intent()` | Returns a JSON-serializable, single-operation bearer intent. Persist it before calling `acquire` or `reconnect`; never reuse it for another operation. |
| `acquire(workspace_id, *, intent, ttl_seconds=300)` | Exclusively reserves a canonical directory and returns a new opaque handle. Retrying the exact request with the same persisted intent returns the same committed handle. |
| `inspect(handle)` | Validates the handle, scope, path, state, and TTL, then returns the current snapshot. It does not renew the heartbeat. |
| `renew(handle, *, ttl_seconds=None)` | Extends the heartbeat for the current process owner and returns the updated snapshot. |
| `reconnect(handle, *, intent, ttl_seconds=None)` | Reclaims a lease after a process restart, rotating the handle. Retrying with the predecessor handle and same intent returns that exact committed successor. |
| `release(handle)` | Atomically detaches the leased path, records the released state, and then cleans or quarantines its contents. Repeating release with the same current handle is safe. |

`ttl_seconds` must be finite and between 1 second and 24 hours. A worker should renew well before
expiry and persist the returned handle before it begins work that must survive a host restart.
Another live process cannot renew, reconnect, or release an unexpired lease it does not own.
The operation intent must be durable before the call: it is the only copy of the future bearer if
the worker exits after Hermes commits but before the response reaches plugin state. Hermes stores
only its capability digest and an immutable operation receipt. A retry must use the same workspace,
TTL, predecessor (for reconnect), operation ID, and capability; mismatches and superseded intents
are rejected.

`workspace_id` is a stable plugin-chosen identifier: 1–128 lowercase ASCII letters, digits,
periods, underscores, or hyphens. It must start with a letter or digit, may not contain `..`, and
may not end in a period or be a reserved device name. Use a durable run ID rather than a display
title; the trailing-period rule prevents Windows from aliasing two textual IDs to one directory.

## Handles and fencing

A handle is a JSON-serializable bearer capability with exactly three fields:

```json
{
  "contract_version": 1,
  "lease_id": "opaque UUID",
  "capability": "opaque bearer secret"
}
```

An operation intent is similarly opaque and serializable, but carries `operation_id` instead of
`lease_id`. Do not put either object in logs or model-visible data.

It deliberately contains no filesystem path, plugin name, or profile path. Hermes persists only a
digest of the bearer capability in the lease database. Treat the complete handle as secret plugin
state: do not log it, put it in a PR body, pass it to the model, or expose it through a browser API.
If restart recovery is required, store the handle in the plugin's protected durable state and
replace it atomically with the handle returned by `reconnect`.

Every reconnect rotates both the lease ID and capability. The predecessor becomes invalid
immediately, so a stale worker cannot inspect, renew, or release the successor's directory. Handles
are also bound to the plugin namespace and active Hermes profile; a handle copied to another plugin
or profile is rejected.

Use `inspect(handle)["path"]` to obtain the path after each acquisition or reconnect. Never derive a
path from a handle or accept a caller-provided substitute.

## Paths, profiles, and permissions

Hermes keeps public workspace names under plugin data, but stores ownership state in a
host-private registry directly below the profile root:

```text
$HERMES_HOME/
├── .plugin-workspace-roots-v1.json
├── .plugin-workspace-roots.lock
├── .plugin-workspace-leases/
│   ├── .plugin-binding-<identity>.json
│   └── plugin-<identity>/workspace-leases.db
└── plugin-data/<plugin-namespace>/
    ├── workspaces/<workspace-id>/
    └── workspace-quarantine/
```

For PR Review, a run named `run-01` is therefore exactly:

```text
$HERMES_HOME/plugin-data/pr-review/workspaces/run-01
```

Runtime data never belongs in an installed plugin directory. An outer marker binds the private
registry and public plugin-data roots. A final per-plugin binding, published only after an empty
initialized database and empty roots exist, binds the database, namespace, and workspace roots;
its public mirror supports fail-closed recovery. Removing or replacing a bound subtree cannot
reset a live lease to a new generation-1 database.

The facade remains bound to the
`PluginContext` profile that created it even if a multiplexed process later changes ambient profile
scope. A random stable profile identity plus relative persisted locators lets supported
same-filesystem profile renames retain handles and operation replays. Plugin namespaces isolate
unrelated plugins, while profile roots isolate tenants.
The `agent-plugin-*` and host-generated `hermes-native-*` families are reserved. A native plugin
whose literal ID uses either prefix is hashed again into a distinct namespace, and every lease also
persists an independent canonical plugin-identity digest. The native ID `pr-review` keeps the exact
readable path shown above.

On POSIX hosts Hermes enforces mode `0700` on host-owned data/workspace directories and `0600` on
markers, the lock, database, and SQLite auxiliaries. On Windows it uses a protected
owner-and-SYSTEM-only DACL and 128-bit file IDs. It rejects symlinks, junctions, hard links, and
aliased namespace, workspace, marker, database, and auxiliary paths rather than following them.
Every generation also persists the workspace leaf identity, so replacement by another regular
directory is fenced. The lease service creates an empty directory; cloning or otherwise
materializing repository content remains the plugin's responsibility.

Filesystem mutations are relative to verified parent handles held through mutation and strict
metadata flush. POSIX uses `O_DIRECTORY | O_NOFOLLOW` descriptors plus `dir_fd` rename/mkdir/stat;
Windows holds reparse-point-safe directory handles without delete sharing, verifies file IDs, and
requests write-through moves. That request plus identity-based recovery is the strongest portable
boundary here; it is not documented as a directory-`fsync` equivalent. Replacing `workspaces/` or
`workspace-quarantine/` during an operation
therefore fails closed instead of redirecting work outside plugin data.
The SQLite connection owns the bootstrap lock and authenticated root chain for its full lifetime.
On POSIX the main database is pre-opened with `O_NOFOLLOW` and SQLite reopens that held leaf through
the descriptor filesystem. Hermes validates the main file plus `-wal`, `-shm`, and `-journal`
before and after use; Windows no-delete-sharing handles prevent replacement while SQLite is live.
The connection denies `ATTACH` and `DETACH`.

The lease schema upgrades additively inside the authenticated registry. A legacy public
`plugin-data/<namespace>/workspace-leases.db` or any auxiliary name is refused before markers or
private state are created. Stop possible old writers and perform an explicit offline migration;
never delete or guess ownership. Conflicting or malformed private rows also abort without being
claimed.

Lease markers, the registry, legacy lease database names, and active/quarantined workspace trees
are non-portable ownership artifacts. Clone-all, profile export/import, full backup/import, and
quick snapshots exclude them while preserving ordinary plugin data. Preserve important active or
quarantined work separately before those operations.

:::caution Trusted host boundary
The registry is trusted host-private state. Workspace-bound dispatch must never expose it to a
plugin job; that confinement is the separate H2 capability. Arbitrary uncooperative same-user
mutation of canonical `HERMES_HOME` itself is equivalent to replacing the trusted profile root and
is outside this lifecycle contract. Cooperative lifecycle processes serialize on the bound OS
lock; public plugin-data paths remain treated as replaceable and hostile.
:::

## Cleanup and recovery

Acquisition records the owner PID, process creation time, diagnostic hostname, hashed stable
machine identity, independent boot witness, generation,
wall-clock audit timestamps, and a monotonic heartbeat/deadline. On the same verified boot, every
expiry decision uses only the monotonic deadline, so wall-clock corrections neither reclaim early
nor extend a lease. A boot mismatch proves death only when stable machine identity also matches;
equal hostnames alone never establish machine identity. Foreign or unverifiable
ownership must be observed continuously by one local boot/process for a full TTL before expiry;
one wall-clock-expired read never grants authority. After a worker restart, call `reconnect` with
its persisted handle. A dead
owner can be reclaimed before expiry; an unexpired live or unverifiable foreign owner is refused.
After expiry, the bearer may take over even if the previous PID still appears live or its liveness
is unknown, because the TTL is the durable fencing boundary. Successful reconnect increments the
generation, rotates the handle, and fences the predecessor.

Release and stale-generation reclamation first rename the old directory away from its public
workspace name. That atomic detach prevents delayed cleanup from deleting a successor that has
already acquired the same `workspace_id`. Hermes treats the filesystem transition as durable only
after a strict directory-metadata flush succeeds: POSIX uses directory `fsync`; Windows requests
write-through `MoveFileEx` namespace transitions (staged creation and tombstoned removal), because
Windows does not support `FlushFileBuffers` on directory handles. A durability error fails the
operation before its success state commits, while the
deterministic detached name lets the same intent reconcile a rename that physically completed.
If detach itself is refused, or both canonical and planned names exist, release remains durably in
`releasing`, records the failure, and raises `WorkspacePathError`; clear the filesystem condition
and retry the same handle. It never reports `released` while the canonical name is still occupied.

Hermes removes only work it can prove disposable:

- an empty directory is removed;
- every nonempty Git checkout is preserved, even when its current tree is clean and `HEAD` is on a
  remote-tracking ref, because another branch, tag, or reflog may contain unique commits;
- dirty, untracked, ignored, unpushed, non-Git, or otherwise uncertain content is moved to
  `workspace-quarantine/` and reported in the cleanup receipt.

Cleanup intent and outcomes also enter an append-only, generation-keyed receipt ledger. Its
deterministic detached names let a retry reconcile a crash after rename but before the lease-row
transition commits; replacing the current lease row does not erase predecessor receipts.
Every lease and cleanup plan also persists the data/workspaces/quarantine root file identities.
A regular-directory replacement therefore fails closed on retry; absence under a replacement root
is never reinterpreted as successful removal, and the original identity/path remains in the receipt.
Snapshots include current ownership and timing fields, the canonical path, generation, bounded
lifecycle events, the latest cleanup receipt, and recent `cleanupReceipts`. Surface quarantine
receipts to an operator and make retention an explicit plugin policy; do not silently delete
quarantined work.

## Failures to handle

Only after the host-feature probe succeeds, import exception classes from
`hermes_cli.plugin_workspaces` when a workflow needs distinct recovery paths:

```python
if getattr(ctx, "has_host_feature", lambda _name: False)("workspace_leases.v1"):
    from hermes_cli.plugin_workspaces import (
        InvalidWorkspaceHandleError,
        WorkspaceLeaseError,
    )
```

| Exception | Meaning |
|---|---|
| `WorkspaceInUseError` | Another generation still owns the requested workspace. |
| `WorkspaceLeaseExpiredError` | The handle expired and must be reconnected before direct use. |
| `WorkspaceOwnershipError` | A different live or unverifiable process owns the lease. |
| `InvalidWorkspaceHandleError` | The handle is malformed, stale, released, tampered with, or belongs to another plugin/profile. |
| `WorkspaceDurabilityError` | A filesystem mutation completed but its strict metadata flush failed; retry the same persisted intent so Hermes reconciles its deterministic name. |
| `WorkspacePathError` | Hermes cannot prove the on-disk path or lease database is canonical and safe. |

Fail closed on all of these. In particular, never fall back to an arbitrary temporary directory
after a fencing, ownership, or path-validation error: doing so would disconnect durable job state
from the workspace that the lease protects.
