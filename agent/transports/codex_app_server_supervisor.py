"""Independent native-process supervisor and durable descendant-cleanup proof.

Run as a small Linux subreaper around a plugin's app-server. It survives the
owner's death long enough to clean up adopted descendants, including children
which create a new process group. Killing this supervisor without a completed
receipt deliberately leaves execution unproven.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

import psutil


def supported():
    return sys.platform == "linux"


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def write_receipt(path, value):
    path = Path(path)
    staged = path.with_name(path.name + "." + uuid.uuid4().hex)
    fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staged.unlink(missing_ok=True)


def read_receipt(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Native supervision receipt exceeds its size bound")
    return json.loads(raw)


def proof(path, *, nonce, binding):
    try:
        value = read_receipt(path)
    except (OSError, ValueError):
        return {"quiescent": False, "reason": "cleanup_receipt_missing"}
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("nonce") != nonce or value.get("binding") != binding:
        return {"quiescent": False, "reason": "cleanup_receipt_mismatch"}
    root = value.get("supervisor")
    if (not isinstance(root, dict) or type(root.get("pid")) is not int or root["pid"] < 1
            or type(root.get("created")) not in {float, int} or root["created"] <= 0
            or not isinstance(value.get("bootId"), str) or not value["bootId"]
            or value.get("state") not in {"starting", "active", "quiescent", "unproven"}):
        return {"quiescent": False, "reason": "cleanup_receipt_mismatch"}
    try:
        uuid.UUID(value["bootId"])
    except ValueError:
        return {"quiescent": False, "reason": "cleanup_receipt_mismatch"}
    if value["bootId"] != boot_id():
        return {"quiescent": True, "reason": "host_reboot", "receipt": value}
    if value["state"] != "quiescent":
        return {"quiescent": False, "reason": "cleanup_unproven", "receipt": value}
    try:
        process = psutil.Process(root["pid"])
        if process.create_time() == root["created"] and process.status() != psutil.STATUS_ZOMBIE:
            return {"quiescent": False, "reason": "supervisor_finishing"}
    except psutil.NoSuchProcess:
        pass  # The completed proof remains valid after the supervisor exits.
    except psutil.Error:
        return {"quiescent": False, "reason": "supervisor_inspection_failed"}
    return {"quiescent": True, "reason": "descendants_reaped", "receipt": value}


def _subreaper():
    if not supported():
        raise RuntimeError("Native process supervision requires Linux subreaper support")
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "Cannot enable native process supervision")


def _reap():
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _cleanup(root, child, *, timeout=6):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        child.poll()
        _reap()
        descendants = root.children(recursive=True)
        if not descendants:
            return True
        force = time.monotonic() - started >= 1
        for process in reversed(descendants):
            try:
                process.kill() if force else process.terminate()
            except psutil.NoSuchProcess:
                continue
        time.sleep(0.025)
    return False


def run(configuration, command):
    _subreaper()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    root = psutil.Process()
    receipt = {"version": 1, "nonce": configuration["nonce"], "binding": configuration["binding"],
               "bootId": boot_id(), "state": "starting", "owner": configuration["owner"],
               "supervisor": {"pid": root.pid, "created": root.create_time()}, "startedAt": time.time()}
    receipt.update({k: configuration[k] for k in ("dispatchId", "workspace", "generation") if k in configuration})
    write_receipt(configuration["receipt"], receipt)
    child = None

    def relay():
        try:
            while not stop.is_set():
                block = os.read(sys.stdin.fileno(), 65536)
                if not block:
                    break
                view = memoryview(block)
                while view:
                    written = os.write(child.stdin.fileno(), view)
                    view = view[written:]
        except (BrokenPipeError, OSError, ValueError):
            pass  # EOF/death enters the same descendant-cleanup path.
        finally:
            stop.set()
            child.stdin.close()

    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=sys.stdout.buffer,
                                 stderr=sys.stderr.buffer, bufsize=0)
        receipt.update(state="active", child={"pid": child.pid})
        write_receipt(configuration["receipt"], receipt)
        threading.Thread(target=relay, daemon=True, name="native-stdin-relay").start()
        while not stop.wait(0.025) and child.poll() is None:
            pass
    finally:
        clean = _cleanup(root, child) if child is not None else not root.children(recursive=True)
        receipt.update(state="quiescent" if clean else "unproven", finishedAt=time.time())
        write_receipt(configuration["receipt"], receipt)
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(run(json.loads(sys.argv[1]), sys.argv[2:]))
