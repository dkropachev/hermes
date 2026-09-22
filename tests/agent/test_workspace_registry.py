"""Behavior contract for workspace-provider values and scoped lookup."""

from dataclasses import FrozenInstanceError

import pytest

from agent import workspace_registry
from agent.workspace_provider import WorkspaceLease, WorkspaceProvider, WorkspaceRequest


class _Provider(WorkspaceProvider):
    def __init__(self, marker: str):
        self.marker = marker

    @property
    def name(self) -> str:
        return "  Git-Leases  "

    def is_available(self) -> bool:
        return True

    def try_acquire(self, request: WorkspaceRequest, **kwargs) -> WorkspaceLease:
        return WorkspaceLease(
            lease_id=f"{self.marker}:{request.run_id}",
            path=f"/workspaces/{request.task_id}",
            branch_name=request.branch_name,
        )

    def renew(self, request: WorkspaceRequest, lease: WorkspaceLease, **kwargs) -> bool:
        return lease.lease_id == f"{self.marker}:{request.run_id}"

    def release(
        self, request: WorkspaceRequest, lease: WorkspaceLease, *, outcome: str, **kwargs
    ) -> None:
        return None


def test_request_lease_and_registry_preserve_profile_scoped_provider_identity():
    workspace_registry._reset_for_tests()
    global_provider = _Provider("global")
    profile_provider = _Provider("profile-a")
    request = WorkspaceRequest(
        task_id="task-1",
        run_id=7,
        owner_id="worker-1",
        board="default",
        board_db_path="/state/kanban.db",
        access="write",
        workspace_kind="worktree",
        requested_path="/repos/project",
        branch_name="tasks/task-1",
        project_id="project-1",
        repo_root="/repos/project",
    )

    try:
        workspace_registry.register_provider(global_provider)
        workspace_registry.register_provider(profile_provider, scope="profile-a")

        assert workspace_registry.get_provider("GIT-LEASES", scope="profile-a") is profile_provider
        assert workspace_registry.get_provider("git-leases", scope="profile-b") is global_provider
        lease = profile_provider.try_acquire(request)
        assert lease == WorkspaceLease(
            lease_id="profile-a:7",
            path="/workspaces/task-1",
            branch_name="tasks/task-1",
        )
        assert profile_provider.renew(request, lease) is True
        with pytest.raises(FrozenInstanceError):
            request.access = "read"
        with pytest.raises(FrozenInstanceError):
            lease.path = "/elsewhere"
    finally:
        workspace_registry._reset_for_tests()
