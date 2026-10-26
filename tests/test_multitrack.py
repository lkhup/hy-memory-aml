import asyncio
import base64
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from multitrack.admission import Admission
from multitrack.api import create_app
from multitrack.config import TrackSettings
from multitrack.engine import Engine
from multitrack.gateway import create_gateway
from multitrack.provider import AsyncBudgetTransport, Budget
from multitrack.schemas import AddRequest, SearchRequest

PNG = (
    "data:image/png;base64,"
    + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"synthetic-test-image").decode()
)


class FakeHY:
    def __init__(self):
        self.add_calls = []

    def add(self, messages, **kwargs):
        self.add_calls.append((messages, kwargs))
        return {"success": True}

    def search(self, **kwargs):
        return {"memories": {"normal": []}}

    def close(self):
        pass


class FakeEmbedding:
    embeddings = property(lambda self: self)

    def create(self, input, **kwargs):
        return SimpleNamespace(
            data=[SimpleNamespace(index=i, embedding=[1.0, 0.0]) for i, _ in enumerate(input)]
        )


class FakeVectors:
    def __init__(self):
        self.items = {}

    def upsert(self, ids, embeddings, metadatas):
        self.items.update(zip(ids, metadatas, strict=True))

    def query(self, where, n_results, **kwargs):
        ids = [i for i, m in self.items.items() if m["user_id"] == where["user_id"]]
        return {"ids": [ids[:n_results]]}


def settings(tmp_path, track="coding", **kwargs):
    return TrackSettings(
        _env_file=None,
        api_token="test-secret",
        aml_track=track,
        track_root=tmp_path / track,
        shared_budget_path=tmp_path / "budget.db",
        memory_embedding_dims=2,
        vision_enabled=False,
        **kwargs,
    )


def payload(content="def repair(): return 'blue'", rid="write-1", user="user-1"):
    return {
        "request_id": rid,
        "user_id": user,
        "session_id": "session",
        "messages": [{"role": "user", "content": content, "timestamp": 1704067200000}],
    }


@pytest.mark.parametrize("track", ["coding", "multimodal"])
def test_complete_write_evidence_isolation_idempotency(tmp_path, track):
    config = settings(tmp_path, track)
    hy = FakeHY()
    engine = Engine(config, hy=hy, embedding=FakeEmbedding(), vectors=FakeVectors())
    content = "def repair(): return 'blue'"
    if track == "multimodal":
        content = [
            {"type": "text", "text": "blue diagram repair"},
            {"type": "image_url", "image_url": {"url": PNG}},
        ]
    headers = {"Authorization": "Bearer test-secret"}
    with TestClient(create_app(config, engine)) as client:
        assert client.post("/add", json=payload(content)).status_code == 401
        first = client.post("/add", json=payload(content), headers=headers)
        assert first.status_code == 200, first.text
        assert client.post("/add", json=payload(content), headers=headers).status_code == 200
        assert len(hy.add_calls) == 1
        assert hy.add_calls[0][1]["agent_id"] == "aml-" + track
        assert client.post("/add", json=payload("different"), headers=headers).status_code == 409
        query = {"query": "repair blue", "user_id": "user-1", "top_k": 100}
        result = client.post("/search", json=query, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["data"][0]["content"] == content
        query["user_id"] = "user-other"
        assert client.post("/search", json=query, headers=headers).json() == {"data": []}


def test_long_code_is_not_trimmed_and_retry_checkpoints_chunks(tmp_path):
    config = settings(tmp_path, chunk_chars=500)
    hy = FakeHY()
    engine = Engine(config, hy=hy, embedding=FakeEmbedding(), vectors=FakeVectors())
    original = "abc你def\n" * 900
    request = AddRequest.model_validate(payload(original))
    engine.store.reserve(request)
    engine.add(request)
    assert "".join(x[0][0]["content"] for x in hy.add_calls) == original
    assert len({x[1]["request_id"] for x in hy.add_calls}) == len(hy.add_calls)
    count = len(hy.add_calls)
    engine.add(request)
    assert len(hy.add_calls) == count


@pytest.mark.parametrize("uri", ["https://example.com/image.jpg", "data:image/png;base64,AAAA"])
def test_invalid_media_rejected(uri):
    with pytest.raises(ValidationError):
        AddRequest.model_validate(payload([{"type": "image_url", "image_url": {"url": uri}}]))


def test_same_user_queue_does_not_block_other_users_or_health_threads():
    async def run():
        gate = Admission(2, 100, 2)
        release = asyncio.Event()
        entered = []

        async def write(user):
            async with gate.enter(user):
                entered.append(user)
                await release.wait()

        tasks = [asyncio.create_task(write("same")) for _ in range(50)]
        tasks.append(asyncio.create_task(write("other")))
        for _ in range(100):
            if "other" in entered:
                break
            await asyncio.sleep(0.001)
        assert entered == ["same", "other"]
        release.set()
        await asyncio.gather(*tasks)
        assert not gate.users
        assert gate.active == gate.inflight == 0

    asyncio.run(run())


def test_64_requests_actually_enter_backend_concurrently(tmp_path):
    config = settings(tmp_path, max_add_concurrency=64)
    engine = Engine(config, hy=FakeHY(), embedding=FakeEmbedding(), vectors=FakeVectors())
    lock = threading.Lock()
    barrier = threading.Barrier(64)
    peak = 0
    active = 0

    def write(request):
        nonlocal peak, active
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=15)
        with lock:
            active -= 1

    engine.add = write
    with TestClient(create_app(config, engine)) as client:

        def send(i):
            return client.post(
                "/add",
                json=payload(rid=f"r-{i}", user=f"u-{i}"),
                headers={"X-Api-Key": "test-secret"},
            ).status_code

        with ThreadPoolExecutor(64) as pool:
            assert list(pool.map(send, range(64))) == [200] * 64
        assert peak == 64
        assert client.get("/health").status_code == 200


