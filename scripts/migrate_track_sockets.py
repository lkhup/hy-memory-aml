"""Designated-host, idle-service TCP-to-UDS migration with source/process rollback.

The deployment operator supplies a private directory containing the prior sources
and files.json (the list of overwritten relative paths). Never logs environment
values or benchmark payloads. Does not restart the frozen legacy text backend.
"""

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx

from scripts.supervise_tracks import LEGACY, ROOT, RUNTIME, environment, matches, services
from scripts.switch_public_gateway import drain, live


def get_metrics(track):
    token = (RUNTIME / "service-token").read_text().strip()
    response = httpx.get(
        f"http://127.0.0.1:18793/{track}/metrics",
        headers={"Authorization": "Bearer " + token},
        timeout=10,
        trust_env=False,
    )
    response.raise_for_status()
    return response.json()


def journal(track):
    path = ROOT / "data" / track / "evidence.sqlite3"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        states = dict(db.execute("SELECT state,count(*) FROM requests GROUP BY state"))
        return {
            "states": states,
            "documents": db.execute("SELECT count(*) FROM documents").fetchone()[0],
            "chunks": db.execute("SELECT count(*) FROM chunks").fetchone()[0],
        }


def snapshot(name):
    pid = int((RUNTIME / (name + ".pid")).read_text())
    proc = Path(f"/proc/{pid}")
    argv = [p.decode() for p in proc.joinpath("cmdline").read_bytes().split(b"\0") if p]
    cwd = proc.joinpath("cwd").resolve()
    if cwd != ROOT or not live(pid):
        raise RuntimeError("Unexpected process owner or cwd: " + name)
    module, binding = {
        "coding": ("multitrack.api:app", "18788"),
        "multimodal": ("multitrack.api:app", "18789"),
        "gateway": ("multitrack.gateway:app", "18793"),
        "supervisor": ("scripts/supervise_tracks.py", "scripts/supervise_tracks.py"),
    }[name]
    if module not in argv or binding not in argv:
        raise RuntimeError("Unexpected process identity: " + name)
    env = dict(
        p.decode().split("=", 1)
        for p in proc.joinpath("environ").read_bytes().split(b"\0")
        if b"=" in p
    )
    return {"pid": pid, "argv": argv, "cwd": cwd, "env": env}


def spawn(name, argv, env, cwd=ROOT):
    with (ROOT / "logs" / (name + ".log")).open("ab") as log:
        child = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    (RUNTIME / (name + ".pid")).write_text(str(child.pid))
    return child


def ready():
    with httpx.Client(timeout=2, trust_env=False) as client:
        for track in ["", "coding/", "multimodal/"]:
            response = client.get("http://127.0.0.1:18793/" + track + "health")
            if response.status_code != 200 or not response.json().get("memory_ready"):
                return False
    return True


def wait_ready(children):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if any(child.poll() is not None for child in children):
            raise RuntimeError("A replacement process exited")
        try:
            if ready():
                return
        except (ValueError, httpx.TransportError):
            pass
        time.sleep(0.2)
    raise RuntimeError("Replacement readiness timed out")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    backup = args.backup.resolve()
    if not backup.is_relative_to(RUNTIME.resolve()):
        raise RuntimeError("Backup must be inside the private runtime directory")
    files = json.loads((backup / "files.json").read_text())
    for name in files:
        if not (ROOT / name).resolve().is_relative_to(ROOT) or not (backup / name).is_file():
            raise RuntimeError("Invalid source backup")
    os.umask(0o077)
    prior = {name: snapshot(name) for name in ["coding", "multimodal", "gateway", "supervisor"]}
    before = {track: journal(track) for track in ["coding", "multimodal"]}
    for track in before:
        metrics = get_metrics(track)
        if any(metrics[op][key] for op in ["add", "search"] for key in ["active", "queued"]):
            raise RuntimeError("Track has active requests; postpone migration")
        if before[track]["states"].get("processing", 0):
            raise RuntimeError("Track has unfinished journal entries")
    text_pid = int((RUNTIME / "legacy-text.pid").read_text())
    if not matches(text_pid, "app.main:app", 18790, LEGACY):
        raise RuntimeError("Legacy text identity mismatch")
    legacy_files = [LEGACY / ".env", *sorted((LEGACY / "app").glob("*.py"))]
    legacy_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in legacy_files}
    print(json.dumps({"preflight": "idle", "journals": before}), flush=True)
    if not args.apply:
        return
    children = {}
    stopped = set()
    public_started = None
    report = {"before": before, "legacy_text_pid": text_pid}
    try:
        # Stop crash recovery first; drain the ingress before touching the writers.
        for name in ["supervisor", "gateway", "coding", "multimodal"]:
            if name == "gateway":
                public_started = time.monotonic()
            drain(prior[name]["pid"], timeout=1860)
            stopped.add(name)
        python = str(LEGACY / ".venv-deploy/bin/python")
        for track in ["coding", "multimodal"]:
            binding = services()[track][1]
            children[track] = spawn(
                track,
                [
                    python,
                    "-m",
                    "uvicorn",
                    "multitrack.api:app",
                    "--uds",
                    binding,
                    "--workers",
                    "1",
                    "--log-level",
                    "warning",
                ],
                environment(track),
            )
        children["gateway"] = spawn("gateway", prior["gateway"]["argv"], environment("gateway"))
        wait_ready(list(children.values()))
        report["public_switch_seconds"] = round(time.monotonic() - public_started, 3)
        after = {track: journal(track) for track in before}
        if before != after:
            raise RuntimeError("Track journal changed unexpectedly during the idle migration")
        if any(
            hashlib.sha256(p.read_bytes()).hexdigest() != legacy_hashes[str(p)]
            for p in legacy_files
        ) or not matches(text_pid, "app.main:app", 18790, LEGACY):
            raise RuntimeError("Frozen legacy process or configuration changed")
        for port in [18788, 18789]:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("0.0.0.0", port))
        children["supervisor"] = spawn(
            "supervisor", prior["supervisor"]["argv"], environment("gateway")
        )
        time.sleep(0.5)
        if children["supervisor"].poll() is not None:
            raise RuntimeError("Replacement crash recovery failed to start")
        report.update(
            {
                "success": True,
                "ports_released": [18788, 18789],
                "after": after,
                "pids": {name: child.pid for name, child in children.items()},
                "legacy_unchanged": True,
            }
        )
    except BaseException as exc:
        # Roll back source first, then restore the exact previous commands/envs.
        for name in ["supervisor", "gateway", "coding", "multimodal"]:
            child = children.get(name)
            if child and child.poll() is None:
                drain(child.pid, timeout=1860)
                child.wait(timeout=5)
        for name in files:
            (ROOT / name).write_bytes((backup / name).read_bytes())
        restored = []
        for name in ["coding", "multimodal", "gateway", "supervisor"]:
            if name in stopped:
                old = prior[name]
                restored.append(spawn(name, old["argv"], old["env"], old["cwd"]))
        wait_ready(restored)
        report.update(
            {"success": False, "rollback_healthy": True, "error_type": type(exc).__name__}
        )
        print(json.dumps(report), flush=True)
        (RUNTIME / "socket-migration-report.json").write_text(json.dumps(report, indent=2))
        raise
    (RUNTIME / "socket-migration-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
