"""Session isolation, resumability and asynchronous callback contracts."""
import asyncio
import base64
import json
import queue
import shlex
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import psutil

from agent import plugin_llm_sessions as sessions
from hermes_cli.plugin_workspaces import InvalidWorkspaceHandleError, WorkspaceInUseError, WorkspaceLeaseError
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_constants import hermes_home_key


@pytest.fixture(autouse=True)
def isolate_session_credentials(monkeypatch):
    # Real protocol fixtures select a provider without authentication. Fake
    # transports must never resolve a developer's production OAuth grant.
    monkeypatch.setattr(sessions, "session_credentials", lambda *a, **k: None)


class Transport:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.notes, self.requests = queue.Queue(), queue.Queue()
        self.calls, self.replies = [], []
        self.alive = True
        self.instances.append(self)

    def initialize(self, **kwargs):
        assert kwargs["capabilities"]["experimentalApi"]

    def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": params.get("threadId", "thread-1")}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "turns": []}}
        return {}

    def respond(self, rid, result):
        self.replies.append((rid, result))

    def respond_error(self, *args):
        self.replies.append(args)

    def take_notification(self):
        try:
            return self.notes.get_nowait()
        except queue.Empty:
            return None

    def take_server_request(self):
        try:
            return self.requests.get_nowait()
        except queue.Empty:
            return None

    def is_alive(self):
        return self.alive

    def close(self):
        self.alive = False

    def is_quiescent(self):
        return not self.alive


def context(home):
    home.mkdir(parents=True, exist_ok=True)
    return PluginContext(PluginManifest(name="session-test", source="user"),
                         PluginManager(scope_key=hermes_home_key(home)))


