"""Cooperative terminal and file dispatch bound to a live plugin workspace lease.

This API fixes the operation's starting directory/path server-side. It is not a
sandbox: trusted commands can still address the rest of the host filesystem, and
filesystem substitution by a hostile local process is outside the v1 contract.
"""

from __future__ import annotations

import ntpath
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from hermes_cli.plugin_workspaces import DEFAULT_TTL_SECONDS, PluginWorkspaces


HOST_FEATURE = "workspace_bound_dispatch.v1"


def _relative_target(root: Path, relative_path: str) -> Path:
    """Join a cross-platform relative spelling without resolving filesystem state."""
    if not isinstance(relative_path, str) or not relative_path or "\0" in relative_path:
        raise ValueError("relative_path must be a non-empty relative path")
    drive, _tail = ntpath.splitdrive(relative_path)
    normalized = relative_path.replace("\\", "/")
    if drive or normalized.startswith("/"):
        raise ValueError("relative_path must not be absolute, drive-qualified, or UNC")
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise ValueError("relative_path must stay below the leased workspace")
    return root.joinpath(*parts)


@contextmanager
def _profile_runtime(home: Path):
    """Bind profile-sensitive config and terminal policy to the context owner."""
    from hermes_cli.plugins_loader import _plugin_home_scope
    from tools.terminal_scope import install_and_reset_profile_terminal_scope

    with _plugin_home_scope(home), install_and_reset_profile_terminal_scope(home):
        yield


@contextmanager
def _local_file_operations(root: Path):
    """A fresh canonical host-local file backend for one synchronous call."""
    from tools.environments.local import LocalEnvironment
    from tools.file_operations import ShellFileOperations

    environment = LocalEnvironment(cwd=str(root), timeout=180)
    try:
        yield ShellFileOperations(environment, cwd=str(root))
    finally:
        environment.cleanup()


class PluginWorkspaceTools:
    """Narrow cooperative operations whose paths come only from a lease handle."""

    def __init__(self, workspaces: PluginWorkspaces) -> None:
        self._workspaces = workspaces

    def terminal(
        self,
        handle: Mapping[str, Any],
        command: str,
        timeout: int | None = None,
    ) -> str:
        if not isinstance(command, str):
            raise TypeError("command must be a string")
        from tools.terminal_tool import FOREGROUND_MAX_TIMEOUT

        if timeout is not None:
            if (
                not isinstance(timeout, int)
                or isinstance(timeout, bool)
                or timeout <= 0
                or timeout > FOREGROUND_MAX_TIMEOUT
            ):
                raise ValueError(
                    f"timeout must be an integer from 1 through {FOREGROUND_MAX_TIMEOUT}"
                )
        with self._workspaces._pin_dispatch(
            handle,
            dispatch_kind="terminal",
            minimum_ttl_seconds=max(DEFAULT_TTL_SECONDS, FOREGROUND_MAX_TIMEOUT),
        ) as pinned:
            with _profile_runtime(self._workspaces._home_path):
                from tools.terminal_tool import terminal_tool

                return terminal_tool(
                    command=command,
                    background=False,
                    timeout=timeout,
                    task_id=pinned.task_id,
                    workdir=str(pinned.path),
                    _host_local=True,
                    _allow_yield_to_background=False,
                )

    def read_file(
        self,
        handle: Mapping[str, Any],
        relative_path: str,
        offset: int = 1,
        limit: int = 2000,
    ) -> str:
        with self._workspaces._pin_dispatch(
            handle, dispatch_kind="read_file"
        ) as pinned:
            target = _relative_target(pinned.path, relative_path)
            with (
                _profile_runtime(self._workspaces._home_path),
                _local_file_operations(pinned.path) as file_ops,
            ):
                from tools.file_tools import read_file_tool

                return read_file_tool(
                    str(target),
                    offset=offset,
                    limit=limit,
                    task_id=pinned.task_id,
                    _file_ops=file_ops,
                    _resolved_path=str(target),
                )

    def write_file(
        self,
        handle: Mapping[str, Any],
        relative_path: str,
        content: str,
    ) -> str:
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        with self._workspaces._pin_dispatch(
            handle, dispatch_kind="write_file"
        ) as pinned:
            target = _relative_target(pinned.path, relative_path)
            with (
                _profile_runtime(self._workspaces._home_path),
                _local_file_operations(pinned.path) as file_ops,
            ):
                from tools.file_tools import write_file_tool

                return write_file_tool(
                    str(target),
                    content,
                    task_id=pinned.task_id,
                    _file_ops=file_ops,
                    _resolved_path=str(target),
                )

    def edit_file(
        self,
        handle: Mapping[str, Any],
        relative_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            raise TypeError("old_string and new_string must be strings")
        if not isinstance(replace_all, bool):
            raise TypeError("replace_all must be a boolean")
        with self._workspaces._pin_dispatch(
            handle, dispatch_kind="edit_file"
        ) as pinned:
            target = _relative_target(pinned.path, relative_path)
            with (
                _profile_runtime(self._workspaces._home_path),
                _local_file_operations(pinned.path) as file_ops,
            ):
                from tools.file_tools import patch_tool

                return patch_tool(
                    mode="replace",
                    path=str(target),
                    old_string=old_string,
                    new_string=new_string,
                    replace_all=replace_all,
                    task_id=pinned.task_id,
                    _file_ops=file_ops,
                    _resolved_path=str(target),
                )


__all__ = ["HOST_FEATURE", "PluginWorkspaceTools"]
