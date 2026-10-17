"""Image reads must obey the same filesystem scope as native commands."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import struct
import threading
import zlib

import pytest

from agent.transports.codex_app_server import check_codex_binary
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_constants import hermes_home_key

pytestmark = [pytest.mark.linux_only, pytest.mark.live_system_guard_bypass]


def pixel():
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\xff")) + chunk(b"IEND", b"")


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["read", "write"])
@pytest.mark.parametrize("inside", [False, True])
async def test_native_image_reads_stay_inside_the_lease(tmp_path, monkeypatch, permission, inside):
    if check_codex_binary("codex", (0, 161, 0)) != (True, "0.161.0"):
        pytest.skip("Pinned native binary is required for image-scope acceptance")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            item = ({"type": "function_call", "id": "view", "call_id": "view", "name": "view_image", "arguments": json.dumps({"path": str(image)})}
                    if len(calls) == 1 else {"type": "message", "id": "done", "role": "assistant", "status": "completed",
                     "content": [{"type": "output_text", "text": "Done.", "annotations": []}]})
            events = [
                {"type": "response.created", "response": {"id": "r", "status": "in_progress"}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": "r", "status": "completed", "output": [item],
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
    ctx = PluginContext(PluginManifest(name="image-contract", source="user"), PluginManager(scope_key=hermes_home_key(home)))
    registration = ctx.register_auxiliary_task("image_contract", display_name="Image contract", description="Local fixture")
    handle = ctx.workspaces.acquire("image", intent=ctx.workspaces.new_intent())
    workspace = Path(ctx.workspaces.inspect(handle)["path"])
    image = workspace / "image.png" if inside else tmp_path / "private.png"
    image.write_bytes(pixel())
    done = asyncio.Event()
    session = await ctx.llm.aopen_session(task="image_contract", key="image", workspace_handle=handle, instructions="Use the fixture responses.",
                                         tools=[], execution_permission=permission, on_event=lambda n: done.set() if n["method"] == "turn/completed" else None)
    try:
        await session.start_turn("Read the fixture image.")
        await asyncio.wait_for(done.wait(), 15)
        assert len(calls) == 2
        contents = json.dumps(calls[1]["input"])
        assert ('"type": "input_image"' in contents) is inside, contents
    finally:
        await session.close()
        registration.dispose()
        ctx.workspaces.release(handle)
        server.shutdown()
        server.server_close()
