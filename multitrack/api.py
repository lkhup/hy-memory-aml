import functools
import logging
import os
import time
from contextlib import asynccontextmanager

import anyio
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.auth import require_api_token
from multitrack.admission import Admission
from multitrack.config import TrackSettings
from multitrack.engine import Engine
from multitrack.schemas import AddRequest, MemoryRecord, SearchRequest

logger = logging.getLogger(__name__)
MAX_BODY = 48 * 1024 * 1024


def create_app(settings=None, engine=None):
    @asynccontextmanager
    async def lifespan(app):
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
        config = settings or TrackSettings()
        load_dotenv(override=False)
        root = config.track_root.resolve()
        # Each backend owns a complete isolated SDK environment and one worker.
        os.environ.update(
            {
                "MEMORY_DATA_DIR": str(root / "hy"),
                "MEMORY_PERSIST_DIR": str(root / "hy-chroma"),
                "MEMORY_CACHE_DB_PATH": str(root / "hy" / "cache.db"),
                "MEMORY_HISTORY_DB_PATH": str(root / "hy" / "history.db"),
                "MEMORY_LOG_DIR": str(root / "hy" / "logs"),
                "MEMORY_COLLECTION_NAME": "aml_" + config.aml_track + "_v2",
                "MEMORY_VECTOR_STORE": "chroma",
                "MEMORY_LLM_GLOBAL_CONCURRENCY": str(config.provider_max_concurrency),
                "MEMORY_EMBEDDING_GLOBAL_CONCURRENCY": str(config.embedding_max_concurrency),
                "MEMORY_LLM_MAX_RETRIES": "1",  # Transport owns the bounded retry loop.
                "MEMORY_EMBEDDER_MAX_RETRIES": "0",
                "MEMORY_LOG_LLM_CONTENT": "false",
                "MEMORY_ENABLE_GRAPH": "false",
            }
        )
        app.state.settings = config
        app.state.engine = engine or Engine(config)
        app.state.add_gate = Admission(
            config.max_add_concurrency, config.max_pending_requests, config.queue_timeout
        )
        app.state.search_gate = Admission(
            config.max_search_concurrency, config.max_pending_requests, config.queue_timeout
        )
        app.state.workers = anyio.CapacityLimiter(
            config.max_add_concurrency + config.max_search_concurrency + 8
        )
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(app.state.engine.close)

    app = FastAPI(
        title="Hy-Memory Coding/Multimodal",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def work(fn, *args):
        # Cancellation does not abandon a still-running write in a background
        # thread. Its admission and user lock remain held through persistence.
        return await anyio.to_thread.run_sync(
            functools.partial(fn, *args), limiter=app.state.workers
        )

    async def read_payload(request, model):
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > MAX_BODY:
                raise HTTPException(413, "Payload exceeds supported 48 MiB transport limit")
            body.extend(part)
        try:
            payload = await work(model.model_validate_json, body)
        except ValidationError as exc:
            # FastAPI's default errors echo invalid input, including whole images.
            errors = [
                {"loc": e["loc"], "type": e["type"], "msg": e["msg"]}
                for e in exc.errors(include_input=False, include_context=False)
            ]
            raise RequestValidationError(errors) from exc
        if app.state.settings.aml_track == "coding":
            contents = (
                [m.content for m in payload.messages]
                if isinstance(payload, AddRequest)
                else [payload.query]
            )
            if any(not isinstance(c, str) for c in contents):
                raise HTTPException(422, "Coding content must be a string")
        elif isinstance(payload, SearchRequest) and isinstance(payload.query, str):
            # Accept text-only multimodal questions too.
            pass
        return payload

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": "0.2.0",
            "memory_ready": True,
            "track": app.state.settings.aml_track,
        }

    @app.get("/metrics", dependencies=[Depends(require_api_token)])
    async def metrics():
        return {
            "add": app.state.add_gate.snapshot(),
            "search": app.state.search_gate.snapshot(),
            "provider": await work(app.state.engine.budget.snapshot),
        }

    @app.post("/add", dependencies=[Depends(require_api_token)])
    async def add(request: Request):
        payload = await read_payload(request, AddRequest)
        started = time.monotonic()
        async with app.state.add_gate.enter(payload.user_id):
            store = app.state.engine.store
            state = await work(store.reserve, payload)
            if state in {"conflict", "processing"}:
                raise HTTPException(409, "request_id conflicts or is already processing")
            if state != "succeeded":
                try:
                    await work(app.state.engine.add, payload)
                    await work(store.state, payload.request_id, "succeeded")
                except BaseException:
                    await work(store.state, payload.request_id, "failed")
                    raise
        logger.info("request operation=add outcome=ok seconds=%.3f", time.monotonic() - started)
        return {
            "success": True,
            "request_id": payload.request_id,
            "user_id": payload.user_id,
            "session_id": payload.session_id,
        }

    @app.post("/search", dependencies=[Depends(require_api_token)])
    async def search(request: Request):
        payload = await read_payload(request, SearchRequest)
        async with app.state.search_gate.enter():
            records = await work(app.state.engine.search, payload)

        def encode_records():
            return JSONResponse(
                {
                    "data": [
                        MemoryRecord.model_validate(r).model_dump(exclude_none=True)
                        for r in records
                    ]
                }
            )

        return await work(encode_records)

    @app.exception_handler(Exception)
    async def unavailable(request, exc):
        # Avoid returning provider payloads, credentials or benchmark content.
        logger.error(
            "request operation=%s outcome=error type=%s", request.url.path, type(exc).__name__
        )
        return JSONResponse(
            {"detail": "Memory dependency unavailable; retry this request"},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    return app


app = create_app()
