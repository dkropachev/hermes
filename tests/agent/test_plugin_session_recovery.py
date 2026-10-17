"""Real owner crash: native history and exact workspace cleanup survive."""
import asyncio
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

import psutil
import pytest

from agent.transports.codex_app_server import check_codex_binary
from hermes_cli.plugin_workspaces import WorkspaceInUseError, WorkspaceLeaseError
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_constants import hermes_home_key

pytestmark = [pytest.mark.linux_only, pytest.mark.live_system_guard_bypass]

OWNER = """
import asyncio,json,sys
from pathlib import Path
from unittest.mock import patch
from hermes_cli.plugins import PluginContext,PluginManager,PluginManifest
from hermes_constants import hermes_home_key

async def main(root):
    home=root/'profile'
    home.mkdir(parents=True,exist_ok=True)
    ctx=PluginContext(PluginManifest(name='recovery-contract',source='user'),PluginManager(scope_key=hermes_home_key(home)))
    ctx.register_auxiliary_task('recovery_contract',display_name='Recovery contract',description='Local crash fixture')
    handle=ctx.workspaces.acquire('crash',intent=ctx.workspaces.new_intent())
    session=await ctx.llm.aopen_session(task='recovery_contract',key='crash',workspace_handle=handle,
        instructions='Execute only the fixture command, then hold.',execution_permission='write',
        tools=[{'type':'function','name':'hold','description':'Wait','inputSchema':{'type':'object'}}],
        on_tool_call=lambda request:None)
    (root/'owner.json').write_text(json.dumps({'handle':handle,'reference':session.reference,'path':ctx.workspaces.inspect(handle)['path']}))
    await session.start_turn('Execute the fixture and hold.')
    await asyncio.Event().wait()

root=Path(sys.argv[1])
with patch.object(Path,'home',return_value=root):
    asyncio.run(main(root))
"""


def context(home):
    home.mkdir(parents=True, exist_ok=True)
    return PluginContext(PluginManifest(name="recovery-contract", source="user"),
                         PluginManager(scope_key=hermes_home_key(home)))


@pytest.mark.asyncio
async def test_owner_crash_recovery_preserves_contents_and_never_replays_native_commands(tmp_path, monkeypatch):
    if check_codex_binary("codex", (0, 161, 0)) != (True, "0.161.0"):
        pytest.skip("Pinned native binary is required for recovery acceptance")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []
    script = "from pathlib import Path;import time;Path('owned-change').write_text('preserved');time.sleep(120)"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            item = ({"type": "function_call", "id": "native-exec", "call_id": "exec-1", "name": "exec_command",
                     "arguments": json.dumps({"cmd": "python3 -c " + shlex.quote(script), "yield_time_ms": 1000})}
                    if len(calls) == 1 else
                    {"type": "function_call", "id": "native-hold", "call_id": "hold-1", "name": "hold", "arguments": "{}"})
            events = [
                {"type": "response.created", "response": {"id": "response", "status": "in_progress"}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": "response", "status": "completed", "output": [item],
                  "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}},
            ]
            data = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    native_home = tmp_path / ".codex"
    native_home.mkdir()
    (native_home / "config.toml").write_text('model="fixture"\nmodel_provider="fixture"\n[model_providers.fixture]\n'
                                             'name="Local fixture"\nwire_api="responses"\nrequires_openai_auth=false\n'
                                             f'base_url="http://127.0.0.1:{server.server_port}/v1"\nsupports_websockets=false\n')
    owner = subprocess.Popen([sys.executable, "-c", OWNER, str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tracked = []
    registration = None
    try:
        deadline = time.monotonic() + 30
        state = None
        while time.monotonic() < deadline:
            if owner.poll() is not None:
                raise AssertionError(owner.stderr.read().decode())
            if (tmp_path / "owner.json").exists():
                state = json.loads((tmp_path / "owner.json").read_text())
                if (Path(state["path"]) / "owned-change").exists():
                    break
            await asyncio.sleep(0.025)
        assert state and (Path(state["path"]) / "owned-change").read_text() == "preserved"
        tracked = psutil.Process(owner.pid).children(recursive=True)
        owner.kill()
        owner.wait(timeout=5)
        ctx = context(tmp_path / "profile")
        registration = ctx.register_auxiliary_task("recovery_contract", display_name="Recovery contract", description="Local fixture")
        deadline = time.monotonic() + 20
        while True:
            inspected = await ctx.llm.ainspect_session(task="recovery_contract", reference=state["reference"])
            if inspected["cleanup"]["quiescent"]:
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(0.025)
        assert inspected["thread"]["turns"]
        assert "owned-change" in json.dumps(inspected["rawHistory"])
        assert inspected["rawHistory"][state["reference"]["threadId"]]["complete"]
        intent = ctx.workspaces.new_intent()
        with pytest.raises(WorkspaceInUseError):
            ctx.workspaces.reconnect(state["handle"], intent=intent, ttl_seconds=2100)
        forged = copy.deepcopy(inspected["cleanup"])
        forged["receipt"]["dispatchId"] = "unrelated"
        with pytest.raises(WorkspaceLeaseError):
            ctx.workspaces._complete_supervised_dispatch(state["handle"], binding=state["reference"]["binding"], cleanup=forged)
        before = len(calls)
        recovered = await ctx.llm.arecover_session(task="recovery_contract", reference=state["reference"], workspace_handle=state["handle"], intent=intent)
        replay = await ctx.llm.arecover_session(task="recovery_contract", reference=state["reference"], workspace_handle=state["handle"], intent=intent)
        assert recovered["workspaceHandle"] == replay["workspaceHandle"]
        assert len(calls) == before
        snapshot = ctx.workspaces.inspect(recovered["workspaceHandle"])
        assert snapshot["generation"] == state["reference"]["binding"]["generation"] + 1
        assert Path(snapshot["path"]) == Path(state["path"])
        assert (Path(snapshot["path"]) / "owned-change").read_text() == "preserved"
        ctx.workspaces.release(recovered["workspaceHandle"])
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        for process in reversed(tracked):
            try:
                if process.is_running():
                    process.kill()
            except psutil.NoSuchProcess:
                pass
        owner.stdout.close()
        owner.stderr.close()
        if registration:
            registration.dispose()
        server.shutdown()
        server.server_close()
