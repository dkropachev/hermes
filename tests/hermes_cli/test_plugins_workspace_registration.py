"""PluginContext registration contract for workspace providers."""

from agent import workspace_registry
from agent.workspace_provider import WorkspaceLease, WorkspaceProvider, WorkspaceRequest
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


class _Provider(WorkspaceProvider):
    def __init__(self, marker: str):
        self.marker = marker

    @property
    def name(self) -> str:
        return "  Repo-Workspace  "

    def is_available(self) -> bool:
        return True

    def try_acquire(self, request: WorkspaceRequest, **kwargs) -> WorkspaceLease:
        return WorkspaceLease(lease_id=self.marker, path="/workspace")

    def renew(self, request: WorkspaceRequest, lease: WorkspaceLease, **kwargs) -> bool:
        return True

    def release(
        self, request: WorkspaceRequest, lease: WorkspaceLease, *, outcome: str, **kwargs
    ) -> None:
        return None


def test_plugin_registrar_normalizes_scopes_and_unload_restores_each_slot(tmp_path):
    workspace_registry._reset_for_tests()
    home_a = str((tmp_path / "a").resolve())
    home_b = str((tmp_path / "b").resolve())
    manager_a = PluginManager(scope_key=home_a)
    manager_b = PluginManager(scope_key=home_b)
    context_a = PluginContext(PluginManifest(name="workspace-a", key="workspace-a"), manager_a)
    context_b = PluginContext(PluginManifest(name="workspace-b", key="workspace-b"), manager_b)
    provider_a = _Provider("a")
    provider_b = _Provider("b")

    try:
        registration_a = context_a.register_workspace_provider(provider_a)
        registration_b = context_b.register_workspace_provider(provider_b)

        assert registration_a is not None
        assert registration_b is not None
        assert registration_a.kind == "workspace_provider"
        assert registration_a.key == "repo-workspace"
        assert workspace_registry.get_provider("REPO-WORKSPACE", scope=home_a) is provider_a
        assert workspace_registry.get_provider("repo-workspace", scope=home_b) is provider_b

        assert manager_a.unload("workspace-a") is True
        assert workspace_registry.get_provider("repo-workspace", scope=home_a) is None
        assert workspace_registry.get_provider("repo-workspace", scope=home_b) is provider_b
    finally:
        manager_a.unload("workspace-a")
        manager_b.unload("workspace-b")
        workspace_registry._reset_for_tests()
