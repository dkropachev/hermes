"""Abstract contract for plugin-owned task workspaces.

Core scheduling code supplies immutable request data and persists the returned
lease.  Providers own allocation and coordination policy; in particular,
``access`` is coordination metadata rather than a filesystem permission mode.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Literal, Optional

from agent.provider_base import ProviderBase

WorkspaceAccess = Literal["read", "write"]


@dataclass(frozen=True)
class WorkspaceRequest:
    """Stable task context passed to every workspace-provider operation."""

    task_id: str
    run_id: int
    owner_id: str
    board: str
    board_db_path: str
    access: WorkspaceAccess
    workspace_kind: str
    requested_path: Optional[str]
    branch_name: Optional[str]
    project_id: Optional[str] = None
    repo_root: Optional[str] = None


@dataclass(frozen=True)
class WorkspaceLease:
    """Opaque provider lease plus the workspace coordinates core may persist."""

    lease_id: str
    path: str
    branch_name: Optional[str] = None
    expires_at: Optional[float] = None


class WorkspaceProvider(ProviderBase):
    """Allocate and coordinate workspaces before a task worker starts."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return whether this provider is configured and ready without network I/O."""

    @abc.abstractmethod
    def try_acquire(
        self, request: WorkspaceRequest, **kwargs: Any
    ) -> Optional[WorkspaceLease]:
        """Acquire a workspace, or return ``None`` when contention should defer the task."""

    @abc.abstractmethod
    def renew(
        self, request: WorkspaceRequest, lease: WorkspaceLease, **kwargs: Any
    ) -> bool:
        """Refresh ``lease``; return ``False`` when the provider no longer owns it."""

    @abc.abstractmethod
    def release(
        self,
        request: WorkspaceRequest,
        lease: WorkspaceLease,
        *,
        outcome: str,
        **kwargs: Any,
    ) -> None:
        """Release ``lease`` idempotently after the worker can no longer execute."""
