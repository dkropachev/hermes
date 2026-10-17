"""Workspace-bound plugin Codex sessions. The host owns transport and credentials.

A dedicated worker holds the existing workspace dispatch pin from process start
until descendant cleanup. Callbacks run on the caller's event loop, independently
of the transport reader and worker, so a suspended tool cannot prevent Stop.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import inspect
import json
import queue
import shutil
import threading
import time
import tomllib
import uuid
import logging
from pathlib import Path

from agent.transports.codex_app_server import CodexAppServerClient, check_codex_binary
from agent.plugin_session_credentials import session_credentials

PROTOCOL_BASELINE = (0, 161, 0)
_live: dict[tuple[str, str, str], "PluginLlmSession"] = {}
_live_lock = threading.Lock()
log = logging.getLogger(__name__)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}={_toml(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(map(_toml, value)) + "]"
    return json.dumps(value, ensure_ascii=False)


def _session_settings(profile_home, task):
    from agent.auxiliary_client import _get_auxiliary_task_config
    from hermes_cli.config import load_config_readonly
    from hermes_cli.codex_runtime_switch import get_configured_codex_binary
    from hermes_cli.plugins_loader import _plugin_home_scope

    with _plugin_home_scope(profile_home):
        config = load_config_readonly() or {}
        codex_home = Path.home() / ".codex"
        native = tomllib.loads((codex_home / "config.toml").read_text()) if (codex_home / "config.toml").exists() else {}
        auxiliary = _get_auxiliary_task_config(task)
        return {"binary": get_configured_codex_binary(config),
                "native": {k: v for k, v in native.items() if k in {
                    "model", "model_provider", "model_providers", "model_reasoning_effort",
                    "preferred_auth_method", "cli_auth_credentials_store"}},
                "auxiliary": {k: v for k, v in auxiliary.items() if k in {"provider", "model", "reasoning_effort"}}}


def _capacity_limit(ctx):
    from hermes_cli.plugins_loader import _plugin_home_scope
    with _plugin_home_scope(ctx.workspaces._home_path):
        return min(4, max(1, int(ctx.get_config("active_agent_sessions", 2))))


def session_config(codex_config: dict, *, workspace: Path, private: Path,
                   execution_permission="read", child_limit=4) -> dict:
    """Construct an isolated configuration; never inherit installed extensions.

    A minimal filesystem allowlist protects other homes/workspaces by default.
    The shell inherits only session-owned HOME/TMPDIR and a normal executable
    search path. Provider credentials remain in the app-server process.
    """
    if execution_permission not in {"read", "write"}:
        raise ValueError("Execution permission must be read or write")
    if type(child_limit) is not int or not 1 <= child_limit <= 4:
        raise ValueError("Native child limit must be between one and four")
    selected = {k: v for k, v in codex_config.items() if k in {
        "model", "model_provider", "model_providers", "model_reasoning_effort",
        "preferred_auth_method", "cli_auth_credentials_store",
    }}
    selected.update({
        "default_permissions": "plugin-workspace",
        "permissions": {"plugin-workspace": {
            "filesystem": {":minimal": "read", str(workspace): execution_permission,
                           str(private / "tmp"): "write", str(private / "home"): "write"},
            "network": {"enabled": True},
        }},
        "approval_policy": "never", "web_search": "disabled",
        "cli_auth_credentials_store": "file",
        "projects": {str(workspace): {"trust_level": "untrusted"}},
        "mcp_servers": {}, "plugins": {}, "notify": [],
        "features": {"apps": False, "plugins": False, "shell_snapshot": False,
                     "hooks": False, "remote_plugin": False, "worktrees": False,
                     "workspace_dependencies": False, "skill_mcp_dependency_install": False,
                     "skip_host_skill_discovery": True, "multi_agent_v2": False, "multi_agent": True,
                     "default_mode_request_user_input": True, "goals": False, "workflows": False,
                     "computer_use": False, "browser_use": False, "browser_use_external": False,
                     "browser_use_full_cdp_access": False, "in_app_browser": False,
                     "image_generation": False, "realtime_conversation": False},
        "agents": {"max_threads": child_limit},
        "shell_environment_policy": {"inherit": "none", "set": {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(private / "home"),
            "TMPDIR": str(private / "tmp"), "LANG": "C.UTF-8",
        }},
    })
    return selected


async def open_session(ctx, *, task, key, workspace_handle, instructions, tools,
                       resume=None, extensions=(), on_event=None, on_tool_call=None,
                       execution_permission="read", child_limit=4, consumer_key=None):
    try:
        session = await _prepare_session(ctx, task=task, key=key, workspace_handle=workspace_handle,
                                         instructions=instructions, tools=tools, resume=resume, extensions=extensions,
                                         on_event=on_event, on_tool_call=on_tool_call,
                                         execution_permission=execution_permission, child_limit=child_limit, consumer_key=consumer_key)
    except BaseException as exc:
        exc.native_execution_started = False
        raise
    try:
        await session._open()
        session._registration = ctx._track("llm_session", key, session._request_close)
        if session._done.done():
            session._registration.dispose()
    except BaseException:
        await session.close()
        raise
    return session


async def _prepare_session(ctx, *, task, key, workspace_handle, instructions, tools,
                           resume, extensions, on_event, on_tool_call,
                           execution_permission, child_limit, consumer_key):
    if ctx.manifest.source not in {"bundled", "user", "entrypoint"}:
        raise PermissionError("Native execution sessions require an installed trusted plugin")
    if not key or not isinstance(instructions, str):
        raise ValueError("Session key and instructions are required")
    if consumer_key is not None and (not isinstance(consumer_key, str) or not 1 <= len(consumer_key) <= 256
                                     or any(ord(char) < 32 for char in consumer_key)):
        raise ValueError("Consumer key must be a bounded nonempty string")
    # Validate before acquiring a workspace pin or starting a native process.
    if execution_permission not in {"read", "write"}:
        raise ValueError("Execution permission must be read or write")
    if type(child_limit) is not int or not 1 <= child_limit <= 4:
        raise ValueError("Native child limit must be between one and four")
    # Extension code must be selected and loaded by the host before it can run.
    # Domain read integrations use ctx.call_mcp through the plugin's scoped tool.
    if extensions:
        raise ValueError("Native extensions are not enabled for plugin sessions")
    for tool in tools:
        if tool.get("type") != "function" or not tool.get("name") or not tool.get("inputSchema"):
            raise ValueError("Dynamic tools require type=function, name and inputSchema")
    if len({t["name"] for t in tools}) != len(tools):
        raise ValueError("Duplicate dynamic tool name")
    snapshot = await asyncio.to_thread(ctx.workspaces.inspect, workspace_handle)
    settings = await asyncio.to_thread(_session_settings, ctx.workspaces._home_path, task)
    layout = ctx.workspaces._layout()
    binding = {"profile": str(layout.profile_key), "plugin": layout.plugin_identity_digest,
               "key": key, "task": task, "workspace": snapshot["workspaceId"],
               "generation": snapshot["generation"],
               "instructions": _digest(instructions), "tools": _digest(tools), "extensions": [],
               "executionPermission": execution_permission, "childLimit": child_limit,
               "configuration": _digest(settings),
               "sessionPolicy": _digest({"protocol": PROTOCOL_BASELINE, "supervision": True, "startupCwd": "sessionHome",
                    "permissions": session_config({}, workspace=Path("workspace"), private=Path("private"),
                                                  execution_permission=execution_permission, child_limit=child_limit)})}
    if resume and resume.get("binding") != binding:
        error = ValueError("Session reference belongs to another binding or configuration")
        error.resume_binding_changed = True
        raise error
    owner = (binding["profile"], binding["plugin"], key)
    with _live_lock:
        if owner in _live:
            raise RuntimeError("A session is already open for this conversation")
        session = PluginLlmSession(ctx, owner, binding, workspace_handle, snapshot, instructions,
                                   tools, resume, on_event, on_tool_call, settings, consumer_key)
        _live[owner] = session
    return session


class PluginLlmSession:
    def __init__(self, ctx, owner, binding, handle, snapshot, instructions, tools,
                 resume, on_event, on_tool_call, settings, consumer_key=None):
        self._ctx, self._owner, self._binding = ctx, owner, binding
        self._handle, self._snapshot = handle, snapshot
        self._instructions, self._tools = instructions, json.loads(json.dumps(tools))
        self._settings = settings
        self._loop = asyncio.get_running_loop()
        self._commands = queue.Queue()
        self._ready = concurrent.futures.Future()
        self._done = concurrent.futures.Future()
        self._on_event, self._on_tool_call = on_event, on_tool_call
        self._callbacks: set[asyncio.Task] = set()
        self._thread_id = resume.get("threadId") if resume else None
        self._turn_id = None
        self._closed = False
        self._starting = False
        self._responding = 0
        self._requests = set()
        self._request_threads = {}
        self._waiting = set()
        self._last_activity = time.monotonic()
        self._started_at = None
        self._active_elapsed = 0.0
        self._reference = None
        self._registration = None
        self._credentials = None
        self._account_id = resume.get("accountId") if resume else None
        self._event_lock = asyncio.Lock()
        self._children = set(resume.get("childThreadIds", [])) if resume else set()
        self._active_children = set()
        self._executing_children = set()
        self._last_child_poll = 0.0
        self._root_agent_waits = set()
        self._execution_id = uuid.uuid4().hex
        self._state_root = None
        self._consumer_key = consumer_key
        self._new_thread = not bool(resume)
        self._turn_submitted = False
        self._native_spawn_attempted = False

    @property
    def reference(self):
        return json.loads(json.dumps({"version": 1, "binding": self._binding,
                                      "threadId": self._thread_id, "accountId": self._account_id,
                                      "childThreadIds": sorted(self._children), "executionId": self._execution_id,
                                      "consumerKey": self._consumer_key,
                                      "newThread": self._new_thread, "turnSubmissionAttempted": self._turn_submitted}))

    @property
    def is_closed(self):
        """The worker exited; close() still establishes cleanup or raises."""
        return self._done.done()

    async def _open(self):
        context = contextvars.copy_context()
        threading.Thread(target=context.run, args=(self._worker,), daemon=True,
                         name="plugin-codex-session").start()
        await asyncio.shield(asyncio.wrap_future(self._ready))

    async def _rpc(self, method, params=None):
        if self._closed:
            raise RuntimeError("Session is closed; resume its saved reference")
        result = concurrent.futures.Future()
        self._commands.put((method, params or {}, result))
        return await asyncio.wrap_future(result)

    async def start_turn(self, inputs):
        if self._starting or self._turn_id:
            raise RuntimeError("Only one turn may run per conversation")
        cfg_limit = _capacity_limit(self._ctx)
        with _live_lock:
            active = sum(s._starting or s._responding > 0 or s._is_executing() for s in _live.values()
                         if s._owner[0] == self._owner[0])
            if active >= cfg_limit and not (self._responding or self._is_executing()):
                raise RuntimeError("Agent execution capacity is occupied")
            self._starting = True
        try:
            items = [{"type": "text", "text": inputs}] if isinstance(inputs, str) else inputs
            result = await self._rpc("turn/start", {"threadId": self._thread_id, "input": items})
            return {"turnId": result["turn"]["id"]}
        finally:
            self._starting = False

    async def steer(self, inputs):
        if not self._turn_id:
            return {"accepted": False}
        turn_id = self._turn_id
        items = [{"type": "text", "text": inputs}] if isinstance(inputs, str) else inputs
        await self._rpc("turn/steer", {"threadId": self._thread_id,
                                      "expectedTurnId": turn_id, "input": items})
        return {"accepted": True, "turnId": turn_id}

    async def inspect(self):
        """Read native history for this bound thread without starting a turn.

        This reports native state only. The caller must also inspect the pinned
        workspace and prove process cleanup before continuing expired execution.
        """
        result = await self._rpc("thread/read", {"threadId": self._thread_id, "includeTurns": True})
        thread = result["thread"]
        if thread["id"] != self._thread_id:
            raise RuntimeError("Inspected another Codex thread")
        children = []
        for child in sorted(self._children):
            inspected = await self._rpc("thread/read", {"threadId": child, "includeTurns": True})
            if inspected["thread"]["id"] != child:
                raise RuntimeError("Inspected another native child thread")
            children.append(inspected["thread"])
        return {"reference": self.reference, "thread": thread,
                "children": children, "activeTurnId": self._turn_id,
                "pendingRequestIds": list(self._requests)}

    async def respond(self, request_id, result):
        reserved = False
        while True:
            limit = _capacity_limit(self._ctx)
            with _live_lock:
                if self._closed:
                    raise RuntimeError("Session is closed; resume its saved reference")
                if request_id not in self._requests:
                    raise ValueError("Unknown or already answered session request")
                active = sum(s._starting or s._responding > 0 or s._is_executing() for s in _live.values() if s._owner[0] == self._owner[0])
                needs_capacity = bool(self._turn_id or self._active_children or request_id in self._waiting)
                if not needs_capacity:
                    self._requests.remove(request_id)
                    break
                if self._responding or self._is_executing() or active < limit:
                    self._responding += 1
                    reserved = True
                    self._requests.remove(request_id)
                    break
            self._last_activity = time.monotonic()
            await asyncio.sleep(0.05)
        try:
            return await self._rpc("$respond", {"id": request_id, "result": result})
        finally:
            self._clear_waiting(request_id)
            if reserved:
                with _live_lock:
                    self._responding -= 1

    def _wait_for_response(self, request_id):
        now = time.monotonic()
        self._waiting.add(request_id)
        thread = self._request_threads.get(request_id)
        if thread in self._children:
            self._executing_children.discard(thread)
        if not self._is_executing() and self._started_at is not None:
            self._active_elapsed += now - self._started_at
            self._started_at = None
        self._last_activity = now

    def _clear_waiting(self, request_id):
        was_waiting = request_id in self._waiting
        self._waiting.discard(request_id)
        thread = self._request_threads.pop(request_id, None)
        if was_waiting and thread in self._active_children and not any(self._request_threads.get(r) == thread for r in self._waiting):
            self._executing_children.add(thread)
        if was_waiting and self._is_executing() and self._started_at is None:
            self._started_at = time.monotonic()

    def _is_executing(self):
        root_waiting = any(self._request_threads.get(request) == self._thread_id for request in self._waiting)
        root_waiting = root_waiting or bool(self._root_agent_waits and self._waiting)
        return bool(self._executing_children or self._turn_id and not root_waiting)

    async def interrupt(self):
        if self._turn_id:
            await self._rpc("turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id})

    def _request_close(self):
        if not self._closed:
            self._closed = True
            self._commands.put(("$close", {}, concurrent.futures.Future()))

    def _dispose_registration(self):
        if self._registration is not None:
            self._registration.dispose()

    async def close(self):
        self._request_close()
        try:
            await asyncio.shield(asyncio.wrap_future(self._done))
        finally:
            callbacks = [task for task in self._callbacks if task is not asyncio.current_task()]
            for callback in callbacks:
                callback.cancel()
            await asyncio.gather(*callbacks, return_exceptions=True)
            self._requests.clear()
            self._request_threads.clear()
            self._waiting.clear()
            with _live_lock:
                if self._done.done() and _live.get(self._owner) is self:
                    del _live[self._owner]

    async def _dispatch(self, message, server_request=False):
        try:
            if server_request:
                request_id = message["id"]
                self._requests.add(request_id)
                method, params = message["method"], message.get("params", {})
                self._request_threads[request_id] = params.get("threadId") or self._thread_id
                if method == "item/tool/call":
                    if params.get("tool") not in {t["name"] for t in self._tools}:
                        await self.respond(request_id, {"success": False, "contentItems": [
                            {"type": "inputText", "text": "Unknown scoped tool."}]})
                        return
                    if self._on_tool_call is None:
                        raise ValueError("No tool dispatcher")
                    result = self._on_tool_call({**params, "requestId": request_id})
                    if inspect.isawaitable(result):
                        result = await result
                    if result is None:
                        self._wait_for_response(request_id)
                        return  # durable interaction will call respond later
                    result = {"success": True, "contentItems": [
                        {"type": "inputText", "text": json.dumps(result)}]}
                elif method in {"item/tool/requestUserInput", "mcpServer/elicitation/request"}:
                    self._wait_for_response(request_id)
                    if self._on_event is not None:
                        result = self._on_event({"method": "session/question", "requestId": request_id,
                                                 "kind": method, "params": params})
                        if inspect.isawaitable(result):
                            result = await result
                        if result is None:
                            return
                    else:
                        raise ValueError("No durable question handler")
                else:
                    handlers = {
                        "item/permissions/requestApproval": lambda: {"permissions": {}, "scope": "turn"},
                        "item/commandExecution/requestApproval": lambda: {"decision": "decline"},
                        "item/fileChange/requestApproval": lambda: {"decision": "decline"},
                        "execCommandApproval": lambda: {"decision": "abort"},
                        "applyPatchApproval": lambda: {"decision": "abort"},
                        "currentTime/read": lambda: {"currentTimeAt": int(time.time())},
                    }
                    if method not in handlers:
                        await self._rpc("$error", {"id": request_id})
                        self._requests.discard(request_id)
                        self._clear_waiting(request_id)
                        return
                    result = handlers[method]()
                await self.respond(request_id, result)
            elif self._on_event:
                # Preserve native notification order across async persistence
                # callbacks. Server requests remain independent for Stop/answers.
                async with self._event_lock:
                    result = self._on_event(message)
                    if inspect.isawaitable(result):
                        await result
        except asyncio.CancelledError:
            raise
        except Exception:
            if server_request and message["id"] in self._requests and not self._closed:
                # A callback may have committed a mutation before failing. Do
                # not report a failed tool and invite an unproven retry.
                self._requests.discard(message["id"])
                self._clear_waiting(message["id"])
                await self.close()
            elif not self._closed:
                # A failed persistence callback must stop execution, not silently
                # continue with unrecorded side effects.
                await self.close()

    def _deliver(self, message, server_request=False):
        def schedule():
            task = self._loop.create_task(self._dispatch(message, server_request))
            self._callbacks.add(task)
            task.add_done_callback(self._callback_done)
        self._loop.call_soon_threadsafe(schedule)

    def _callback_done(self, task):
        self._callbacks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                log.error("Native session callback terminated: %s", type(error).__name__)

    def _register_children(self, children):
        selected = set(children)
        if self._thread_id in selected or len(self._active_children | selected) > self._binding["childLimit"]:
            raise RuntimeError("Native session exceeded its child thread limit")
        self._children.update(selected)
        self._active_children.update(selected)
        self._executing_children.update(selected)
        if selected and self._started_at is None:
            self._started_at = time.monotonic()
        from agent.plugin_session_recovery import persist_reference
        persist_reference(self._state_root, self.reference)

    def _poll_children(self, client):
        now = time.monotonic()
        if not self._active_children or now - self._last_child_poll < 1:
            return
        self._last_child_poll = now
        executing = set()
        for child in list(self._active_children):
            thread = client.request("thread/read", {"threadId": child, "includeTurns": False})["thread"]
            status = thread.get("status", {"type": "active"})
            waiting = any(self._request_threads.get(r) == child for r in self._waiting)
            if status.get("type") == "active" and not status.get("activeFlags") and not waiting:
                executing.add(child)
        self._executing_children = executing
        if self._is_executing() and self._started_at is None:
            self._started_at = now
        elif not self._is_executing() and self._started_at is not None:
            self._active_elapsed += now - self._started_at
            self._started_at = None

    def _adopt_request_thread(self, client, thread_id):
        chain, current = [], thread_id
        known = {self._thread_id, *self._children}
        while current not in known:
            if current in chain or len(chain) >= self._binding["childLimit"]:
                return False
            thread = client.request("thread/read", {"threadId": current, "includeTurns": False})["thread"]
            if thread.get("id") != current or not thread.get("parentThreadId"):
                return False
            chain.append(current)
            current = thread["parentThreadId"]
        if chain:
            self._register_children(chain)
        return True

    def _configure_native(self, root, codex_bin):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in ("tmp", "home"):
            (root / name).mkdir(mode=0o700, exist_ok=True)
        # Codex auth stays outside the readable permission roots.
        codex_config = self._settings["native"]
        # Remove credentials left by older session implementations. Native
        # execution never owns a rotating refresh token.
        (root / "auth.json").unlink(missing_ok=True)
        selected = session_config(codex_config, workspace=Path(self._snapshot["path"]), private=root,
                                  execution_permission=self._binding["executionPermission"],
                                  child_limit=self._binding["childLimit"])
        # Codex re-execs its own binary inside bwrap. User installations
        # commonly live under ~/.codex; allow the executable files only,
        # while keeping authentication and all other package state hidden.
        executable = Path(shutil.which(codex_bin) or codex_bin).resolve()
        runtime_files = selected["permissions"]["plugin-workspace"]["filesystem"]
        runtime_files[str(executable)] = "read"
        code_host = executable.parent / "codex-code-mode-host"
        if code_host.is_file():
            runtime_files[str(code_host)] = "read"
        task_config = self._settings["auxiliary"]
        provider = str(task_config.get("provider") or "auto")
        if provider not in {"auto", "openai-codex", "codex"}:
            raise ValueError("The session auxiliary task must select Codex")
        if task_config.get("model"):
            selected["model"] = task_config["model"]
        if task_config.get("reasoning_effort"):
            selected["model_reasoning_effort"] = task_config["reasoning_effort"]
        selected_provider = selected.get("model_provider", "openai")
        provider_config = selected.get("model_providers", {}).get(selected_provider, {})
        if selected_provider == "openai" or provider_config.get("requires_openai_auth", False):
            self._credentials = session_credentials(self._binding["profile"], previous_account=self._account_id)
            if self._credentials:
                self._account_id = self._credentials["chatgptAccountId"]
        elif self._account_id:
            raise PermissionError("Resumed session provider no longer owns its account")
        (root / "config.toml").write_text("\n".join(f"{json.dumps(k)} = {_toml(v)}" for k, v in selected.items()))
        (root / "config.toml").chmod(0o600)
        return selected

    def _worker(self):
        client = None
        home_token = secret_token = None
        worker_failure = None
        try:
            from hermes_constants import set_hermes_home_override, reset_hermes_home_override
            from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope
            profile_home = self._ctx.workspaces._home_path
            home_token = set_hermes_home_override(profile_home)
            secret_token = set_secret_scope(build_profile_secret_scope(profile_home))
            codex_bin = self._settings["binary"]
            ok, version = check_codex_binary(codex_bin, PROTOCOL_BASELINE)
            if not ok or version != ".".join(map(str, PROTOCOL_BASELINE)):
                raise RuntimeError("Plugin sessions require the protocol-tested Codex 0.161.0")
            from agent.plugin_session_recovery import session_root
            root = session_root(self._ctx, self._binding)
            self._state_root = root
            selected = self._configure_native(root, codex_bin)
            # Native shell execution uses the existing durable terminal pin. Its
            # receipt prevents reconnect/release even after an owner crash.
            with self._ctx.workspaces._pin_dispatch(self._handle, dispatch_kind="terminal",
                                                    minimum_ttl_seconds=2100,
                                                    require_quiescent=lambda: client is None or client.is_quiescent()) as pinned:
                from agent.plugin_session_recovery import execution, persist_reference
                supervision = execution(root, self._binding, dispatch_id=pinned.dispatch_id)
                self._execution_id = supervision["nonce"]
                persist_reference(root, self.reference)
                self._native_spawn_attempted = True
                client = CodexAppServerClient(codex_bin=codex_bin, codex_home=str(root),
                                              extra_args=[arg for k, v in selected.items()
                                                          for arg in ("-c", f"{k}={_toml(v)}")],
                                              env={"TMPDIR": str(root / "tmp")}, supervision=supervision)
                try:
                    client.initialize(capabilities={"experimentalApi": True})
                    if self._credentials:
                        client.request("account/login/start", {"type": "chatgptAuthTokens", **self._credentials})
                    params = {"cwd": self._snapshot["path"], "permissions": "plugin-workspace",
                              "approvalPolicy": "never", "developerInstructions": self._instructions,
                              "dynamicTools": self._tools, "experimentalRawEvents": False}
                    if selected.get("model"):
                        params["model"] = selected["model"]
                    if self._thread_id:
                        result = client.request("thread/resume", {**params, "threadId": self._thread_id})
                        if result["thread"]["id"] != self._thread_id:
                            raise RuntimeError("Resumed another Codex thread")
                        pending = set(self._children)
                        verified = {self._thread_id}
                        while pending:
                            before = len(pending)
                            for child in list(pending):
                                native = client.request("thread/read", {"threadId": child, "includeTurns": False})["thread"]
                                if native["id"] == child and native.get("parentThreadId") in verified:
                                    verified.add(child)
                                    pending.remove(child)
                            if len(pending) == before:
                                raise ValueError("Resume reference contains unrelated native child threads")
                    else:
                        self._thread_id = client.request("thread/start", params)["thread"]["id"]
                    persist_reference(root, self.reference)
                    self._ready.set_result(True)
                    self._pump(client)
                finally:
                    client.close()
        except BaseException as exc:
            if not self._native_spawn_attempted:
                # Configuration and pin acquisition can fail before any native
                # process exists. Preserve the original exception type while
                # distinguishing this known pre-execution failure for callers.
                exc.native_execution_started = False
            worker_failure = exc
            if not self._ready.done():
                self._ready.set_exception(exc)
            else:
                self._deliver({"method": "session/failed", "params": {"recoverable": True}})
        finally:
            if secret_token is not None:
                reset_secret_scope(secret_token)
            if home_token is not None:
                reset_hermes_home_override(home_token)
            self._closed = True
            while True:
                try:
                    _, _, future = self._commands.get_nowait()
                except queue.Empty:
                    break
                if not future.done():
                    future.set_exception(RuntimeError("Codex session closed"))
            if worker_failure is not None:
                self._done.set_exception(worker_failure)
            else:
                self._done.set_result(True)
            self._loop.call_soon_threadsafe(self._dispose_registration)
            with _live_lock:
                if _live.get(self._owner) is self:
                    del _live[self._owner]

    def _notification(self, note, now):
        params = note.get("params") or {}
        thread = params.get("thread") or {}
        if note["method"] == "thread/started" and thread.get("parentThreadId") in {self._thread_id, *self._children}:
            self._register_children([thread["id"]])
        item = params.get("item") or {}
        if item.get("type") == "collabAgentToolCall" and item.get("tool") == "wait" and item.get("senderThreadId") == self._thread_id:
            if note["method"] == "item/started":
                self._root_agent_waits.add(item["id"])
            elif note["method"] == "item/completed":
                self._root_agent_waits.discard(item["id"])
        if (note["method"] == "item/completed" and item.get("type") == "collabAgentToolCall"
                and item.get("status") == "completed" and item.get("senderThreadId") in {self._thread_id, *self._children}):
            if item.get("tool") in {"spawnAgent", "resumeAgent"}:
                self._register_children(item.get("receiverThreadIds", []))
            elif item.get("tool") == "closeAgent":
                self._active_children.difference_update(item.get("receiverThreadIds", []))
                self._executing_children.difference_update(item.get("receiverThreadIds", []))
        observed_thread = params.get("threadId") or thread.get("id")
        if observed_thread not in {None, self._thread_id, *self._children}:
            return
        if note["method"] == "thread/closed":
            self._active_children.discard(observed_thread)
            self._executing_children.discard(observed_thread)
        if observed_thread in self._children:
            note = {**note, "params": {**params, "sessionRootThreadId": self._thread_id}}
        if observed_thread not in self._children and note["method"] == "turn/completed" and (
            (params.get("turn") or {}).get("id") == self._turn_id
        ):
            self._turn_id = None
            if not self._executing_children:
                self._started_at = None
                self._active_elapsed = 0.0
            self._last_activity = now
        self._deliver(note)

    def _server_request(self, client, request):
        request_thread = (request.get("params") or {}).get("threadId")
        if request_thread not in {None, self._thread_id, *self._children}:
            if not self._adopt_request_thread(client, request_thread):
                client.respond_error(request["id"], -32602, "Request belongs to another native thread")
                return
        if request.get("method") == "account/chatgptAuthTokens/refresh":
            try:
                if self._credentials is None:
                    raise PermissionError("No host-owned Codex credential for this session")
                self._credentials = session_credentials(
                    self._binding["profile"], rejected_token=self._credentials["accessToken"],
                    previous_account=(request.get("params") or {}).get("previousAccountId")
                                     or self._credentials["chatgptAccountId"])
                client.respond(request["id"], self._credentials)
            except Exception:
                client.respond_error(request["id"], -32000, "Host credential refresh failed")
                raise RuntimeError("Host credential refresh failed") from None
        else:
            self._deliver(request, True)

    def _command(self, client, method, params, future, now):
        self._last_activity = now
        try:
            if method == "$close":
                future.set_result(None)
                return False
            if method == "$respond":
                result = client.respond(params["id"], params["result"])
            elif method == "$error":
                result = client.respond_error(params["id"], -32601, "Unsupported plugin session request")
            else:
                if method in {"turn/start", "turn/steer"}:
                    from agent.plugin_session_recovery import persist_reference
                    # Write ahead of the RPC. Absence of native history alone
                    # never proves that a submitted turn did not run.
                    self._turn_submitted = True
                    persist_reference(self._state_root, self.reference)
                result = client.request(method, params)
                if method == "turn/start":
                    self._turn_id = result["turn"]["id"]
                    if not self._executing_children:
                        self._started_at = time.monotonic()
                        self._active_elapsed = 0.0
            if not future.done():
                future.set_result(result)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
        return True

    def _pump(self, client):
        while not self._closed and client.is_alive():
            now = time.monotonic()
            active_elapsed = self._active_elapsed + (now - self._started_at if self._started_at is not None else 0)
            if self._is_executing() and active_elapsed > 1800:
                if self._turn_id:
                    client.request("turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id})
                self._deliver({"method": "session/timeout", "params": {}})
                return
            if not self._is_executing() and not self._starting and now - self._last_activity > 120:
                return
            try:
                method, params, future = self._commands.get(timeout=0.01)
            except queue.Empty:
                method = None
            if method and not self._command(client, method, params, future, now):
                return
            while note := client.take_notification():
                self._notification(note, now)
            while request := client.take_server_request():
                self._server_request(client, request)
            self._poll_children(client)
        if not self._closed:
            raise RuntimeError("Codex transport exited during the session")