@pytest.mark.asyncio
async def test_pin_scope_resume_and_nonblocking_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "CodexAppServerClient", Transport)
    monkeypatch.setattr(sessions, "check_codex_binary", lambda *a: (True, "0.161.0"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tools = [{"type": "function", "name": "hold", "description": "Wait",
              "inputSchema": {"type": "object"}}]
    blocked, entered = asyncio.Event(), asyncio.Event()

    async def tool(params):
        entered.set()
        await blocked.wait()
        return {"saved": True}

    ctx = context(tmp_path / "a")
    registration = ctx.register_auxiliary_task("session_contract", display_name="Session contract",
                                                description="Isolated execution contract")
    handle = ctx.workspaces.acquire("test", intent=ctx.workspaces.new_intent())
    with pytest.raises(PermissionError):
        await ctx.llm.aopen_session(task="foreign_task", key="conversation", workspace_handle=handle,
                                    instructions="Stable", tools=tools)
    ctx.manifest.source = "project"
    with pytest.raises(PermissionError, match="installed trusted"):
        await ctx.llm.aopen_session(task="session_contract", key="conversation", workspace_handle=handle,
                                    instructions="Stable", tools=tools)
    ctx.manifest.source = "user"
    session = await ctx.llm.aopen_session(task="session_contract", key="conversation", workspace_handle=handle,
                                         instructions="Stable", tools=tools, on_tool_call=tool)
    transport = Transport.instances[-1]
    try:
        await session.start_turn("Hello")
        transport.requests.put({"id": 1, "method": "item/tool/call", "params": {"tool": "hold"}})
        await asyncio.wait_for(entered.wait(), 5)
        assert (await session.steer("New direction"))["accepted"]
        await asyncio.wait_for(session.interrupt(), 5)
        ref = session.reference
        with pytest.raises(ValueError, match="another binding"):
            await ctx.llm.aopen_session(task="session_contract", key="other", workspace_handle=handle,
                                        instructions="Stable", tools=tools, resume=ref)
    finally:
        await session.close()
    assert not transport.alive
    assert not blocked.is_set()  # close cancelled the waiting callback
    resumed = await ctx.llm.aopen_session(task="session_contract", key="conversation", workspace_handle=handle,
                                         instructions="Stable", tools=tools, resume=ref)
    await resumed.close()
    registration.dispose()
    assert Transport.instances[-1].calls[0][0] == "thread/resume"
    ctx.workspaces.release(handle)
    other = context(tmp_path / "b")
    with pytest.raises(InvalidWorkspaceHandleError):
        other.workspaces.inspect(handle)
    assert json.loads(json.dumps(ref)) == ref

    # Failed cleanup proof must retain the existing durable dispatch receipt,
    # even if the process itself appears to have stopped.
    uncertain = ctx.workspaces.acquire("uncertain", intent=ctx.workspaces.new_intent())
    monkeypatch.setattr(Transport, "is_quiescent", lambda self: False)
    unproven = await sessions.open_session(ctx, task="test", key="uncertain", workspace_handle=uncertain,
                                          instructions="Stable", tools=tools)
    with pytest.raises(WorkspaceLeaseError, match="retaining workspace dispatch"):
        await unproven.close()
    with pytest.raises(WorkspaceInUseError, match="dispatch"):
        ctx.workspaces.release(uncertain)


@pytest.mark.asyncio
async def test_active_turn_deadline_survives_question_suspension(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(sessions, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(sessions, "CodexAppServerClient", Transport)
    monkeypatch.setattr(sessions, "check_codex_binary", lambda *a: (True, "0.161.0"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    waiting, timed_out = asyncio.Event(), asyncio.Event()

    def ask(params):
        waiting.set()
        return None

    def event(message):
        if message["method"] == "session/timeout":
            timed_out.set()

    ctx = context(tmp_path / "profile")
    handle = ctx.workspaces.acquire("deadline", intent=ctx.workspaces.new_intent())
    session = await sessions.open_session(ctx, task="test", key="deadline", workspace_handle=handle,
        instructions="Stable", tools=[{"type": "function", "name": "ask", "description": "Ask",
                                     "inputSchema": {"type": "object"}}], on_event=event, on_tool_call=ask)
    try:
        await session.start_turn("Investigate")
        clock[0] += 1000
        Transport.instances[-1].requests.put({"id": 3, "method": "item/tool/call", "params": {"tool": "ask"}})
        await asyncio.wait_for(waiting.wait(), 5)
        clock[0] += 100  # waiting does not consume the execution budget
        await session.respond(3, {"success": True, "contentItems": []})
        clock[0] += 801  # cumulative active time now exceeds 30 minutes
        await asyncio.wait_for(timed_out.wait(), 5)
    finally:
        await session.close()
        ctx.workspaces.release(handle)


@pytest.mark.asyncio
@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("execution_permission", ["read", "write"])
@pytest.mark.parametrize("external_auth", [False, True])
async def test_real_app_server_registers_function_and_resumes(tmp_path, monkeypatch, execution_permission, external_auth):
    """Real Codex, local inference fixture: tools, completion and persisted resume."""
    ok, version = sessions.check_codex_binary("codex", sessions.PROTOCOL_BASELINE)
    if not ok or version != "0.161.0":
        pytest.skip("Pinned Codex 0.161.0 is not installed")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []
    refreshes = []
    unauthorized = [False]
    access_token = "header." + base64.urlsafe_b64encode(json.dumps({
        "email": "fixture@example.invalid", "exp": 4102444800,
        "https://api.openai.com/auth": {"chatgpt_account_id": "fixture-account", "chatgpt_plan_type": "plus"},
    }).encode()).decode().rstrip("=") + ".signature"
    if external_auth:
        def credentials(*args, **kwargs):
            if kwargs.get("rejected_token"):
                refreshes.append(kwargs)
            return {"accessToken": access_token, "chatgptAccountId": "fixture-account"}
        monkeypatch.setattr(sessions, "session_credentials", credentials)
    clients = []
    real_client = sessions.CodexAppServerClient

    def observed_client(**kwargs):
        client = real_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(sessions, "CodexAppServerClient", observed_client)
    forbidden = tmp_path / "unrelated-secret"
    forbidden.write_text("test-only-secret")
    probe = (
        "import os, pathlib, urllib.request\n" +
        ("pathlib.Path('workspace-probe').write_text('ok')\n" if execution_permission == "write" else
         "try:\n pathlib.Path('workspace-probe').write_text('unsafe')\n"
         "except (PermissionError, OSError):\n pass\n"
         "else:\n raise AssertionError('Read session modified repository source')\n") +
        "assert 'GITHUB_USER_TOKEN' not in os.environ\n"
        f"p=pathlib.Path({str(forbidden)!r})\n"
        "try:\n p.read_text()\nexcept (PermissionError, FileNotFoundError):\n pass\n"
        "else:\n raise AssertionError('Unrelated file was readable')\n"
    )
    command_events = []
    child_script = "import time;time.sleep(120)"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"network-ok")

        def do_POST(self):
            if external_auth:
                assert self.headers.get("Authorization") == "Bearer " + access_token
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if external_auth and not unauthorized[0]:
                unauthorized[0] = True
                data = b'{"error":{"message":"Fixture requests host credential refresh","type":"invalid_request_error"}}'
                self.send_response(401)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
                return
            calls.append(body)
            item = ({"type": "function_call", "id": "fc_0", "call_id": "call_0",
                     "name": "exec_command", "arguments": json.dumps({"cmd": "python3 -c " + shlex.quote(probe), "max_output_tokens": 1000})} if len(calls) == 1 else
                    {"type": "function_call", "id": "fc_child", "call_id": "call_child",
                     "name": "exec_command", "arguments": json.dumps({"cmd": "python3 -c " + shlex.quote(
                         child_script),
                         "yield_time_ms": 1000, "max_output_tokens": 100})} if len(calls) == 2 else
                    {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                     "name": "save", "arguments": "{}"} if len(calls) == 3 else
                    {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                     "content": [{"type": "output_text", "text": "Saved.", "annotations": []}]})
            events = [
                {"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": "resp_1", "status": "completed",
                 "output": [item], "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}},
            ]
            data = "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    probe += f"assert urllib.request.urlopen('http://127.0.0.1:{server.server_port}/probe').read() == b'network-ok'\nprint('PROBE_OK')\n"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model="fixture"\nmodel_provider="fixture"\n[model_providers.fixture]\n'
        'name="Local fixture"\nwire_api="responses"\n'
        f'requires_openai_auth={str(external_auth).lower()}\n'
        f'base_url="http://127.0.0.1:{server.server_port}/v1"\nsupports_websockets=false\n')
    completed, tool_called = asyncio.Event(), asyncio.Event()
    pending_requests = []

    async def on_event(event):
        if event["method"] == "turn/completed":
            completed.set()
        if event["method"] == "item/completed" and event.get("params", {}).get("item", {}).get("type") == "commandExecution":
            command_events.append(event["params"]["item"])

    async def on_tool_call(params):
        assert params["tool"] == "save"
        pending_requests.append(params["requestId"])
        tool_called.set()
        return None  # the product has persisted a question before suspension
    ctx = context(tmp_path / "profile")
    (tmp_path / "profile" / ".env").write_text("GITHUB_USER_TOKEN=test-only-token\n")
    handle = ctx.workspaces.acquire("native", intent=ctx.workspaces.new_intent())
    kwargs = dict(task="test", key="native", workspace_handle=handle, instructions="Stable",
                  execution_permission=execution_permission,
                  child_limit=1 if execution_permission == "read" else 4,
                  tools=[{"type": "function", "name": "save", "description": "Save private result",
                          "inputSchema": {"type": "object", "properties": {}}}],
                  on_event=on_event, on_tool_call=on_tool_call)
    session = await sessions.open_session(ctx, **kwargs)
    try:
        await session.start_turn("Save the result.")
        await asyncio.wait_for(tool_called.wait(), 20)
        assert (await session.steer("Use the approved scope."))["accepted"]
        await session.respond(pending_requests[0], {"success": True, "contentItems": [
            {"type": "inputText", "text": "The operator answered: continue."}]})
        with pytest.raises(ValueError, match="already answered"):
            await session.respond(pending_requests[0], {})
        await asyncio.wait_for(completed.wait(), 20)
        assert tool_called.is_set()
        assert len(calls) == 4
        if external_auth:
            assert refreshes
            assert all(refresh["previous_account"] == "fixture-account" for refresh in refreshes)
        assert any(t.get("name") == "exec_command" for t in calls[0]["tools"]), calls[0]["tools"]
        assert command_events, calls[1]
        assert command_events[0]["exitCode"] == 0, command_events[0].get("aggregatedOutput")
        assert "PROBE_OK" in command_events[0].get("aggregatedOutput", ""), command_events
        workspace = Path(ctx.workspaces.inspect(handle)["path"])
        if execution_permission == "write":
            assert (workspace / "workspace-probe").read_text() == "ok"
        else:
            assert not (workspace / "workspace-probe").exists()
        history = await session.inspect()
        assert history["reference"] == session.reference
        assert history["thread"]["turns"]
        # The sandbox has its own PID namespace; a PID written from within it
        # must never be interpreted as an unrelated host process identity.
        children = []
        for process in psutil.Process().children(recursive=True):
            try:
                if process.cmdline()[-1:] == [child_script]:
                    children.append(process)
            except psutil.NoSuchProcess:
                pass
        assert len(children) == 1
        child = children[0]
        assert child.is_running()
        reference = session.reference
        await session.close()
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE, {
            "root": clients[0]._proc.pid, "child": child.pid,
            "snapshotted": [p.pid for p in clients[0]._retired_descendants],
            "parents": [p.pid for p in child.parents()],
        }
        resumed = await sessions.open_session(ctx, **kwargs, resume=reference)
        assert resumed.reference == reference
        await resumed.close()
        ctx.workspaces.release(handle)
    finally:
        await session.close()
        if "child" in locals() and child.is_running():
            child.kill()
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_permissions_and_child_limits_are_resume_bindings(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "CodexAppServerClient", Transport)
    monkeypatch.setattr(sessions, "check_codex_binary", lambda *a: (True, "0.161.0"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ctx = context(tmp_path / "profile")
    handle = ctx.workspaces.acquire("permissions", intent=ctx.workspaces.new_intent())
    kwargs = dict(task="test", key="permissions", workspace_handle=handle, instructions="Stable", tools=[])
    session = await sessions.open_session(ctx, **kwargs)
    try:
        native = Transport.instances[-1]
        assert (await session.inspect())["thread"]["id"] == session.reference["threadId"]
        config = sessions.session_config({}, workspace=Path("/source"), private=tmp_path)
        assert config["permissions"]["plugin-workspace"]["filesystem"]["/source"] == "read"
        ref = session.reference
    finally:
        await session.close()
    for changes in ({"execution_permission": "write"}, {"child_limit": 1}):
        with pytest.raises(ValueError, match="another binding"):
            await sessions.open_session(ctx, **kwargs, resume=ref, **changes)
    for limit in (-1, 0, 5, True):
        with pytest.raises(ValueError, match="child limit"):
            await sessions.open_session(ctx, **kwargs, child_limit=limit)
    with pytest.raises(ValueError, match="permission"):
        await sessions.open_session(ctx, **kwargs, execution_permission="unrestricted")
    native_config = tmp_path / ".codex" / "config.toml"
    native_config.parent.mkdir(exist_ok=True)
    native_config.write_text('model="changed-model"\n')
    with pytest.raises(ValueError, match="another binding"):
        await sessions.open_session(ctx, **kwargs, resume=ref)
    assert not native.alive
    ctx.workspaces.release(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["item/tool/requestUserInput", "mcpServer/elicitation/request"])
async def test_native_question_routes_and_callback_failure(tmp_path, monkeypatch, method):
    monkeypatch.setattr(sessions, "CodexAppServerClient", Transport)
    monkeypatch.setattr(sessions, "check_codex_binary", lambda *a: (True, "0.161.0"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ctx = context(tmp_path / "profile")
    handle = ctx.workspaces.acquire("question", intent=ctx.workspaces.new_intent())
    delivered = asyncio.Event()

    def on_event(event):
        if event["method"] == "session/question":
            assert event["kind"] == method
            delivered.set()
            return None

    session = await sessions.open_session(ctx, task="test", key="question", workspace_handle=handle,
                                         instructions="Stable", tools=[], on_event=on_event)
    try:
        native = Transport.instances[-1]
        native.requests.put({"id": 7, "method": method, "params": {}})
        await asyncio.wait_for(delivered.wait(), 5)
        assert 7 in (await session.inspect())["pendingRequestIds"]
        await session.respond(7, {"answers": {}})
        assert native.replies == [(7, {"answers": {}})]
        with pytest.raises(ValueError, match="already answered"):
            await session.respond(7, {})
        session._on_event = lambda event: (_ for _ in ()).throw(RuntimeError("Persistence unavailable"))
        await session._dispatch({"id": 8, "method": method, "params": {}}, True)
        assert native.replies[-1][0] == 8
        assert native.replies[-1][1] == -32601
        assert not session._waiting
    finally:
        await session.close()
        ctx.workspaces.release(handle)


@pytest.mark.asyncio
async def test_child_events_are_scoped_and_native_limit_violation_retires_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "CodexAppServerClient", Transport)
    monkeypatch.setattr(sessions, "check_codex_binary", lambda *a: (True, "0.161.0"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ctx = context(tmp_path / "profile")
    handle = ctx.workspaces.acquire("children", intent=ctx.workspaces.new_intent())
    child_seen, failed = asyncio.Event(), asyncio.Event()
    observed = []

    def event(note):
        observed.append(note)
        if note["method"] == "item/completed":
            child_seen.set()
        if note["method"] == "session/failed":
            failed.set()

    session = await sessions.open_session(ctx, task="test", key="children", workspace_handle=handle,
                                         instructions="Stable", tools=[], child_limit=1, on_event=event)
    native = Transport.instances[-1]
    native.notes.put({"method": "thread/started", "params": {"thread": {"id": "child-1", "parentThreadId": "thread-1"}}})
    native.notes.put({"method": "item/completed", "params": {"threadId": "foreign", "item": {"id": "foreign-command"}}})
    native.notes.put({"method": "item/completed", "params": {"threadId": "child-1", "item": {"id": "child-command"}}})
    await asyncio.wait_for(child_seen.wait(), 5)
    assert not any(n.get("params", {}).get("threadId") == "foreign" for n in observed)
    assert observed[-1]["params"]["sessionRootThreadId"] == "thread-1"
    assert session.reference["childThreadIds"] == ["child-1"]
    native.notes.put({"method": "thread/started", "params": {"thread": {"id": "child-2", "parentThreadId": "child-1"}}})
    await asyncio.wait_for(failed.wait(), 5)
    with pytest.raises(RuntimeError, match="child thread limit"):
        await session.close()
    assert session.is_closed
    assert not native.alive
    ctx.workspaces.release(handle)


@pytest.mark.asyncio
async def test_cancel_open_does_not_cancel_worker_ready_or_leak_pin(tmp_path, monkeypatch):
    entered, proceed = threading.Event(), threading.Event()

    def version(*args):
        entered.set()
        assert proceed.wait(timeout=5)
        return True, "0.161.0"

    monkeypatch.setattr(sessions, "CodexAppServerClient", Transport)
    monkeypatch.setattr(sessions, "check_codex_binary", version)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ctx = context(tmp_path / "profile")
    handle = ctx.workspaces.acquire("cancel-open", intent=ctx.workspaces.new_intent())
    opening = asyncio.create_task(sessions.open_session(ctx, task="test", key="cancel-open", workspace_handle=handle,
                                                       instructions="Stable", tools=[]))
    assert await asyncio.to_thread(entered.wait, 5)
    opening.cancel()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(opening, 5)
    assert not Transport.instances[-1].alive
    ctx.workspaces.release(handle)


@pytest.mark.asyncio
async def test_async_notifications_preserve_native_order(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "CodexAppServerClient", Transport)
    monkeypatch.setattr(sessions, "check_codex_binary", lambda *a: (True, "0.161.0"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ctx = context(tmp_path / "profile")
    handle = ctx.workspaces.acquire("events", intent=ctx.workspaces.new_intent())
    first, release, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    saved = []

    async def event(note):
        if note["method"] == "first":
            first.set()
            await release.wait()
        saved.append(note["method"])
        if note["method"] == "second":
            second.set()

    session = await sessions.open_session(ctx, task="test", key="events", workspace_handle=handle,
                                         instructions="Stable", tools=[], on_event=event)
    try:
        native = Transport.instances[-1]
        native.notes.put({"method": "first", "params": {}})
        native.notes.put({"method": "second", "params": {}})
        await asyncio.wait_for(first.wait(), 5)
        assert not second.is_set()
        release.set()
        await asyncio.wait_for(second.wait(), 5)
        assert saved == ["first", "second"]
    finally:
        await session.close()
        ctx.workspaces.release(handle)
