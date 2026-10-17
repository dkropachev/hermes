"""Cold native-history inspection backed by host-owned supervision evidence."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

import psutil

from agent.transports.codex_app_server_supervisor import proof, read_receipt, supported, write_receipt


def session_root(ctx, binding):
    from agent.plugin_llm_sessions import _digest
    return Path(ctx.workspaces._home_path) / "plugin-sessions" / binding["plugin"] / _digest(binding)


def execution(root, binding, *, dispatch_id=None):
    from agent.plugin_llm_sessions import _digest
    nonce = uuid.uuid4().hex
    owner = psutil.Process()
    return {"receipt": str(Path(root) / ("execution-" + nonce + ".json")), "nonce": nonce,
            "binding": _digest(binding), "owner": {"pid": owner.pid, "created": owner.create_time()},
            "dispatchId": dispatch_id, "workspace": binding["workspace"], "generation": binding["generation"]}


def persist_reference(root, reference):
    from agent.plugin_llm_sessions import _digest
    value = {"reference": reference, "bindingDigest": _digest(reference["binding"]), "updatedAt": time.time()}
    # A new native root or resume must not erase an earlier execution's
    # recoverable identity. Children may be added within the same execution.
    write_receipt(_reference_path(root, reference), value)
    write_receipt(Path(root) / "reference.json", value)


def _reference_path(root, reference):
    identity = reference.get("executionId", "")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise ValueError("Native reference lacks durable execution identity")
    return Path(root) / ("execution-" + identity + "-reference.json")


def _saved_reference(root, reference):
    from agent.plugin_llm_sessions import _digest
    path = _reference_path(root, reference)
    # Earlier host versions kept only the latest reference. Accept that record
    # only when it still identifies this exact execution.
    stored = read_receipt(path if path.exists() else Path(root) / "reference.json")
    saved = stored.get("reference", {})
    if (stored.get("bindingDigest") != _digest(reference["binding"])
            or any(saved.get(key) != reference.get(key) for key in ("binding", "threadId", "executionId", "accountId", "consumerKey"))):
        raise ValueError("Native reference does not match its host-owned history")
    return saved


def _raw_history(root, thread):
    path = thread.get("path")
    if not isinstance(path, str):
        raise ValueError("Native thread has no durable history path")
    selected = Path(path).resolve()
    if not selected.is_relative_to(root.resolve()):
        raise ValueError("Native history path is outside its bound session home")
    fd = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError("Native history exceeds the complete-inspection bound")
    records, incomplete = [], None
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("type"), str):
                raise ValueError("Invalid native history record")
            records.append(record)
        except ValueError:
            if index != len(lines) - 1:
                raise ValueError("Native durable history contains a corrupt interior record") from None
            incomplete = line.decode("utf-8", errors="replace")
    if not records or records[0].get("type") != "session_meta" or records[0].get("payload", {}).get("id") not in {thread["id"], thread.get("sessionId")}:
        raise ValueError("Native durable history belongs to another session")
    return {"records": records, "complete": incomplete is None, "incompleteTail": incomplete}


def _native_descendants(client, thread_id):
    result = set()
    for archived in (False, True):
        cursor, cursors = None, set()
        while True:
            page = client.request("thread/list", {"ancestorThreadId": thread_id, "archived": archived,
                                  "sourceKinds": ["subAgent", "subAgentReview", "subAgentCompact", "subAgentThreadSpawn", "subAgentOther"],
                                  "limit": 100, "cursor": cursor})
            if not isinstance(page.get("data"), list):
                raise ValueError("Native descendant discovery did not return complete metadata")
            result.update(item["id"] for item in page["data"])
            if len(result) > 10000:
                raise ValueError("Native descendant history exceeds the inspection bound")
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if cursor in cursors:
                raise ValueError("Native descendant pagination repeated its cursor")
            cursors.add(cursor)
    return result


def _scope(ctx, task, reference):
    if ctx.manifest.source not in {"bundled", "user", "entrypoint"}:
        raise PermissionError("Native history requires an installed trusted plugin")
    if not supported():
        raise RuntimeError("Native session recovery requires process supervision")
    binding = reference.get("binding") if isinstance(reference, dict) else None
    layout = ctx.workspaces._layout()
    if not isinstance(binding, dict) or any(binding.get(k) != expected for k, expected in (
        ("profile", str(layout.profile_key)), ("plugin", layout.plugin_identity_digest), ("task", task)
    )) or not isinstance(binding.get("key"), str) or not binding["key"]:
        raise ValueError("Native history belongs to another profile, plugin or auxiliary task")
    root = session_root(ctx, binding)
    if (reference.get("threadId") is not None and not isinstance(reference["threadId"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", reference.get("executionId", ""))):
        raise ValueError("Native reference lacks durable execution identity")
    saved = _saved_reference(root, reference)
    if reference.get("threadId") is None and not (saved.get("newThread") is True and saved.get("turnSubmissionAttempted") is False):
        raise ValueError("Native execution lacks a thread or unsubmitted startup evidence")
    return root, binding


def _inspect(ctx, task, reference):
    from agent.plugin_llm_sessions import (_digest, _live, _live_lock, _session_settings, _toml,
                                           CodexAppServerClient, PROTOCOL_BASELINE, check_codex_binary)
    root, binding = _scope(ctx, task, reference)
    with _live_lock:
        if (binding["profile"], binding["plugin"], binding["key"]) in _live:
            return {"reference": reference, "cleanup": {"quiescent": False, "reason": "session_active"}, "thread": None, "children": []}
    receipt = root / ("execution-" + reference["executionId"] + ".json")
    cleanup = proof(receipt, nonce=reference["executionId"], binding=_digest(binding))
    if not cleanup["quiescent"]:
        return {"reference": reference, "cleanup": cleanup, "thread": None, "children": []}
    saved = _saved_reference(root, reference)
    if saved.get("newThread") is True and saved.get("turnSubmissionAttempted") is False:
        # The native server does not persist a fresh thread until its first
        # turn. Report host evidence explicitly; do not invent native history.
        return {"reference": reference, "cleanup": cleanup, "thread": None, "children": [],
                "rawHistory": {}, "noTurnSubmitted": True}
    settings = _session_settings(ctx.workspaces._home_path, task)
    binary = settings["binary"]
    if check_codex_binary(binary, PROTOCOL_BASELINE) != (True, "0.161.0"):
        raise RuntimeError("Native history requires the protocol-tested Codex 0.161.0")
    import tomllib
    config = tomllib.loads((root / "config.toml").read_text())
    inspection = execution(root, binding)
    client = CodexAppServerClient(codex_bin=binary, codex_home=str(root), supervision=inspection,
                                  extra_args=[arg for key, value in config.items() for arg in ("-c", f"{key}={_toml(value)}")],
                                  env={"TMPDIR": str(root / "tmp")})
    threads = {}
    try:
        client.initialize(capabilities={"experimentalApi": True})
        latest = _saved_reference(root, reference)
        pending = {reference["threadId"], *reference.get("childThreadIds", []), *latest.get("childThreadIds", []),
                   *_native_descendants(client, reference["threadId"])}
        while pending:
            thread_id = pending.pop()
            thread = client.request("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]
            if thread["id"] != thread_id or not isinstance(thread.get("turns"), list):
                raise ValueError("Native history is incomplete or belongs to another thread")
            threads[thread_id] = thread
            for turn in thread["turns"]:
                for item in turn.get("items", []):
                    if item.get("type") == "collabAgentToolCall":
                        pending.update(i for i in item.get("receiverThreadIds", []) if i not in threads)
        verified, remaining = {reference["threadId"]}, set(threads) - {reference["threadId"]}
        while remaining:
            found = {i for i in remaining if threads[i].get("parentThreadId") in verified}
            if not found:
                raise ValueError("Native history contains unrelated child threads")
            remaining -= found
            verified |= found
    finally:
        client.close()
        if not client.is_quiescent():
            raise RuntimeError("Native history inspection did not establish its own process cleanup")
    return {"reference": reference, "cleanup": cleanup, "thread": threads[reference["threadId"]],
            "children": [threads[i] for i in sorted(threads) if i != reference["threadId"]],
            "rawHistory": {i: _raw_history(root, thread) for i, thread in threads.items()}}


async def inspect_session(ctx, *, task, reference):
    def scoped():
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope
        home_token = set_hermes_home_override(ctx.workspaces._home_path)
        secret_token = set_secret_scope(build_profile_secret_scope(ctx.workspaces._home_path))
        try:
            return _inspect(ctx, task, reference)
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)
    return await asyncio.to_thread(scoped)


async def recover_session(ctx, *, task, reference, workspace_handle, intent):
    """Reconcile only the supervised dispatch, then reconnect its preserved tree."""
    from hermes_cli.plugin_workspaces import WorkspaceInUseError
    inspected = await inspect_session(ctx, task=task, reference=reference)
    if not inspected["cleanup"]["quiescent"]:
        return {**inspected, "workspaceHandle": None}
    await asyncio.to_thread(ctx.workspaces._validate_recovery_binding, workspace_handle,
                            intent=intent, binding=reference["binding"])
    try:
        handle = await asyncio.to_thread(ctx.workspaces.reconnect, workspace_handle, intent=intent, ttl_seconds=2100)
    except WorkspaceInUseError:
        await asyncio.to_thread(ctx.workspaces._complete_supervised_dispatch, workspace_handle,
                                binding=reference["binding"], cleanup=inspected["cleanup"])
        handle = await asyncio.to_thread(ctx.workspaces.reconnect, workspace_handle, intent=intent, ttl_seconds=2100)
    return {**inspected, "workspaceHandle": handle}


async def list_sessions(ctx, *, task, key):
    if ctx.manifest.source not in {"bundled", "user", "entrypoint"}:
        raise PermissionError("Native history requires an installed trusted plugin")
    def read():
        layout = ctx.workspaces._layout()
        result = []
        root = Path(ctx.workspaces._home_path) / "plugin-sessions" / layout.plugin_identity_digest
        seen = set()
        for path in [*root.glob("*/execution-*-reference.json"), *root.glob("*/reference.json")]:
            value = read_receipt(path)
            ref = value.get("reference", {})
            binding = ref.get("binding", {})
            if (binding.get("profile"), binding.get("plugin"), binding.get("task"), binding.get("key")) != (
                str(layout.profile_key), layout.plugin_identity_digest, task, key
            ):
                continue
            _scope(ctx, task, ref)
            identity = (path.parent, ref["executionId"])
            if identity in seen:
                continue
            seen.add(identity)
            result.append({"reference": ref, "updatedAt": value["updatedAt"]})
        return sorted(result, key=lambda r: r["updatedAt"])
    return await asyncio.to_thread(read)
