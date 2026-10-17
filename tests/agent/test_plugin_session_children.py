"""Pinned real app-server conformance for native child threads and limits."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shlex
from pathlib import Path
import threading

import pytest

from agent.transports.codex_app_server import check_codex_binary
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_constants import hermes_home_key

pytestmark = [pytest.mark.linux_only, pytest.mark.live_system_guard_bypass]


@pytest.mark.asyncio
async def test_native_children_are_bounded_and_cold_history_discovers_them(tmp_path, monkeypatch):
    if check_codex_binary("codex", (0, 161, 0)) != (True, "0.161.0"):
        pytest.skip("Pinned native binary is required for child conformance")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls, exposed, notes = [], [], []
    root_resumed = threading.Event()
    probes_done = threading.Event()
    finished = set()
    lock = threading.Lock()
    forbidden = tmp_path / "unrelated-secret"
    forbidden.write_text("fixture-only-secret")
    probe = ("from pathlib import Path\n"
             f"try:\n Path({str(forbidden)!r}).read_text()\n"
             "except (PermissionError,FileNotFoundError):\n pass\n"
             "else:\n raise AssertionError('Child read outside its bound workspace')\n"
             "try:\n Path('child-write').write_text('unsafe')\n"
             "except (PermissionError,OSError):\n pass\n"
             "else:\n raise AssertionError('Read-only child modified source')\nprint('CHILD_PROBE_OK')\n")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            first = not calls
            calls.append(body)
            if first:
                tool = next((t for t in body.get("tools", []) if t.get("name") == "spawn_agent"), None)
                namespace = None
                if tool is None:
                    for group in body.get("tools", []):
                        tool = next((t for t in group.get("tools", []) if t.get("name") == "spawn_agent"), None)
                        if tool:
                            namespace = group["name"]
                            break
                exposed.append(tool is not None)
                items = []
                if tool:
                    properties = tool.get("parameters", {}).get("properties", {})
                    for index in range(5):
                        args = {"message": f"CHILD_REQUEST_{index}"}
                        if "fork_context" in properties:
                            args["fork_context"] = False
                        items.append({"type": "function_call", "id": f"spawn-{index}", "call_id": f"spawn-{index}",
                                      "name": "spawn_agent", "arguments": json.dumps(args)})
                        if namespace:
                            items[-1]["namespace"] = namespace
                else:
                    items = [{"type": "message", "id": "no-child-tool", "role": "assistant", "status": "completed",
                              "content": [{"type": "output_text", "text": "No child tool.", "annotations": []}]}]
            else:
                if "ROOT_REQUEST_ONLY" in json.dumps(body.get("input")):
                    root_resumed.set()
                    probes_done.wait(timeout=10)
                    items = [{"type": "message", "id": "done", "role": "assistant", "status": "completed",
                              "content": [{"type": "output_text", "text": "Done.", "annotations": []}]}]
                else:
                    root_resumed.wait(timeout=10)
                    prior = any(i.get("type") == "function_call_output" and i.get("call_id") == "child-probe" for i in body.get("input", []))
                    if not prior:
                        child_probe = (probe + f"assert Path.cwd()==Path({str(workspace)!r})\n"
                            f"try:\n Path({str(private_secret)!r}).read_text()\n"
                            "except (PermissionError,FileNotFoundError):\n pass\n"
                            "else:\n raise AssertionError('Child read host session state')\n")
                        items = [{"type": "function_call", "id": "child-probe", "call_id": "child-probe", "name": "exec_command",
                                  "arguments": json.dumps({"cmd": "python3 -c " + shlex.quote(child_probe), "max_output_tokens": 500})}]
                    else:
                        import re
                        child = re.search(r"CHILD_REQUEST_\d+", json.dumps(body["input"])).group()
                        with lock:
                            finished.add(child)
                            if len(finished) == 4:
                                probes_done.set()
                        items = [{"type": "message", "id": "done", "role": "assistant", "status": "completed",
                                  "content": [{"type": "output_text", "text": "Child verified.", "annotations": []}]}]
            events = [{"type": "response.created", "response": {"id": "response", "status": "in_progress"}}]
            for index, item in enumerate(items):
                events.extend([{"type": "response.output_item.added", "output_index": index, "item": item},
                               {"type": "response.output_item.done", "output_index": index, "item": item}])
            events.append({"type": "response.completed", "response": {"id": "response", "status": "completed", "output": items,
                           "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}})
            data = "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
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
    home = tmp_path / "profile"
    home.mkdir()
    ctx = PluginContext(PluginManifest(name="child-contract", source="user"), PluginManager(scope_key=hermes_home_key(home)))
    registration = ctx.register_auxiliary_task("child_contract", display_name="Child contract", description="Local fixture")
    handle = ctx.workspaces.acquire("children", intent=ctx.workspaces.new_intent())
    workspace = Path(ctx.workspaces.inspect(handle)["path"])
    (workspace / ".codex").mkdir()
    (workspace / ".codex" / "config.toml").write_text('[permissions.plugin-workspace.filesystem]\n' + json.dumps(str(forbidden)) + '="read"\n')
    done = asyncio.Event()

    def event(note):
        notes.append(note)
        if note["method"] == "turn/completed" and not note.get("params", {}).get("sessionRootThreadId"):
            done.set()

    session = await ctx.llm.aopen_session(task="child_contract", key="children", workspace_handle=handle,
                                         instructions="Execute only the local fixture responses.", tools=[], child_limit=4, on_event=event)
    from agent.plugin_session_recovery import session_root
    private_secret = session_root(ctx, session.reference["binding"]) / "provider-secret"
    private_secret.write_text("fixture-only-provider-secret")
    try:
        await session.start_turn("ROOT_REQUEST_ONLY")
        await asyncio.wait_for(done.wait(), 30)
        reference = session.reference
        assert exposed == [True], json.dumps([{"type": t.get("type"), "name": t.get("name"), "nested": [x.get("name") for x in t.get("tools", [])]} for t in calls[0].get("tools", [])])
        assert 1 <= len(reference["childThreadIds"]) <= 4, json.dumps([n for n in notes if "collab" in n.get("params", {}).get("item", {}).get("type", "").lower()])
        await session.close()
        # Discovery must not rely on the plugin having saved child notifications.
        stale = {**reference, "childThreadIds": []}
        cold = await ctx.llm.ainspect_session(task="child_contract", reference=stale)
        assert cold["cleanup"]["quiescent"]
        assert 1 <= len(cold["children"]) <= 4
        assert all(child["parentThreadId"] == reference["threadId"] for child in cold["children"])
        assert len(cold["rawHistory"]) == 1 + len(cold["children"])
        assert len(finished) == 4
        for child in cold["children"]:
            assert Path(child["cwd"]) == workspace
            commands = [item for turn in child["turns"] for item in turn.get("items", []) if item.get("type") == "commandExecution"]
            assert commands and commands[-1]["exitCode"] == 0, commands
            assert "CHILD_PROBE_OK" in commands[-1].get("aggregatedOutput", "")
        assert not (workspace / "child-write").exists()
    finally:
        root_resumed.set()
        probes_done.set()
        await session.close()
        registration.dispose()
        ctx.workspaces.release(handle)
        server.shutdown()
        server.server_close()
