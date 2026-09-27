---
title: Durable Plugin Workspace Leases
description: Reserve profile-scoped work directories across cooperative plugin worker restarts
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
    register_development_surfaces(ctx, workspaces)
```

`hermes plugins validate` probes registration once with this host feature unavailable and once
with it available. Keep both paths importable and functional. Registration probes expose only the
API shape and reject lifecycle mutations such as `acquire`.

:::warning Cooperative-host boundary
Version 1 coordinates trusted plugin workers on a cooperative machine. It is not a filesystem
security boundary: it does not defend against symlink, junction, mount, inode-replacement,
permission, or case-alias attacks by another local process or plugin. It also does not sandbox
commands. That hardening is intentionally deferred to
[Hermes issue #9](https://github.com/dkropachev/hermes/issues/9). Do not use this API to run
untrusted code or to isolate mutually hostile tenants.
:::

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

The facade has five methods:

```python
handle = ctx.workspaces.acquire("run-01", ttl_seconds=300)
snapshot = ctx.workspaces.inspect(handle)
snapshot = ctx.workspaces.renew(handle, ttl_seconds=300)
successor = ctx.workspaces.reconnect(handle, ttl_seconds=300)
released = ctx.workspaces.release(successor)
```

| Method | Contract |
|---|---|
| `acquire(workspace_id, *, ttl_seconds=300)` | Exclusively reserves the plugin's named directory and returns a new opaque handle. A live, unexpired generation cannot be acquired twice. |
| `inspect(handle)` | Validates the handle, scope, path, state, and TTL, then returns the current snapshot. It does not renew the heartbeat. |
| `renew(handle, *, ttl_seconds=None)` | Extends the heartbeat for the current process owner and returns the updated snapshot. |
| `reconnect(handle, *, ttl_seconds=None)` | Reclaims a lease after a process restart when ownership can be proven safe. It rotates the lease ID and bearer capability and returns a successor handle. |
| `release(handle)` | Atomically detaches the leased path, records the released state, and then cleans or quarantines its contents. Repeating release with the same current handle is safe. |

`ttl_seconds` must be finite and between 1 second and 24 hours. A worker should renew well before
expiry and persist the returned handle before it begins work that must survive a host restart.
Another live process cannot renew, reconnect, or release an unexpired lease it does not own.

`workspace_id` is a stable plugin-chosen identifier: 1–128 lowercase ASCII letters, digits,
periods, underscores, or hyphens. It must start with a letter or digit, may not contain `..`, and
may not be a reserved device name. Use a durable run ID rather than a display title.

## Handles and fencing

A handle is a JSON-serializable bearer capability with exactly three fields:

```json
{
  "contract_version": 1,
  "lease_id": "opaque UUID",
  "capability": "opaque bearer secret"
}
```

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

## Paths and profiles

Hermes allocates workspaces under the active profile's plugin-data directory:

```text
$HERMES_HOME/plugin-data/<plugin-namespace>/
├── workspace-leases.db
├── workspaces/
│   └── <workspace-id>/
└── workspace-quarantine/
```

For PR Review, a run named `run-01` is therefore exactly:

```text
$HERMES_HOME/plugin-data/pr-review/workspaces/run-01
```

Runtime data never belongs in an installed plugin directory. The facade remains bound to the
`PluginContext` profile that created it even if a multiplexed process later changes ambient profile
scope. Plugin namespaces and profile paths keep cooperative plugins' state separate; they are not
an access-control boundary. The lease service creates an empty directory. Cloning or otherwise
materializing repository content remains the plugin's responsibility.

## Cleanup and recovery

Acquisition records the owner PID, process creation time, host, host-instance witness, generation,
heartbeat, and expiry. After a worker restart, call `reconnect` with its persisted handle. A dead
owner or expired generation can be reclaimed; a live foreign owner is refused. Successful
reconnect increments the generation and fences the predecessor.

Release and stale-generation reclamation first rename the old directory away from its public
workspace name. That atomic detach prevents delayed cleanup from deleting a successor that has
already acquired the same `workspace_id`.

The MVP cleanup rule is deliberately small and conservative:

- an empty directory is removed;
- every non-empty directory is moved to `workspace-quarantine/` and reported in the cleanup
  receipt.

Version 1 does not invoke Git or attempt to distinguish pushed checkouts from dirty, untracked,
ignored, unpushed, or non-Git content. Smarter destructive cleanup is deferred with the other
workspace hardening in [Hermes issue #9](https://github.com/dkropachev/hermes/issues/9).

Snapshots include current ownership and timing fields, the allocated path, generation, bounded
lifecycle events, and the latest cleanup receipt. Surface quarantine receipts to an operator and
make retention an explicit plugin policy; do not silently delete quarantined work.

## Failures to handle

Import the exception classes from `hermes_cli.plugin_workspaces` when a workflow needs distinct
recovery paths:

| Exception | Meaning |
|---|---|
| `WorkspaceInUseError` | Another generation still owns the requested workspace. |
| `WorkspaceLeaseExpiredError` | The handle expired and must be reconnected before direct use. |
| `WorkspaceOwnershipError` | A different live or unverifiable process owns the lease. |
| `InvalidWorkspaceHandleError` | The handle is malformed, stale, released, tampered with, or belongs to another plugin/profile. |
| `WorkspacePathError` | The expected workspace directory is missing or cannot be used. |

Do not fall back to an arbitrary temporary directory after a fencing, ownership, or path error:
doing so would disconnect durable job state from the workspace that the lease protects.
