from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from app.config import Settings
from app.memory import MemoryService
from app.schemas import AddRequest, MemoryRecord, Message, SearchRequest
from app.store import AdapterStore


class BlockingMemoryClient:
    def __init__(self) -> None:
        self.add_entered: list[str] = []
        self.search_active = 0
        self.search_peak = 0
        self.condition = threading.Condition()
        self.release_add = threading.Event()
        self.release_search = threading.Event()

    def add(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, bool]:
        with self.condition:
            self.add_entered.append(kwargs["user_id"])
            self.condition.notify_all()
        if not self.release_add.wait(timeout=5):
            raise TimeoutError("test did not release add")
        return {"success": True}

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        with self.condition:
            self.search_active += 1
            self.search_peak = max(self.search_peak, self.search_active)
            self.condition.notify_all()
        try:
            if not self.release_search.wait(timeout=5):
                raise TimeoutError("test did not release search")
            return {"memories": {"normal": [], "schema": [], "proactive": []}}
        finally:
            with self.condition:
                self.search_active -= 1

    def close(self) -> None:
        return None


class FakeFastMemory:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.queries: list[str] = []
        self.closed = False

    def add(self, request: AddRequest) -> None:
        self.added.append(request.request_id)

    def search(self, request: SearchRequest, query: str) -> list[MemoryRecord]:
        self.queries.append(query)
        return [MemoryRecord(id="fast-1", content="fast result", score=0.9)]

    def close(self) -> None:
        self.closed = True


def _service(tmp_path: Path, client: BlockingMemoryClient) -> MemoryService:
    settings = Settings(
        api_token="test-secret",
        data_dir=tmp_path,
        max_add_concurrency=2,
        max_search_concurrency=2,
    )
    return MemoryService(settings, AdapterStore(settings.db_path), client)


def _add_request(request_id: str, user_id: str) -> AddRequest:
    return AddRequest(
        request_id=request_id,
        messages=[Message(role="user", content="hello", timestamp=1704067200000)],
        user_id=user_id,
        session_id=f"session-{request_id}",
    )


def test_same_user_waiter_does_not_consume_global_add_slot(tmp_path: Path) -> None:
    client = BlockingMemoryClient()
    service = _service(tmp_path, client)

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(service.add, _add_request("a-1", "user-a"))
        with client.condition:
            assert client.condition.wait_for(
                lambda: client.add_entered == ["user-a"], timeout=1
            )

        second = pool.submit(service.add, _add_request("a-2", "user-a"))
        other = pool.submit(service.add, _add_request("b-1", "user-b"))
        with client.condition:
            assert client.condition.wait_for(
                lambda: "user-b" in client.add_entered, timeout=1
            )

        client.release_add.set()
        first.result(timeout=2)
        second.result(timeout=2)
        other.result(timeout=2)


def test_same_user_searches_can_run_concurrently(tmp_path: Path) -> None:
    client = BlockingMemoryClient()
    service = _service(tmp_path, client)
    request = SearchRequest(query="hello", user_id="user-a", top_k=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.search, request)
        second = pool.submit(service.search, request)
        with client.condition:
            assert client.condition.wait_for(lambda: client.search_peak == 2, timeout=1)

        client.release_search.set()
        first.result(timeout=2)
        second.result(timeout=2)


def test_fast_memory_bypasses_llm_client(tmp_path: Path) -> None:
    settings = Settings(
        api_token="test-secret",
        data_dir=tmp_path,
        fast_memory_enabled=True,
    )
    fast_memory = FakeFastMemory()
    service = MemoryService(
        settings,
        AdapterStore(settings.db_path),
        fast_memory=fast_memory,
    )
    add_request = _add_request("fast-add", "user-a")
    search_request = SearchRequest(query="hello", user_id="user-a", top_k=10)

    service.add(add_request)
    result = service.search(search_request)
    service.close()

    assert fast_memory.added == ["fast-add"]
    assert fast_memory.queries == ["hello"]
    assert result[0].id == "fast-1"
    assert fast_memory.closed is True


def test_historical_fast_reads_do_not_bypass_new_llm_writes(tmp_path: Path) -> None:
    settings = Settings(
        api_token="test-secret", data_dir=tmp_path,
        fast_memory_enabled=False, fast_memory_read_fallback=True,
    )
    llm = BlockingMemoryClient()
    llm.release_add.set()
    llm.release_search.set()
    fast = FakeFastMemory()
    service = MemoryService(settings, AdapterStore(settings.db_path), llm, fast)
    service.add(_add_request("hy-new-write", "user-a"))
    assert llm.add_entered == ["user-a"]
    assert fast.added == []
    records = service.search(SearchRequest(query="hello", user_id="user-a", top_k=10))
    assert records[0].id == "fast-1"
    service.close()
