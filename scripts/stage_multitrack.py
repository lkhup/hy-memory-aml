"""Start isolated loopback backends. Does not touch the existing text process."""

import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values


def main():
    root = Path(__file__).resolve().parents[1]
    original = Path.home() / "hy-memory-aml"
    values = {k: v for k, v in dotenv_values(original / ".env").items() if v is not None}
    # Credentials are kept on the server, never copied to the development PC.
    allowed = {
        k: v
        for k, v in values.items()
        if k.startswith(("MEMORY_LLM_", "MEMORY_EMBEDDER_", "MEMORY_EMBEDDING_"))
    }
    (root / "logs").mkdir(exist_ok=True, mode=0o700)
    os.chmod(root / "logs", 0o700)
    (root / "data").mkdir(exist_ok=True, mode=0o700)
    os.chmod(root / "data", 0o700)
    runtime = root / "data" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime, 0o700)
    token_path = runtime / "service-token"
    if not token_path.exists():
        token_path.touch(mode=0o600)
        token_path.write_text(secrets.token_urlsafe(36))
    token = token_path.read_text().strip()
    py = str(original / ".venv-deploy/bin/python")
    report = {}
    for track, port in (("coding", 18788), ("multimodal", 18789)):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
        env = dict(os.environ)
        env.update(allowed)
        env.update(
            {
                "API_TOKEN": token,
                "AML_TRACK": track,
                "TRACK_ROOT": str(root / "data" / track),
                "SHARED_BUDGET_PATH": str(root / "data/shared/provider-budget.sqlite3"),
                "MAX_ADD_CONCURRENCY": "64",
                "MAX_SEARCH_CONCURRENCY": "64",
                "PROVIDER_MAX_CONCURRENCY": "48",
                "PROVIDER_INITIAL_CONCURRENCY": "32",
                "EMBEDDING_MAX_CONCURRENCY": "64",
                "VISION_ENABLED": "true",
                "PYTHONPATH": str(root),
                "MEMORY_MODE": "pro",
                "HY_MEMORY_READER": "hybrid",
                "MEMORY_LOCAL_RERANK_ENABLED": "false",
            }
        )
        with (root / "logs" / (track + ".log")).open("ab") as log:
            child = subprocess.Popen(
                [
                    py,
                    "-m",
                    "uvicorn",
                    "multitrack.api:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--workers",
                    "1",
                    "--log-level",
                    "warning",
                ],
                env=env,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        (runtime / (track + ".pid")).write_text(str(child.pid))
        for _ in range(90):
            if child.poll() is not None:
                raise RuntimeError(track + " backend exited; inspect local private logs")
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
                if response.status_code == 200:
                    report[track] = {"pid": child.pid, "health": response.json()}
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError(track + " startup timeout")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
