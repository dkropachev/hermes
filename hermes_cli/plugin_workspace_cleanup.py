"""Conservative classification for detached plugin workspace trees."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    allowed = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TMPDIR", "TEMP", "TMP")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"})
    pass_fds: tuple[int, ...] = ()
    if os.name != "nt":
        match = re.match(r"^/(?:proc/self|dev)/fd/(\d+)(?:/|$)", str(workspace))
        if match:
            pass_fds = (int(match.group(1)),)
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(workspace), *args],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=5, env=env, check=False,
        pass_fds=pass_fds,
    )


def classify_workspace(path: Path) -> dict[str, Any]:
    checked_at = time.time()
    if path.is_symlink():
        return {"classification": "symlink", "safe_to_delete": False, "checked_at": checked_at}
    if not path.exists():
        return {"classification": "missing", "safe_to_delete": True, "checked_at": checked_at}
    if not path.is_dir():
        if not path.exists():
            return {"classification": "missing", "safe_to_delete": True, "checked_at": checked_at}
        return {"classification": "non_directory", "safe_to_delete": False, "checked_at": checked_at}
    try:
        if next(path.iterdir(), None) is None:
            return {"classification": "clean_empty", "safe_to_delete": True, "checked_at": checked_at}
        status = _git(
            path, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching",
        )
        if status.returncode != 0:
            return {
                "classification": "uncertain", "safe_to_delete": False,
                "checked_at": checked_at, "reason": "git_status_failed",
            }
        lines = [line for line in status.stdout.splitlines() if line]
        if lines:
            untracked = any(line.startswith("??") for line in lines)
            ignored = any(line.startswith("!!") for line in lines)
            tracked = any(not line.startswith(("??", "!!")) for line in lines)
            classification = "dirty" if tracked else "untracked" if untracked else "ignored"
            return {
                "classification": classification,
                "safe_to_delete": False, "checked_at": checked_at,
                "dirty": tracked, "untracked": untracked, "ignored": ignored,
                "changed_paths": len(lines),
            }
        contains = _git(path, "branch", "-r", "--contains", "HEAD", "--format=%(refname)")
        if contains.returncode == 0 and contains.stdout.strip():
            return {"classification": "clean_git", "safe_to_delete": False, "checked_at": checked_at}
        head = _git(path, "rev-parse", "--verify", "HEAD")
        if head.returncode == 0:
            return {
                "classification": "unpushed", "safe_to_delete": False,
                "checked_at": checked_at, "head": head.stdout.strip()[:64],
            }
        return {
            "classification": "uncertain", "safe_to_delete": False,
            "checked_at": checked_at, "reason": "git_head_unverifiable",
        }
    except FileNotFoundError:
        return {"classification": "missing", "safe_to_delete": True, "checked_at": checked_at}
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "classification": "uncertain", "safe_to_delete": False,
            "checked_at": checked_at, "reason": type(exc).__name__,
        }


def planned_name(
    workspace_id: str, lease_id: str, generation: int, operation: str,
) -> str:
    return f".{operation}-{workspace_id}-g{int(generation)}-{lease_id}"


def render_cleanup_paths(
    layout: Any, workspace_id: str, cleanup: Mapping[str, Any], *,
    lease_id: str = "", generation: int | None = None, operation: str | None = None,
) -> dict[str, Any]:
    """Render persisted relative names without trusting legacy absolute paths."""
    result: dict[str, Any] = {}
    path_keys = {"original_path", "planned_detached_path", "detached_path", "quarantine_path"}
    for key, value in cleanup.items():
        if key in path_keys:
            continue
        if isinstance(value, Mapping):
            result[key] = render_cleanup_paths(
                layout, workspace_id, value, lease_id=lease_id, generation=generation,
                operation=str(value.get("operation") or operation or ""),
            )
        elif isinstance(value, list):
            result[key] = [
                render_cleanup_paths(
                    layout, workspace_id, item, lease_id=lease_id, generation=generation,
                    operation=str(item.get("operation") or operation or ""),
                ) if isinstance(item, Mapping) else item
                for item in value
            ]
        else:
            result[key] = value
    shape_keys = {
        "operation", "classification", "disposition", "original_path",
        "planned_detached_name", "detached_name", "quarantine_name",
    }
    if set(cleanup) & shape_keys:
        result["original_path"] = str(layout.workspaces_dir / workspace_id)
    operation = str(cleanup.get("operation") or operation or "")
    if lease_id and generation is not None:
        expected = {
            planned_name(workspace_id, lease_id, generation, candidate)
            for candidate in ("acquire", "release")
        }
        recovery = planned_name(workspace_id, lease_id, generation, "recovery")
        for legacy_key, name_key in (
            ("planned_detached_path", "planned_detached_name"),
            ("detached_path", "detached_name"),
            ("quarantine_path", "quarantine_name"),
        ):
            legacy = cleanup.get(legacy_key)
            if not isinstance(legacy, str):
                continue
            path = Path(legacy)
            valid_parent = (
                path.parent.name == "workspace-quarantine"
                and path.parent.parent.name == layout.plugin_namespace
            )
            if valid_parent and (
                path.name in expected or path.name == recovery
                or path.name.startswith(f"{recovery}-")
            ):
                result.setdefault(name_key, path.name)
    for name_key, path_key in (
        ("planned_detached_name", "planned_detached_path"),
        ("detached_name", "detached_path"),
        ("quarantine_name", "quarantine_path"),
    ):
        name = result.get(name_key)
        if isinstance(name, str):
            result[path_key] = str(layout.quarantine_dir / name)
    return result
