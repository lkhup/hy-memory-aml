from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeMemoryClient:
    def __init__(self) -> None:
        self.add_calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
        self.search_calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def add(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.add_calls.append((messages, kwargs))
        return {"success": True}

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.search_calls.append((query, kwargs))
        return {
            "memories": {
                "normal": [
                    {
                        "memory_id": "hy-1",
                        "content": "The user lives in Singapore.",
                        "score": 0.91,
                        "memory_at": 1704067200,
                    }
                ],
                "schema": [],
                "proactive": [],
            }
        }

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_memory() -> FakeMemoryClient:
    return FakeMemoryClient()


@pytest.fixture
def client(tmp_path: Path, fake_memory: FakeMemoryClient) -> Iterator[TestClient]:
    settings = Settings(
        api_token="test-secret",
        data_dir=tmp_path,
        docs_enabled=False,
        raw_evidence_limit=20,
    )
    with TestClient(create_app(settings=settings, memory_client=fake_memory)) as test_client:
        yield test_client


@pytest.fixture
def headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-secret"}


@pytest.fixture
def add_payload() -> dict[str, Any]:
    return {
        "request_id": "eval:run_abc:locomo:conv-0:chunk-0",
        "messages": [
            {
                "role": "user",
                "content": "I live in Singapore and I like ramen.",
                "timestamp": 1704067200000,
            }
        ],
        "user_id": "eval:run_abc:locomo:conv-0",
        "session_id": "eval:run_abc:sample:0",
    }

