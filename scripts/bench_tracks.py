"""Real API synthetic checks, with separate users and no benchmark data."""

import argparse
import base64
import concurrent.futures
import json
import time
import uuid
from pathlib import Path

import httpx


def main():
    args = argparse.ArgumentParser()
    args.add_argument("--count", type=int, default=32)
    args.add_argument("--concurrency", type=int, default=32)
    args.add_argument("--track", choices=["coding", "multimodal"], default="coding")
    opts = args.parse_args()
    root = Path(__file__).resolve().parents[1]
    token = (root / "data/runtime/service-token").read_text().strip()
    port = 18788 if opts.track == "coding" else 18789
    prefix = "synthetic-" + uuid.uuid4().hex
    headers = {"X-Api-Key": token}
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers=headers,
        timeout=1200,
        limits=httpx.Limits(max_connections=128),
    ) as client:

        def run(index):
            user = f"{prefix}-{index}"
            content = (
                f"Repository synthetic-{index}. Bug: parse_count('') raises ValueError. "
                "Fix in counter.py: return 0 if value == '' else int(value). "
                "Regression test: assert parse_count('') == 0. Keep numeric strings unchanged."
            )
            if opts.track == "multimodal":
                # Tiny valid image is generated locally; no participant assets are used.
                import io

                from PIL import Image

                image = Image.new("RGB", (64, 64), color="blue")
                stream = io.BytesIO()
                image.save(stream, format="PNG")
                content = [
                    {"type": "text", "text": "My blue square icon is project Orion."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(stream.getvalue()).decode()
                        },
                    },
                ]
            payload = {
                "request_id": user,
                "user_id": user,
                "session_id": user,
                "messages": [
                    {"role": "user", "content": content, "timestamp": int(time.time() * 1000)}
                ],
            }
            start = time.monotonic()
            response = client.post("/add", json=payload)
            return {
                "index": index,
                "user": user,
                "status": response.status_code,
                "seconds": round(time.monotonic() - start, 3),
            }

        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(opts.concurrency) as pool:
            results = list(pool.map(run, range(opts.count)))
        elapsed = time.monotonic() - started

        # Search separately to distinguish write throughput from retrieval latency.
        def search(result):
            response = client.post(
                "/search",
                json={
                    "user_id": result["user"],
                    "top_k": 100,
                    "query": "blue Orion icon"
                    if opts.track == "multimodal"
                    else "parse_count ValueError fix",
                },
            )
            records = response.json().get("data", []) if response.status_code == 200 else []
            found = any(
                (
                    isinstance(r["content"], list)
                    and any(p["type"] == "image_url" for p in r["content"])
                )
                if opts.track == "multimodal"
                else "parse_count" in r["content"]
                for r in records
            )
            return response.status_code == 200 and found

        with concurrent.futures.ThreadPoolExecutor(opts.concurrency) as pool:
            searches = list(pool.map(search, results))
        isolation = client.post(
            "/search",
            json={"user_id": prefix + "-other", "top_k": 100, "query": "parse_count Orion blue"},
        )
        durations = sorted(r["seconds"] for r in results)
        report = {
            "track": opts.track,
            "count": opts.count,
            "concurrency": opts.concurrency,
            "add_ok": sum(r["status"] == 200 for r in results),
            "search_found": sum(searches),
            "wall_seconds": round(elapsed, 3),
            "writes_per_second": round(opts.count / elapsed, 3),
            "p50_seconds": durations[len(durations) // 2],
            "p95_seconds": durations[min(len(durations) - 1, int(len(durations) * 0.95))],
            "isolation_ok": isolation.status_code == 200 and isolation.json() == {"data": []},
            "metrics": client.get("/metrics").json(),
            "results": results,
        }
        path = root / "data" / (prefix + ".json")
        path.write_text(json.dumps(report))
        print(json.dumps(report), flush=True)
        if report["add_ok"] != opts.count or report["search_found"] != opts.count:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
