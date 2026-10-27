"""Checked one-time migration to one public port, retaining the legacy text code.

Run only after both new backends pass real smoke and load checks. This script
does not alter legacy sources, .env, or databases. It gracefully moves the old
text process to loopback and starts the gateway, with automatic rollback.
"""

import argparse
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

from multitrack.endpoints import backend_socket

LEGACY = Path.home() / "hy-memory-aml"
ROOT = Path(__file__).resolve().parents[1]
PYTHON = LEGACY / ".venv-deploy/bin/python"
RUNTIME = ROOT / "data/runtime"


def health(port, wait=90):
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if response.status_code == 200 and response.json().get("memory_ready"):
                return True
        except (ValueError, httpx.TransportError):
            pass
        time.sleep(0.25)
    return False


def live(pid):
    path = Path(f"/proc/{pid}/stat")
    try:
        return path.read_text().split()[2] != "Z"
    except FileNotFoundError:
        return False


def track_health(track):
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(backend_socket(track)))) as client:
            response = client.get("http://backend/health", timeout=3)
            return response.status_code == 200 and response.json().get("memory_ready")
    except (ValueError, httpx.TransportError):
        return False


def drain(pid, timeout=180):
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while live(pid) and time.monotonic() < deadline:
        time.sleep(0.25)
    if live(pid):
        raise RuntimeError("Process did not drain; no forced termination")


def start(module, cwd, host, port, env, name):
    with (ROOT / "logs" / (name + ".log")).open("ab") as log:
        child = subprocess.Popen(
            [
                str(PYTHON),
                "-m",
                "uvicorn",
                module,
                "--host",
                host,
                "--port",
                str(port),
                "--workers",
                "1",
            ],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    (RUNTIME / (name + ".pid")).write_text(str(child.pid))
    return child


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    pid = int((LEGACY / "app.pid").read_text())
    proc = Path(f"/proc/{pid}")
    cmd = (proc / "cmdline").read_bytes().split(b"\0")
    if proc.joinpath("cwd").resolve() != LEGACY or b"18793" not in cmd:
        raise RuntimeError("Legacy PID/cwd/port mismatch; refusing migration")
    if not health(18793, 3) or not all(track_health(t) for t in ("coding", "multimodal")):
        raise RuntimeError("A required backend is unhealthy")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 18790))
    # Confirm there is no write to interrupt or journal to reset on restart.
    values = dotenv_values(LEGACY / ".env")
    dbpath = Path(values.get("ADAPTER_DB_PATH") or values.get("DATA_DIR", "data/adapter"))
    if dbpath.suffix != ".sqlite3":
        dbpath /= "adapter.sqlite3"
    if not dbpath.is_absolute():
        dbpath = LEGACY / dbpath
    db = sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True)
    pending = db.execute("SELECT count(*) FROM add_requests WHERE state='processing'").fetchone()[0]
    db.close()
    if pending:
        raise RuntimeError("Legacy writes are active; retry migration after they finish")
    files = sorted((LEGACY / "app").glob("*.py"))
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    report = {
        "legacy_pid": pid,
        "pending_writes": pending,
        "legacy_source_hashes": hashes,
        "public_port": 18793,
        "text_backend": "127.0.0.1:18790",
        "ready": True,
    }
    print(json.dumps(report), flush=True)
    if not args.apply:
        return
    env = dict(
        item.decode().split("=", 1)
        for item in (proc / "environ").read_bytes().split(b"\0")
        if b"=" in item
    )
    # Retain a private rollback snapshot; never print its contents.
    recovery = RUNTIME / "legacy-process-env.json"
    recovery.touch(mode=0o600, exist_ok=False)
    os.chmod(recovery, 0o600)
    recovery.write_text(json.dumps(env))
    moved = gateway = None
    old_stopped = False
    started = time.monotonic()
    try:
        drain(pid)
        old_stopped = True
        moved = start("app.main:app", LEGACY, "127.0.0.1", 18790, env, "legacy-text")
        # Gateway can bind immediately; text becomes ready as the original SDK loads.
        gateway = start(
            "multitrack.gateway:app",
            ROOT,
            "0.0.0.0",
            18793,
            {**os.environ, "PYTHONPATH": str(ROOT)},
            "gateway",
        )
        if not health(18790) or not health(18793):
            raise RuntimeError("Text or gateway failed health verification")
        for track in ("coding", "multimodal"):
            response = httpx.get(f"http://127.0.0.1:18793/{track}/health", timeout=10)
            if response.status_code != 200:
                raise RuntimeError(track + " gateway verification failed")
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        if hashes != after:
            raise RuntimeError("Legacy source hashes changed")
        (LEGACY / "app.pid").write_text(str(moved.pid))
        report.update(
            {
                "legacy_new_pid": moved.pid,
                "gateway_pid": gateway.pid,
                "legacy_sources_unchanged": True,
                "success": True,
                "switch_seconds": round(time.monotonic() - started, 3),
            }
        )
        (RUNTIME / "deployment-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    except BaseException:
        for child in (gateway, moved):
            if child is not None and live(child.pid):
                drain(child.pid)
        if old_stopped:
            restored = start("app.main:app", LEGACY, "0.0.0.0", 18793, env, "legacy-rollback")
            (LEGACY / "app.pid").write_text(str(restored.pid))
            print(
                json.dumps({"rollback_pid": restored.pid, "health_ok": health(18793)}), flush=True
            )
        raise


if __name__ == "__main__":
    main()