def test_shared_budget_bounds_two_process_equivalents(tmp_path):
    config = settings(tmp_path, provider_max_concurrency=3, provider_initial_concurrency=3)
    first, second = Budget(config), Budget(config)
    tokens = [first.try_acquire("llm") for _ in range(3)]
    assert all(tokens)
    assert second.try_acquire("llm") is None
    first.finish(tokens[0], "llm", 200)
    token = second.try_acquire("llm")
    assert token
    for item in tokens[1:] + [token]:
        second.finish(item, "llm", 200)
    assert first.snapshot()["llm"]["peak"] == 3


def test_provider_recovers_from_502_without_losing_successful_peers(tmp_path, monkeypatch):
    import multitrack.provider as provider

    monkeypatch.setattr(provider, "retry_delay", lambda *args: 0)
    config = settings(tmp_path)
    budget = Budget(config)
    calls = 0

    async def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(502 if calls == 1 else 200, json={"ok": True})

    async def run():
        transport = AsyncBudgetTransport(budget, attempts=2, inner=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=transport) as client:
            results = await asyncio.gather(
                *(client.post("https://test/v1/chat/completions") for _ in range(32))
            )
        assert [r.status_code for r in results] == [200] * 32
        assert calls == 33

    asyncio.run(run())
    assert budget.snapshot()["llm"]["errors"] == 1


def test_failed_embedding_is_never_acknowledged_as_a_success(tmp_path):
    config = settings(tmp_path)
    engine = Engine(config, hy=FakeHY(), embedding=FakeEmbedding(), vectors=FakeVectors())
    engine.embed = lambda texts: (_ for _ in ()).throw(RuntimeError("temporary embedding error"))
    with TestClient(create_app(config, engine), raise_server_exceptions=False) as client:
        response = client.post("/add", json=payload(), headers={"X-Api-Key": "test-secret"})
        assert response.status_code == 503
        assert engine.store.reserve(AddRequest.model_validate(payload())) == "new"


def test_gateway_preserves_track_path_auth_and_text_payload():
    received = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"success":true}'

    async def handler(request):
        received.append(
            (
                request.url.host,
                request.url.path,
                request.headers.get("authorization"),
                await request.aread(),
            )
        )
        return httpx.Response(200, stream=Stream(), headers={"content-type": "application/json"})

    targets = {
        "textual": "http://text.test",
        "coding": "http://code.test",
        "multimodal": "http://image.test",
    }
    with TestClient(create_gateway(targets, httpx.MockTransport(handler))) as client:
        for path in ["/add", "/coding/add", "/multimodal/add"]:
            response = client.post(
                path,
                content=b'{"original":"bytes"}',
                headers={"Authorization": "Token test-secret"},
            )
            assert response.status_code == 200
    assert [r[0] for r in received] == ["text.test", "code.test", "image.test"]
    assert all(r[1:] == ("/add", "Token test-secret", b'{"original":"bytes"}') for r in received)


def test_multimodal_query_preserves_order():
    parts = [
        {"type": "text", "text": "which diagram?"},
        {"type": "image_url", "image_url": {"url": PNG}},
        {"type": "text", "text": "compare its labels"},
    ]
    query = SearchRequest(query=parts, user_id="u", top_k=100)
    assert query.model_dump(exclude_none=True)["query"] == parts
