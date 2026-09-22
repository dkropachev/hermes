"""Profile-scoped registry for plugin workspace providers."""

from __future__ import annotations

import logging

from agent.provider_registry import ProviderRegistry, lower_key
from agent.workspace_provider import WorkspaceProvider

logger = logging.getLogger(__name__)


_registry: ProviderRegistry[WorkspaceProvider] = ProviderRegistry(
    label="Workspace",
    provider_cls=WorkspaceProvider,
    logger=logger,
    normalize=lower_key,
)
_registry.export(globals())
