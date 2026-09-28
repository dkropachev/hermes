"""Guard routing for trusted callers that inject an exact host-local target.

Workspace-bound plugin dispatch can run while the session's ordinary terminal
backend is Docker.  Its file operations are deliberately host-local, so guard
resolution must follow the injected target rather than re-resolve the path in
the configured backend namespace (especially for Windows drive paths).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tools.file_tools as file_tools
import tools.file_tools_write_guards as write_guards


class _NeverWrite:
    def write_file(self, *_args, **_kwargs):
        pytest.fail("binary guard allowed the write to reach the backend")


def test_injected_target_drives_every_write_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = str(tmp_path / "checkout" / "notes.txt")
    model_path = r"C:\workspace\notes.txt"
    seen: dict[str, object] = {}

    def sensitive(path, task_id, *, resolved_path=None):
        seen["sensitive"] = (path, task_id, resolved_path)

    def binary(path, task_id, *, resolved_path=None):
        seen["binary"] = (path, task_id, resolved_path)

    def protected(paths, task_id, *, resolved_paths=None):
        seen["protected"] = (paths, task_id, resolved_paths)

    def approval(paths, task_id, *, resolved_paths=None):
        seen["approval"] = (paths, task_id, resolved_paths)

    def mirror(path, task_id, *, resolved_path=None, host_local=False):
        seen["mirror"] = (path, task_id, resolved_path, host_local)
        return "stop after guard routing"

    monkeypatch.setattr(file_tools, "_check_sensitive_path", sensitive)
    monkeypatch.setattr(file_tools, "_check_binary_document_write", binary)
    monkeypatch.setattr(file_tools, "_check_protected_instruction_write", protected)
    monkeypatch.setattr(file_tools, "_check_approval_required_write", approval)
    monkeypatch.setattr(file_tools, "_check_cross_profile_path", mirror)

    result = json.loads(file_tools.write_file_tool(
        model_path,
        "replacement",
        task_id="docker-session",
        _file_ops=object(),
        _resolved_path=actual,
    ))

    assert result["error"] == "stop after guard routing"
    assert seen == {
        "sensitive": (model_path, "docker-session", actual),
        "binary": (model_path, "docker-session", actual),
        "protected": ([model_path], "docker-session", {model_path: actual}),
        "approval": ([model_path], "docker-session", {model_path: actual}),
        "mirror": (model_path, "docker-session", actual, True),
    }


def test_injected_target_drives_every_replace_precheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = str(tmp_path / "checkout" / "notes.txt")
    model_path = r"C:\workspace\notes.txt"
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        file_tools,
        "_check_sensitive_path",
        lambda path, task_id, **kwargs: calls.append(("sensitive", kwargs)) or None,
    )
    monkeypatch.setattr(
        file_tools,
        "_check_cross_profile_path",
        lambda path, task_id, **kwargs: calls.append(("mirror", kwargs)) or None,
    )
    monkeypatch.setattr(
        file_tools,
        "_check_binary_document_write",
        lambda path, task_id, **kwargs: calls.append(("binary", kwargs)) or None,
    )
    monkeypatch.setattr(
        file_tools,
        "_check_protected_instruction_write",
        lambda paths, task_id, **kwargs: calls.append(("protected", kwargs)) or None,
    )
    monkeypatch.setattr(
        file_tools,
        "_check_approval_required_write",
        lambda paths, task_id, **kwargs: calls.append(("approval", kwargs))
        or "stop after guard routing",
    )

    result = json.loads(file_tools.patch_tool(
        mode="replace",
        path=model_path,
        old_string="old",
        new_string="new",
        task_id="docker-session",
        _file_ops=object(),
        _resolved_path=actual,
    ))

    assert result["error"] == "stop after guard routing"
    assert calls == [
        ("sensitive", {"resolved_path": actual}),
        ("mirror", {"resolved_path": actual, "host_local": True}),
        ("binary", {"resolved_path": actual}),
        ("protected", {"resolved_paths": {model_path: actual}}),
        ("approval", {"resolved_paths": {model_path: actual}}),
    ]


def test_injected_existing_binary_is_checked_on_host_not_task_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = tmp_path / "checkout" / "image.png"
    actual.parent.mkdir()
    actual.write_bytes(b"\x89PNG\r\n\x1a\noriginal")

    monkeypatch.setattr(
        write_guards,
        "_resolve_path_for_task",
        lambda *_args, **_kwargs: pytest.fail(
            "injected host target was re-resolved in the Docker task namespace"
        ),
    )

    result = json.loads(file_tools.write_file_tool(
        r"C:\workspace\image.png",
        "plain text",
        task_id="docker-session",
        _file_ops=_NeverWrite(),
        _resolved_path=str(actual),
    ))

    assert "existing binary" in result["error"].lower()
    assert actual.read_bytes() == b"\x89PNG\r\n\x1a\noriginal"


def test_host_injected_mirror_guard_ignores_configured_docker_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = str(tmp_path / "checkout" / "notes.txt")
    monkeypatch.setattr(
        write_guards,
        "_get_container_mirror_prefix_for_task",
        lambda *_args, **_kwargs: pytest.fail(
            "host-local guard consulted the configured Docker backend"
        ),
    )

    assert write_guards._check_cross_profile_path(
        r"C:\workspace\notes.txt",
        "docker-session",
        resolved_path=actual,
        host_local=True,
    ) is None
