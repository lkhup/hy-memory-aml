from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from app.config import Settings
from app.schemas import AddRequest, MemoryRecord, SearchRequest
from app.store import AdapterStore

logger = logging.getLogger(__name__)


class MemoryClient(Protocol):
    def add(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any: ...

    def search(self, query: str, **kwargs: Any) -> Mapping[str, Any]: ...

    def close(self) -> None: ...


def create_hy_memory_client(settings: Settings) -> MemoryClient:
    from hy_memory import HyMemoryClient

    return HyMemoryClient(mode=settings.memory_mode)


def _stable_id(content: str) -> str:
    return "hy:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return (
                datetime.fromtimestamp(seconds, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _as_memory_record(item: Any) -> MemoryRecord | None:
    if isinstance(item, str):
        content = item.strip()
        return MemoryRecord(id=_stable_id(content), content=content) if content else None
    if not isinstance(item, Mapping):
        return None
    content_value = item.get("content") or item.get("text") or item.get("statement")
    if not isinstance(content_value, str) or not content_value.strip():
        return None
    content = content_value.strip()
    identifier = item.get("id") or item.get("memory_id") or item.get("uuid") or _stable_id(content)
    score_value = item.get("score")
    try:
        score = float(score_value) if score_value is not None else None
    except (TypeError, ValueError):
        score = None
    created_at = _to_iso(
        item.get("created_at")
        or item.get("memory_at")
        or item.get("gmt_created")
        or item.get("timestamp")
    )
    return MemoryRecord(
        id=str(identifier), content=content, score=score, created_at=created_at
    )


def flatten_hy_memory_result(result: Mapping[str, Any]) -> list[MemoryRecord]:
    candidates: list[Any] = []
    memories = result.get("memories")
    if isinstance(memories, Mapping):
        for channel in ("normal", "schema", "proactive"):
            channel_items = memories.get(channel, [])
            if isinstance(channel_items, list):
                candidates.extend(channel_items)
    for channel in ("facts", "experiences"):
        channel_items = result.get(channel, [])
        if isinstance(channel_items, list):
            candidates.extend(channel_items)

    records = [record for item in candidates if (record := _as_memory_record(item))]
    records.sort(key=lambda record: record.score if record.score is not None else -1, reverse=True)
    return records


def merge_records(
    semantic: list[MemoryRecord], raw: list[MemoryRecord], limit: int
) -> list[MemoryRecord]:
    merged: list[MemoryRecord] = []
    seen_ids: set[str] = set()
    seen_content: set[str] = set()
    for record in [*semantic, *raw]:
        normalized_content = " ".join(record.content.lower().split())
        if record.id in seen_ids or normalized_content in seen_content:
            continue
        seen_ids.add(record.id)
        seen_content.add(normalized_content)
        merged.append(record)
    merged.sort(
        key=lambda record: record.score if record.score is not None else 0.0,
        reverse=True,
    )
    return merged[:limit]


class MemoryService:
    def __init__(
        self,
        settings: Settings,
        store: AdapterStore,
        client: MemoryClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client or create_hy_memory_client(settings)
        self._stripes = [threading.RLock() for _ in range(64)]
        self._add_slots = threading.BoundedSemaphore(settings.max_add_concurrency)
        self._search_slots = threading.BoundedSemaphore(settings.max_search_concurrency)
        self.ready = True

    def _user_lock(self, user_id: str) -> threading.RLock:
        digest = hashlib.blake2b(user_id.encode("utf-8"), digest_size=2).digest()
        return self._stripes[int.from_bytes(digest, byteorder="big") % len(self._stripes)]

    def add(self, request: AddRequest) -> None:
        messages = [message.model_dump(exclude_none=True) for message in request.messages]
        timestamps = [message.timestamp for message in request.messages if message.timestamp]
        memory_at = None
        if timestamps:
            # Hy-Memory currently accepts one datetime for a batch. Keep it as naive UTC,
            # matching the SDK's internal datetime representation.
            memory_at = datetime.fromtimestamp(
                max(timestamps) / 1000, tz=timezone.utc
            ).replace(tzinfo=None)
        with self._add_slots, self._user_lock(request.user_id):
            result = self.client.add(
                messages,
                user_id=request.user_id,
                agent_id="aml-textual",
                session_id=request.session_id,
                source="aml",
                scene="chat",
                memory_at=memory_at,
                request_id=request.request_id,
                metadata={"aml_request_id": request.request_id},
            )
            if isinstance(result, Mapping) and result.get("success") is False:
                raise RuntimeError("Hy-Memory reported an unsuccessful Add operation")

    def search(self, request: SearchRequest) -> list[MemoryRecord]:
        query = request.query
        if self.settings.include_options_in_query and request.options:
            query = f"{query}\nOptions:\n" + "\n".join(request.options)
        raw_limit = min(self.settings.raw_evidence_limit, request.top_k)
        semantic_limit = request.top_k

        with self._search_slots, self._user_lock(request.user_id):
            result = self.client.search(
                query=query,
                user_ids=[request.user_id],
                agent_ids=["aml-textual"],
                limit=semantic_limit,
                schema_limit=min(10, semantic_limit),
                intention_limit=min(5, semantic_limit),
                search_mode="default",
            )
            if not isinstance(result, Mapping):
                raise RuntimeError("Hy-Memory returned an invalid Search response")
            semantic = flatten_hy_memory_result(result)
            raw = self.store.search_raw(request.user_id, query, raw_limit)
            return merge_records(semantic, raw, request.top_k)

    def close(self) -> None:
        self.ready = False
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


