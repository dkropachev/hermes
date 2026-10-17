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
from pathlib import Path

from agent.transports.codex_app_server import CodexAppServerClient, check_codex_binary

PROTOCOL_BASELINE = (0, 161, 0)
_live: dict[tuple[str, str, str], "PluginLlmSession"] = {}
_live_lock = threading.Lock()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(k)}={_toml(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(map(_toml, value)) + "]"
    return json.dumps(value)


def session_config(codex_config: dict, *, workspace: Path, private: Path) -> dict:
    """Construct an isolated configuration; never inherit installed extensions.

    A minimal filesystem allowlist protects other homes/workspaces by default.
    The shell inherits only session-owned HOME/TMPDIR and a normal executable
    search path. Provider credentials remain in the app-server process.
    """
    selected = {k: v for k, v in codex_config.items() if k in {
        "model", "model_provider", "model_providers", "model_reasoning_effort",
        "preferred_auth_method", "cli_auth_credentials_store",
    }}
    selected.update({
        "default_permissions": "plugin-workspace",
        "permissions": {"plugin-workspace": {
            "filesystem": {":minimal": "read", str(workspace): "write",
                           str(private / "tmp"): "write", str(private / "home"): "write"},
            "network": {"enabled": True},
        }},
        "approval_policy": "never", "web_search": "disabled",
        "mcp_servers": {}, "plugins": {}, "notify": [],
        "features": {"apps": False, "plugins": False, "shell_snapshot": False,
                     "codex_hooks": False},
        "agents": {"max_threads": 4},
        "shell_environment_policy": {"inherit": "none", "set": {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(private / "home"),
            "TMPDIR": str(private / "tmp"), "LANG": "C.UTF-8",
        }},
    })
    return selected


async def open_session(ctx, *, task, key, workspace_handle, instructions, tools,
                       resume=None, extensions=(), on_event=None, on_tool_call=None):
    if ctx.manifest.source not in {"bundled", "user", "entrypoint"}:
        raise PermissionError("Native execution sessions require an installed trusted plugin")
    if not key or not isinstance(instructions, str):
        raise ValueError("Session key and instructions are required")
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
    layout = ctx.workspaces._layout()
    binding = {"profile": str(layout.profile_key), "plugin": layout.plugin_identity_digest,
               "key": key, "task": task, "workspace": snapshot["workspaceId"],
               "generation": snapshot["generation"],
               "instructions": _digest(instructions), "tools": _digest(tools), "extensions": []}
    if resume and resume.get("binding") != binding:
        raise ValueError("Session reference belongs to another binding or configuration")
    owner = (binding["profile"], binding["plugin"], key)
    with _live_lock:
        if owner in _live:
            raise RuntimeError("A session is already open for this conversation")
        session = PluginLlmSession(ctx, owner, binding, workspace_handle, snapshot, instructions,
                                   tools, resume, on_event, on_tool_call)
        _live[owner] = session
    try:
        await session._open()
        session._registration = ctx._track("llm_session", key, session._request_close)
        if session._done.done():
            session._registration.dispose()
    except BaseException:
        await session.close()
        raise
    return session


class PluginLlmSession:
    def __init__(self, ctx, owner, binding, handle, snapshot, instructions, tools,
                 resume, on_event, on_tool_call):
        self._ctx, self._owner, self._binding = ctx, owner, binding
        self._handle, self._snapshot = handle, snapshot
        self._instructions, self._tools = instructions, json.loads(json.dumps(tools))
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
        self._requests = set()
        self._waiting = set()
        self._last_activity = time.monotonic()
        self._started_at = None
        self._active_elapsed = 0.0
        self._reference = None
        self._registration = None

    @property
    def reference(self):
        return json.loads(json.dumps({"version": 1, "binding": self._binding,
                                      "threadId": self._thread_id}))

    async def _open(self):
        context = contextvars.copy_context()
        threading.Thread(target=context.run, args=(self._worker,), daemon=True,
                         name="plugin-codex-session").start()
        await asyncio.wrap_future(self._ready)

    async def _rpc(self, method, params=None):
        if self._closed:
            raise RuntimeError("Session is closed; resume its saved reference")
        result = concurrent.futures.Future()
        self._commands.put((method, params or {}, result))
        return await asyncio.wrap_future(result)

    async def start_turn(self, inputs):
        if self._starting or self._turn_id:
            raise RuntimeError("Only one turn may run per conversation")
        with _live_lock:
            cfg_limit = min(4, max(1, int(self._ctx.get_config("active_agent_sessions", 2))))
            active = sum(s._starting or bool(s._turn_id and not s._waiting) for s in _live.values()
                         if s._owner[0] == self._owner[0])
            if active >= cfg_limit:
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
        items = [{"type": "text", "text": inputs}] if isinstance(inputs, str) else inputs
        await self._rpc("turn/steer", {"threadId": self._thread_id,
                                      "expectedTurnId": self._turn_id, "input": items})
        return {"accepted": True}

    async def respond(self, request_id, result):
        if request_id not in self._requests:
            raise ValueError("Unknown or already answered session request")
        self._requests.remove(request_id)
        self._clear_waiting(request_id)
        return await self._rpc("$respond", {"id": request_id, "result": result})

    def _wait_for_response(self, request_id):
        now = time.monotonic()
        if not self._waiting and self._started_at is not None:
            self._active_elapsed += now - self._started_at
            self._started_at = None
        self._waiting.add(request_id)
        self._last_activity = now

    def _clear_waiting(self, request_id):
        was_waiting = request_id in self._waiting
        self._waiting.discard(request_id)
        if was_waiting and not self._waiting and self._turn_id:
            self._started_at = time.monotonic()

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
        await asyncio.wrap_future(self._done)
        for callback in list(self._callbacks):
            if callback is not asyncio.current_task():
                callback.cancel()
        with _live_lock:
            if _live.get(self._owner) is self:
                del _live[self._owner]

    async def _dispatch(self, message, server_request=False):
        try:
            if server_request:
                request_id = message["id"]
                self._requests.add(request_id)
                method, params = message["method"], message.get("params", {})
                if method == "item/tool/call":
                    if params.get("tool") not in {t["name"] for t in self._tools}:
                        raise ValueError("Unknown dynamic tool")
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
                elif method == "item/permissions/requestApproval":
                    result = {"permissions": {}, "scope": "turn"}
                elif method == "item/commandExecution/requestApproval":
                    result = {"decision": "decline"}
                elif method == "item/fileChange/requestApproval":
                    result = {"decision": "decline"}
                else:
                    await self._rpc("$error", {"id": request_id})
                    self._requests.discard(request_id)
                    return
                await self.respond(request_id, result)
            elif self._on_event:
                result = self._on_event(message)
                if inspect.isawaitable(result):
                    await result
        except asyncio.CancelledError:
            raise
        except Exception:
            if server_request and message["id"] in self._requests and not self._closed:
                if message["method"] == "item/tool/call":
                    await self.respond(message["id"], {"success": False, "contentItems": [
                        {"type": "inputText", "text": "The host rejected this request."}]})
                else:
                    self._requests.discard(message["id"])
                    self._clear_waiting(message["id"])
                    await self._rpc("$error", {"id": message["id"]})
            elif not self._closed:
                # A failed persistence callback must stop execution, not silently
                # continue with unrecorded side effects.
                await self.close()

    def _deliver(self, message, server_request=False):
        def schedule():
            task = self._loop.create_task(self._dispatch(message, server_request))
            self._callbacks.add(task)
            task.add_done_callback(self._callbacks.discard)
        self._loop.call_soon_threadsafe(schedule)

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
            from agent.auxiliary_client import _get_auxiliary_task_config
            from hermes_cli.config import load_config_readonly
            from hermes_cli.codex_runtime_switch import get_configured_codex_binary
            from hermes_constants import get_hermes_home

            config = load_config_readonly() or {}
            codex_bin = get_configured_codex_binary(config)
            ok, version = check_codex_binary(codex_bin, PROTOCOL_BASELINE)
            if not ok or version != ".".join(map(str, PROTOCOL_BASELINE)):
                raise RuntimeError("Plugin sessions require the protocol-tested Codex 0.161.0")
            root = Path(get_hermes_home()) / "plugin-sessions" / _digest(self._binding)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            for name in ("tmp", "home"):
                (root / name).mkdir(mode=0o700, exist_ok=True)
            # Codex auth stays outside the readable permission roots.
            codex_home = Path.home() / ".codex"
            codex_config = tomllib.loads((codex_home / "config.toml").read_text()) if (codex_home / "config.toml").exists() else {}
            if (codex_home / "auth.json").exists():
                shutil.copyfile(codex_home / "auth.json", root / "auth.json")
                (root / "auth.json").chmod(0o600)
            selected = session_config(codex_config, workspace=Path(self._snapshot["path"]), private=root)
            # Codex re-execs its own binary inside bwrap. User installations
            # commonly live under ~/.codex; allow the executable files only,
            # while keeping authentication and all other package state hidden.
            executable = Path(shutil.which(codex_bin) or codex_bin).resolve()
            runtime_files = selected["permissions"]["plugin-workspace"]["filesystem"]
            runtime_files[str(executable)] = "read"
            code_host = executable.parent / "codex-code-mode-host"
            if code_host.is_file():
                runtime_files[str(code_host)] = "read"
            task_config = _get_auxiliary_task_config(self._binding["task"])
            provider = str(task_config.get("provider") or "auto")
            if provider not in {"auto", "openai-codex", "codex"}:
                raise ValueError("The session auxiliary task must select Codex")
            if task_config.get("model"):
                selected["model"] = task_config["model"]
            if task_config.get("reasoning_effort"):
                selected["model_reasoning_effort"] = task_config["reasoning_effort"]
            (root / "config.toml").write_text("\n".join(f"{json.dumps(k)} = {_toml(v)}" for k, v in selected.items()))
            (root / "config.toml").chmod(0o600)
            # Native shell execution uses the existing durable terminal pin. Its
            # receipt prevents reconnect/release even after an owner crash.
            with self._ctx.workspaces._pin_dispatch(self._handle, dispatch_kind="terminal",
                                                    minimum_ttl_seconds=2100,
                                                    require_quiescent=lambda: client is None or client.is_quiescent()):
                client = CodexAppServerClient(codex_bin=codex_bin, codex_home=str(root),
                                              extra_args=[arg for k, v in selected.items()
                                                          for arg in ("-c", f"{k}={_toml(v)}")],
                                              env={"TMPDIR": str(root / "tmp")})
                try:
                    client.initialize(capabilities={"experimentalApi": True})
                    params = {"cwd": self._snapshot["path"], "permissions": "plugin-workspace",
                              "approvalPolicy": "never", "developerInstructions": self._instructions,
                              "dynamicTools": self._tools, "experimentalRawEvents": False}
                    if selected.get("model"):
                        params["model"] = selected["model"]
                    if self._thread_id:
                        result = client.request("thread/resume", {**params, "threadId": self._thread_id})
                        if result["thread"]["id"] != self._thread_id:
                            raise RuntimeError("Resumed another Codex thread")
                    else:
                        self._thread_id = client.request("thread/start", params)["thread"]["id"]
                    self._ready.set_result(True)
                    self._pump(client)
                finally:
                    client.close()
        except BaseException as exc:
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

    def _pump(self, client):
        while not self._closed and client.is_alive():
            now = time.monotonic()
            active_elapsed = self._active_elapsed + (now - self._started_at if self._started_at is not None else 0)
            if self._turn_id and active_elapsed > 1800:
                client.request("turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id})
                self._deliver({"method": "session/timeout", "params": {}})
                return
            if (not self._turn_id or self._waiting) and not self._starting and now - self._last_activity > 120:
                return
            try:
                method, params, future = self._commands.get(timeout=0.01)
            except queue.Empty:
                method = None
            if method:
                self._last_activity = now
                try:
                    if method == "$close":
                        future.set_result(None)
                        return
                    if method == "$respond":
                        result = client.respond(params["id"], params["result"])
                    elif method == "$error":
                        result = client.respond_error(params["id"], -32601, "Unsupported plugin session request")
                    else:
                        result = client.request(method, params)
                        if method == "turn/start":
                            self._turn_id = result["turn"]["id"]
                            self._started_at = time.monotonic()
                            self._active_elapsed = 0.0
                    if not future.done():
                        future.set_result(result)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc)
            while note := client.take_notification():
                params = note.get("params") or {}
                if params.get("threadId") not in {None, self._thread_id}:
                    continue
                if note["method"] == "turn/completed":
                    self._turn_id = None
                    self._started_at = None
                    self._active_elapsed = 0.0
                    self._last_activity = now
                self._deliver(note)
            while request := client.take_server_request():
                self._deliver(request, True)
        if not self._closed:
            raise RuntimeError("Codex transport exited during the session")
