from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.schemas import AddRequest, MemoryRecord, SearchRequest
from app.store import RawDocument

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)


def _record_id(request_id: str) -> str:
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return f"fast:{digest}"


def _created_at(request: AddRequest) -> str:
    timestamps = [message.timestamp for message in request.messages if message.timestamp]
    if not timestamps:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return (
        datetime.fromtimestamp(max(timestamps) / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class FastVectorMemory:
    """Embedding-only memory path for high-throughput benchmark ingestion."""

    def __init__(self, settings: Settings) -> None:
        if not settings.memory_embedder_api_key.strip():
            raise ValueError("MEMORY_EMBEDDER_API_KEY is required for fast memory")

        import chromadb
        from openai import OpenAI

        self._model = settings.memory_embedder_model
        self._dimensions = settings.memory_embedding_dims
        self._max_chars = settings.fast_memory_max_document_chars
        self._client = OpenAI(
            api_key=settings.memory_embedder_api_key,
            base_url=settings.memory_embedder_base_url,
            timeout=60,
            max_retries=2,
        )
        settings.fast_memory_persist_dir.mkdir(parents=True, exist_ok=True)
        self._chroma = chromadb.PersistentClient(
            path=str(settings.fast_memory_persist_dir)
        )
        self._collection = self._chroma.get_or_create_collection(
            name=settings.fast_memory_collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._collection_lock = threading.RLock()

    def _trim(self, text: str) -> str:
        if len(text) <= self._max_chars:
            return text
        half = (self._max_chars - 32) // 2
        return f"{text[:half]}\n[...content trimmed...]\n{text[-half:]}"

    def _embed(self, texts: list[str]) -> list[list[float]]:
        response = self._client.embeddings.create(
            model=self._model,
            input=texts,
            dimensions=self._dimensions,
        )
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]

    def _upsert(
        self,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict[str, str]],
    ) -> None:
        embeddings = self._embed(documents)
        with self._collection_lock:
            self._collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )

    def add(self, request: AddRequest) -> None:
        document = self._trim(
            "\n".join(f"{message.role}: {message.content}" for message in request.messages)
        )
        try:
            self._upsert(
                [_record_id(request.request_id)],
                [document],
                [
                    {
                        "request_id": request.request_id,
                        "user_id": request.user_id,
                        "session_id": request.session_id,
                        "created_at": _created_at(request),
                    }
                ],
            )
        except Exception:
            # The durable raw-evidence store is still completed by the API
            # layer, so a temporary embedding outage must not stall ingestion.
            logger.exception("Fast vector upsert failed; keeping raw evidence")

    def search(self, request: SearchRequest, query: str) -> list[MemoryRecord]:
        try:
            embedding = self._embed([self._trim(query)])[0]
            with self._collection_lock:
                result = self._collection.query(
                    query_embeddings=[embedding],
                    n_results=request.top_k,
                    where={"user_id": request.user_id},
                    include=["documents", "metadatas", "distances"],
                )
        except Exception:
            logger.exception("Fast vector search failed; using lexical evidence only")
            return []

        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        records: list[MemoryRecord] = []
        for identifier, document, metadata, distance in zip(
            ids, documents, metadatas, distances, strict=False
        ):
            if not document:
                continue
            score = max(0.0, min(1.0, 1.0 - float(distance)))
            records.append(
                MemoryRecord(
                    id=str(identifier),
                    content=str(document),
                    score=round(score, 6),
                    created_at=str((metadata or {}).get("created_at") or "") or None,
                )
            )
        return records

    def backfill(self, documents: list[RawDocument], batch_size: int = 16) -> int:
        added = 0
        for offset in range(0, len(documents), batch_size):
            batch = documents[offset : offset + batch_size]
            ids = [_record_id(item.request_id) for item in batch]
            with self._collection_lock:
                existing = set(self._collection.get(ids=ids).get("ids", []))
            missing = [
                item
                for item, identifier in zip(batch, ids, strict=True)
                if identifier not in existing
            ]
            if not missing:
                continue
            try:
                self._upsert(
                    [_record_id(item.request_id) for item in missing],
                    [self._trim(item.content) for item in missing],
                    [
                        {
                            "request_id": item.request_id,
                            "user_id": item.user_id,
                            "session_id": item.session_id,
                            "created_at": item.created_at,
                        }
                        for item in missing
                    ],
                )
                added += len(missing)
            except Exception:
                logger.exception("Fast memory backfill batch failed")
        return added

    def close(self) -> None:
        self._client.close()
