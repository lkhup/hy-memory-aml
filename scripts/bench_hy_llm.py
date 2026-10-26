"""Synthetic, isolated benchmark of the complete Hy-Memory Pro Add/Search path."""

import argparse
import concurrent.futures
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--count", type=int, default=16)
    parser.add_argument("--rounds", type=int, default=1)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    run = Path(tempfile.mkdtemp(prefix="hy-pro-bench-", dir=root))
    env = dict(os.environ)
    env.update({k: v for k, v in dotenv_values(args.env_file).items() if v is not None})
    env.update({
        "API_TOKEN": secrets.token_urlsafe(24),
        "FAST_MEMORY_ENABLED": "false",
        "FAST_MEMORY_READ_FALLBACK": "false",
        "FAST_MEMORY_BACKFILL_HOURS": "0",
        "DATA_DIR": str(run / "adapter"),
        "ADAPTER_DB_PATH": str(run / "adapter" / "adapter.sqlite3"),
        "MEMORY_DATA_DIR": str(run / "hy"),
        "MEMORY_PERSIST_DIR": str(run / "chroma"),
        "MEMORY_CACHE_DB_PATH": str(run / "hy" / "cache.db"),
        "MEMORY_HISTORY_DB_PATH": str(run / "hy" / "history.db"),
        "MEMORY_COLLECTION_NAME": "synthetic_bench",
        "MEMORY_MODE": "pro",
        "RAW_EVIDENCE_LIMIT": "0",
        "MAX_ADD_CONCURRENCY": str(args.concurrency),
        "MAX_SEARCH_CONCURRENCY": str(args.concurrency),
        "MEMORY_LLM_GLOBAL_CONCURRENCY": str(args.concurrency),
        "MEMORY_LOG_LLM_CONTENT": "false",
    })
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": "Bearer " + env["API_TOKEN"]}
    with (run / "app.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--workers", "1"],
            cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=base, headers=headers, timeout=240) as client:
                for _ in range(60):
                    if process.poll() is not None:
                        raise RuntimeError(f"Benchmark service stopped; inspect {run}/app.log")
                    try:
                        if client.get("/health", timeout=2).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError("Benchmark startup timeout")
                print(json.dumps({"started": str(run), "concurrency": args.concurrency}),
                      flush=True)

                def add(index):
                    started = time.perf_counter()
                    response = client.post("/add", json={
                        "request_id": f"synthetic-{index}",
                        "user_id": f"synthetic-user-{index}",
                        "session_id": f"synthetic-session-{index}",
                        "messages": [
                            {"role": "user", "timestamp": 1792368000000,
                             "content": (
                                 f"My name is Jordan {index}. I live in Singapore and work "
                                 "as a software engineer. My favorite color is blue. "
                                 "I enjoy hiking on weekends and cooking vegetarian food. "
                                 "I used to live in Tokyo but moved to Singapore in March 2025."
                             )},
                            {"role": "assistant", "timestamp": 1792368001000,
                             "content": "What would you like me to remember about your plans?"},
                            {"role": "user", "timestamp": 1792368002000,
                             "content": (
                                 "Please remember I am learning Spanish every Tuesday evening. "
                                 "My sister Maya is visiting in December. We plan to go hiking. "
                                 "I am allergic to peanuts, so avoid them when suggesting meals. "
                                 "For technical answers I prefer short explanations and examples."
                             )},
                        ],
                    })
                    result = {"index": index, "status": response.status_code,
                              "seconds": round(time.perf_counter() - started, 3)}
                    print(json.dumps(result), flush=True)
                    return result

                started = time.perf_counter()
                with concurrent.futures.ThreadPoolExecutor(args.concurrency) as pool:
                    results = list(pool.map(add, range(args.count)))
                elapsed = time.perf_counter() - started

                update_results = []
                if args.rounds > 1:
                    def update(index):
                        started = time.perf_counter()
                        response = client.post("/add", json={
                            "request_id": f"synthetic-update-{index}",
                            "user_id": f"synthetic-user-{index}",
                            "session_id": f"synthetic-update-session-{index}",
                            "messages": [{
                                "role": "user", "timestamp": 1792454400000,
                                "content": (
                                    "Update my favorite color: I now prefer green, not blue. "
                                    "Please remember the new preference. My sister Maya has "
                                    "confirmed her December visit and we booked a hiking trip."
                                ),
                            }],
                        })
                        result = {"update": index, "status": response.status_code,
                                  "seconds": round(time.perf_counter() - started, 3)}
                        print(json.dumps(result), flush=True)
                        return result

                    with concurrent.futures.ThreadPoolExecutor(args.concurrency) as pool:
                        update_results = list(pool.map(update, range(args.count)))

                def search(index):
                    response = client.post("/search", json={
                        "user_id": f"synthetic-user-{index}",
                        "query": "What is my favorite color?",
                        "top_k": 10,
                    })
                    return response.status_code == 200 and any(
                        ("green" if args.rounds > 1 else "blue") in item["content"].lower()
                        for item in response.json().get("data", [])
                    )

                with concurrent.futures.ThreadPoolExecutor(args.concurrency) as pool:
                    found = list(pool.map(search, range(args.count)))
                latencies = sorted(r["seconds"] for r in results)
                summary = {
                    "count": args.count, "concurrency": args.concurrency,
                    "add_ok": sum(r["status"] == 200 for r in results),
                    "wall_seconds": round(elapsed, 3),
                    "requests_per_second": round(args.count / elapsed, 3),
                    "p50_seconds": latencies[len(latencies) // 2],
                    "max_seconds": max(latencies),
                    "semantic_search_found": sum(found),
                    "raw_evidence_fallback": False,
                    "update_ok": sum(r["status"] == 200 for r in update_results),
                }
                (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
                print(json.dumps(summary), flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
