"""Errors shared by the plugin workspace lifecycle and filesystem boundary."""


class WorkspaceLeaseError(RuntimeError):
    """Base class for workspace lifecycle failures."""


class InvalidWorkspaceHandleError(WorkspaceLeaseError):
    """The handle is malformed, stale, released, or belongs to another scope."""


class WorkspaceInUseError(WorkspaceLeaseError):
    """A live or not-yet-expired owner already holds the workspace."""


class WorkspaceLeaseExpiredError(InvalidWorkspaceHandleError):
    """The lease heartbeat expired; reconnect is required before further use."""


class WorkspaceOwnershipError(WorkspaceLeaseError):
    """A different live process owns the lease."""


class WorkspacePathError(WorkspaceLeaseError):
    """The workspace layout cannot be proven safe and canonical."""


class WorkspaceDurabilityError(WorkspacePathError):
    """A filesystem mutation completed, but its metadata could not be durably flushed."""

    def __init__(self, message: str, *, mutation_completed: bool) -> None:
        super().__init__(message)
        self.mutation_completed = mutation_completed
