from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import FakeMemoryClient


def test_health_is_public(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["memory_ready"] is True


def test_add_requires_auth(client: TestClient, add_payload: dict[str, Any]) -> None:
    response = client.post("/add", json=add_payload)
    assert response.status_code == 401


def test_all_supported_auth_schemes(
    client: TestClient, add_payload: dict[str, Any]
) -> None:
    for index, auth_headers in enumerate(
        (
            {"Authorization": "Bearer test-secret"},
            {"Authorization": "Token test-secret"},
            {"X-Api-Key": "test-secret"},
        )
    ):
        payload = {**add_payload, "request_id": f"request-{index}"}
        response = client.post("/add", json=payload, headers=auth_headers)
        assert response.status_code == 200


def test_add_is_idempotent(
    client: TestClient,
    headers: dict[str, str],
    add_payload: dict[str, Any],
    fake_memory: FakeMemoryClient,
) -> None:
    first = client.post("/add", json=add_payload, headers=headers)
    second = client.post("/add", json=add_payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {
        "success": True,
        "request_id": add_payload["request_id"],
        "user_id": add_payload["user_id"],
        "session_id": add_payload["session_id"],
    }
    assert len(fake_memory.add_calls) == 1
    assert fake_memory.add_calls[0][1]["request_id"] == add_payload["request_id"]
    assert fake_memory.add_calls[0][1]["memory_at"].isoformat() == "2024-01-01T00:00:00"


def test_request_id_payload_conflict(
    client: TestClient, headers: dict[str, str], add_payload: dict[str, Any]
) -> None:
    assert client.post("/add", json=add_payload, headers=headers).status_code == 200
    changed = {**add_payload, "session_id": "a-different-session"}
    response = client.post("/add", json=changed, headers=headers)
    assert response.status_code == 409


def test_search_maps_hy_memory_and_raw_evidence(
    client: TestClient,
    headers: dict[str, str],
    add_payload: dict[str, Any],
    fake_memory: FakeMemoryClient,
) -> None:
    assert client.post("/add", json=add_payload, headers=headers).status_code == 200
    response = client.post(
        "/search",
        headers=headers,
        json={
            "query": "Where does the user live?",
            "options": ["A. Singapore", "B. Tokyo"],
            "user_id": add_payload["user_id"],
            "top_k": 100,
        },
    )

    assert response.status_code == 200
    records = response.json()["data"]
    assert records[0]["id"] == "hy-1"
    assert records[0]["content"] == "The user lives in Singapore."
    assert any(record["id"].startswith("raw:") for record in records)
    scores = [record["score"] or 0 for record in records]
    assert scores == sorted(scores, reverse=True)
    assert fake_memory.search_calls[0][1]["user_ids"] == [add_payload["user_id"]]


def test_raw_evidence_is_isolated_by_user(
    client: TestClient, headers: dict[str, str], add_payload: dict[str, Any]
) -> None:
    assert client.post("/add", json=add_payload, headers=headers).status_code == 200
    response = client.post(
        "/search",
        headers=headers,
        json={"query": "ramen", "user_id": "different-user", "top_k": 100},
    )
    assert response.status_code == 200
    assert not any(item["id"].startswith("raw:") for item in response.json()["data"])

