from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.auth import require_api_token
from app.config import Settings, get_settings
from app.memory import MemoryClient, MemoryService
from app.schemas import (
    AddRequest,
    AddResponse,
    HealthResponse,
    SearchRequest,
    SearchResponse,
)
from app.store import AdapterStore

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    memory_client: MemoryClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store = AdapterStore(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        service = MemoryService(settings=settings, store=store, client=memory_client)
        if service.fast_memory is not None and settings.fast_memory_backfill_hours > 0:
            try:
                documents = store.recent_raw_documents(settings.fast_memory_backfill_hours)
                backfill = getattr(service.fast_memory, "backfill", None)
                if callable(backfill):
                    added = backfill(documents)
                    logger.info(
                        "Fast memory backfill complete: candidates=%d added=%d",
                        len(documents),
                        added,
                    )
            except Exception:
                logger.exception("Fast memory backfill failed; continuing without it")
        app.state.memory_service = service
        try:
            yield
        finally:
            service.close()

    docs_url = "/docs" if settings.docs_enabled and not settings.is_production else None
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_url else None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error for %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal memory service error"},
        )

    def get_service(request: Request) -> MemoryService:
        return request.app.state.memory_service

    @app.get("/health", response_model=HealthResponse)
    def health(service: MemoryService = Depends(get_service)) -> HealthResponse:
        return HealthResponse(
            status="ok" if service.ready else "degraded",
            version=settings.app_version,
            memory_ready=service.ready,
        )

    @app.post(
        "/add",
        response_model=AddResponse,
        dependencies=[Depends(require_api_token)],
    )
    def add(
        payload: AddRequest,
        service: MemoryService = Depends(get_service),
    ) -> AddResponse:
        reservation = store.reserve(payload)
        if reservation.state == "conflict":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="request_id was already used with a different payload",
            )
        if reservation.state == "busy":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The same logical write is already being processed",
            )
        if reservation.state == "cached":
            return AddResponse(
                request_id=payload.request_id,
                user_id=payload.user_id,
                session_id=payload.session_id,
            )
        try:
            service.add(payload)
            store.complete(payload)
        except Exception:
            store.fail(payload.request_id)
            raise
        return AddResponse(
            request_id=payload.request_id,
            user_id=payload.user_id,
            session_id=payload.session_id,
        )

    @app.post(
        "/search",
        response_model=SearchResponse,
        dependencies=[Depends(require_api_token)],
    )
    def search(
        payload: SearchRequest,
        service: MemoryService = Depends(get_service),
    ) -> SearchResponse:
        return SearchResponse(data=service.search(payload))

    return app


app = create_app()

