"""Independent cleanup survives owner death and retains proof on uncertainty."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

import psutil
import pytest

from agent.transports import codex_app_server_supervisor as supervisor

# Crash tests deliberately reparent their own children before final cleanup.
pytestmark = [pytest.mark.linux_only, pytest.mark.live_system_guard_bypass]


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError("Supervision condition did not become true")


def configuration(path):
    owner = psutil.Process()
    return {"receipt": str(path), "nonce": uuid.uuid4().hex, "binding": "test-binding",
            "owner": {"pid": owner.pid, "created": owner.create_time()}}


def script_command(tmp_path):
    detached = "import os,pathlib,time;pathlib.Path(" + repr(str(tmp_path / "detached-pid")) + ").write_text(str(os.getpid()));time.sleep(120)"
    script = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c'," + repr(detached) + "],start_new_session=True);time.sleep(120)"
    return [sys.executable, "-I", "-c", script]


def exited(process):
    return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE


def kill_tracked(processes):
    for process in reversed(processes):
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass


def test_stdin_eof_reaps_detached_descendants_and_persists_proof(tmp_path):
    config = configuration(tmp_path / "receipt.json")
    process = subprocess.Popen([sys.executable, "-I", supervisor.__file__, json.dumps(config), *script_command(tmp_path)],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    tracked = []
    try:
        wait_for(lambda: (tmp_path / "detached-pid").exists())
        child = psutil.Process(int((tmp_path / "detached-pid").read_text()))
        tracked = psutil.Process(process.pid).children(recursive=True)
        process.stdin.close()
        process.wait(timeout=15)
        assert process.returncode == 0, process.stderr.read().decode()
        assert exited(child)
        receipt = supervisor.proof(config["receipt"], nonce=config["nonce"], binding=config["binding"])
        assert receipt["quiescent"]
        assert receipt["reason"] == "descendants_reaped"
        assert supervisor.proof(config["receipt"], nonce="different", binding=config["binding"])["quiescent"] is False
    finally:
        kill_tracked(tracked)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()


def test_supervisor_survives_owner_crash_and_cleans_orphans(tmp_path):
    config = configuration(tmp_path / "receipt.json")
    owner_code = (
        "import json,os,pathlib,psutil,subprocess,sys,time;"
        "cfg=json.loads(sys.argv[1]);cfg['owner']={'pid':os.getpid(),'created':psutil.Process().create_time()};"
        "p=subprocess.Popen([sys.executable,'-I',sys.argv[2],json.dumps(cfg),*json.loads(sys.argv[3])],"
        "stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "pathlib.Path(sys.argv[4]).write_text(str(p.pid));time.sleep(120)"
    )
    owner = subprocess.Popen([sys.executable, "-I", "-c", owner_code, json.dumps(config), supervisor.__file__,
                              json.dumps(script_command(tmp_path)), str(tmp_path / "supervisor-pid")])
    tracked = []
    try:
        wait_for(lambda: (tmp_path / "detached-pid").exists())
        root = psutil.Process(int((tmp_path / "supervisor-pid").read_text()))
        child = psutil.Process(int((tmp_path / "detached-pid").read_text()))
        tracked = [root, *root.children(recursive=True)]
        owner.kill()
        owner.wait(timeout=5)
        wait_for(lambda: supervisor.proof(config["receipt"], nonce=config["nonce"], binding=config["binding"])["quiescent"])
        assert exited(child)
        assert exited(root)
    finally:
        kill_tracked(tracked)
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


def test_killed_supervisor_does_not_claim_quiescence_from_dead_root(tmp_path):
    config = configuration(tmp_path / "receipt.json")
    process = subprocess.Popen([sys.executable, "-I", supervisor.__file__, json.dumps(config), *script_command(tmp_path)],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tracked = []
    try:
        wait_for(lambda: (tmp_path / "detached-pid").exists())
        tracked = psutil.Process(process.pid).children(recursive=True)
        process.kill()
        process.wait(timeout=5)
        assert supervisor.proof(config["receipt"], nonce=config["nonce"], binding=config["binding"])["quiescent"] is False
    finally:
        process.stdin.close()
        kill_tracked(tracked)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_missing_or_malformed_receipt_is_not_cleanup_proof(tmp_path):
    path = tmp_path / "receipt.json"
    assert supervisor.proof(path, nonce="n", binding="b")["quiescent"] is False
    supervisor.write_receipt(path, {"version": 1, "nonce": "n", "binding": "b", "state": "quiescent"})
    assert supervisor.proof(path, nonce="n", binding="b")["quiescent"] is False
