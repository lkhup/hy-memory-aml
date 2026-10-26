"""One public port; separate legacy text and new track backend processes."""

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

TARGETS = {
    "textual": os.getenv("TEXTUAL_BACKEND_URL", "http://127.0.0.1:18790"),
    "coding": os.getenv("CODING_BACKEND_URL", "http://127.0.0.1:18788"),
    "multimodal": os.getenv("MULTIMODAL_BACKEND_URL", "http://127.0.0.1:18789"),
}
HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def create_gateway(targets=None, transport=None):
    targets = targets or TARGETS

    @asynccontextmanager
    async def lifespan(app):
        app.state.client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=512, max_keepalive_connections=128),
            timeout=httpx.Timeout(1860, connect=5, pool=5),
            transport=transport,
            trust_env=False,
        )
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def proxy(path: str, request: Request):
        segments = path.split("/")
        track = segments[0] if segments[0] in targets else "textual"
        remainder = "/".join(segments[1:]) if segments[0] in targets else path
        if remainder not in {"add", "search", "health", "metrics"}:
            raise HTTPException(404, "Unknown endpoint")
        if (
            request.headers.get("content-length", "").isdigit()
            and int(request.headers["content-length"]) > 48 * 1024 * 1024
        ):
            raise HTTPException(413, "Payload exceeds supported 48 MiB transport limit")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        # Preserve bytes, auth, status and response headers. Never retry POST here:
        # only the backend journal can determine whether a write has committed.
        forwarded = app.state.client.build_request(
            request.method,
            targets[track] + "/" + remainder,
            headers=headers,
            content=request.stream(),
        )
        try:
            response = await app.state.client.send(forwarded, stream=True)
        except httpx.TimeoutException as exc:
            raise HTTPException(504, "Backend request timed out") from exc
        except httpx.TransportError as exc:
            raise HTTPException(
                503, "Backend temporarily unavailable", headers={"Retry-After": "5"}
            ) from exc
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items() if k.lower() not in HOP},
            background=BackgroundTask(response.aclose),
        )

    return app


app = create_gateway()
