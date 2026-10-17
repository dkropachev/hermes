"""Real native request_user_input round trip through the pinned server."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

from agent.transports.codex_app_server import check_codex_binary
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_constants import hermes_home_key

pytestmark = [pytest.mark.linux_only, pytest.mark.live_system_guard_bypass]


@pytest.mark.asyncio
async def test_native_user_input_persists_and_resumes_without_blocking_transport(tmp_path, monkeypatch):
    if check_codex_binary("codex", (0, 161, 0)) != (True, "0.161.0"):
        pytest.skip("Pinned native binary is required for question conformance")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            item = ({"type": "function_call", "id": "ask", "call_id": "ask", "name": "request_user_input", "arguments": json.dumps({
                "questions": [{"id": "scope", "header": "Scope", "question": "Which scope should be used?", "options": [
                    {"label": "Keep scope", "description": "Keep the approved scope"},
                    {"label": "Plan again", "description": "Prepare a new scope"}]}]})} if len(calls) == 1 else
                {"type": "message", "id": "done", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": "Answer received.", "annotations": []}]})
            events = [
                {"type": "response.created", "response": {"id": "response", "status": "in_progress"}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": "response", "status": "completed", "output": [item],
                 "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}},
            ]
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
    ctx = PluginContext(PluginManifest(name="question-contract", source="user"), PluginManager(scope_key=hermes_home_key(home)))
    registration = ctx.register_auxiliary_task("question_contract", display_name="Question contract", description="Local fixture")
    handle = ctx.workspaces.acquire("question", intent=ctx.workspaces.new_intent())
    pending = asyncio.get_running_loop().create_future()
    done = asyncio.Event()

    def event(note):
        if note["method"] == "session/question":
            pending.set_result(note)
            return None
        if note["method"] == "turn/completed":
            done.set()

    session = await ctx.llm.aopen_session(task="question_contract", key="question", workspace_handle=handle,
                                         instructions="Use the local fixture responses.", tools=[], on_event=event)
    try:
        await session.start_turn("Ask the fixture question.")
        question = await asyncio.wait_for(pending, 15)
        assert question["kind"] == "item/tool/requestUserInput"
        assert question["params"]["questions"][0]["id"] == "scope"
        assert not session._is_executing()
        assert (await session.inspect())["pendingRequestIds"] == [question["requestId"]]
        await session.respond(question["requestId"], {"answers": {"scope": {"answers": ["Keep scope"]}}})
        await asyncio.wait_for(done.wait(), 15)
        assert len(calls) == 2
        assert "Keep scope" in json.dumps(calls[-1]["input"])
    finally:
        await session.close()
        registration.dispose()
        ctx.workspaces.release(handle)
        server.shutdown()
        server.server_close()
