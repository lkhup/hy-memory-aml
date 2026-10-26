"""User-owned crash recovery for the gateway and isolated backend processes."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
LEGACY = Path.home() / "hy-memory-aml"
RUNTIME = ROOT / "data/runtime"


def matches(pid, module, port, cwd):
    proc = Path(f"/proc/{pid}")
    try:
        return (
            proc.joinpath("stat").read_text().split()[2] != "Z"
            and proc.joinpath("cwd").resolve() == cwd
            and module.encode() in proc.joinpath("cmdline").read_bytes().split(b"\0")
            and str(port).encode() in proc.joinpath("cmdline").read_bytes().split(b"\0")
        )
    except (FileNotFoundError, ProcessLookupError):
        return False


def environment(name):
    if name == "legacy-text":
        return json.loads((RUNTIME / "legacy-process-env.json").read_text())
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    if name == "gateway":
        return env
    values = dotenv_values(LEGACY / ".env")
    env.update(
        {
            k: v
            for k, v in values.items()
            if v is not None
            and k.startswith(("MEMORY_LLM_", "MEMORY_EMBEDDER_", "MEMORY_EMBEDDING_"))
        }
    )
    env.update(
        {
            "API_TOKEN": (RUNTIME / "service-token").read_text().strip(),
            "AML_TRACK": name,
            "TRACK_ROOT": str(ROOT / "data" / name),
            "SHARED_BUDGET_PATH": str(ROOT / "data/shared/provider-budget.sqlite3"),
            "MAX_ADD_CONCURRENCY": "64",
            "MAX_SEARCH_CONCURRENCY": "64",
            "PROVIDER_MAX_CONCURRENCY": "48",
            "PROVIDER_INITIAL_CONCURRENCY": "32",
            "EMBEDDING_MAX_CONCURRENCY": "64",
            "VISION_ENABLED": "true",
            "MEMORY_MODE": "pro",
            "HY_MEMORY_READER": "hybrid",
            "MEMORY_LOCAL_RERANK_ENABLED": "false",
        }
    )
    return env


def main():
    # flock prevents multiple supervisors from racing to restart a process.
    import fcntl

    lock = (RUNTIME / "supervisor.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    (RUNTIME / "supervisor.pid").write_text(str(os.getpid()))
    running = True

    def stop(*args):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    services = {
        "coding": ("multitrack.api:app", 18788, ROOT, "127.0.0.1"),
        "multimodal": ("multitrack.api:app", 18789, ROOT, "127.0.0.1"),
        "legacy-text": ("app.main:app", 18790, LEGACY, "127.0.0.1"),
        "gateway": ("multitrack.gateway:app", 18793, ROOT, "0.0.0.0"),
    }
    children = {}
    last_start = {}
    while running:
        for name, (module, port, cwd, host) in services.items():
            if name in children:
                children[name].poll()  # Reap exited children before checking /proc.
            path = RUNTIME / (name + ".pid")
            pid = int(path.read_text()) if path.exists() else 0
            if matches(pid, module, port, cwd):
                continue
            # Do not terminate an unrelated process holding a configured port.
            if time.monotonic() - last_start.get(name, 0) < 30:
                continue
            last_start[name] = time.monotonic()
            with (ROOT / "logs" / (name + ".log")).open("ab") as log:
                child = subprocess.Popen(
                    [
                        str(LEGACY / ".venv-deploy/bin/python"),
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
                    env=environment(name),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            children[name] = child
            path.write_text(str(child.pid))
            if name == "legacy-text":
                (LEGACY / "app.pid").write_text(str(child.pid))
            print(json.dumps({"restarted": name, "pid": child.pid}), flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
