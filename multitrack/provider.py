"""Cross-process request budgets; transient retries never close peers' pools."""

import asyncio
import logging
import random
import sqlite3
import time
import uuid
from contextlib import contextmanager

import httpx

from multitrack.config import TrackSettings

logger = logging.getLogger(__name__)
TRANSIENT = {408, 425, 429, 500, 502, 503, 504, 524}


class Budget:
    def __init__(self, settings: TrackSettings):
        self.path = settings.shared_budget_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS lanes (
                    name TEXT PRIMARY KEY, ceiling INTEGER, target INTEGER,
                    cooldown REAL DEFAULT 0, adjusted REAL DEFAULT 0,
                    ok INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, peak INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS leases (id TEXT PRIMARY KEY, lane TEXT, expiry REAL);
                CREATE INDEX IF NOT EXISTS lease_lane ON leases(lane, expiry);
            """)
            for name, ceiling, initial in (
                ("llm", settings.provider_max_concurrency, settings.provider_initial_concurrency),
                (
                    "embedding",
                    settings.embedding_max_concurrency,
                    settings.embedding_max_concurrency,
                ),
            ):
                db.execute(
                    "INSERT OR IGNORE INTO lanes(name,ceiling,target) VALUES(?,?,?)",
                    (name, ceiling, initial),
                )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            yield connection
        finally:
            connection.close()

    def try_acquire(self, lane):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM leases WHERE expiry < ?", (now,))
            target, cooldown = db.execute(
                "SELECT target,cooldown FROM lanes WHERE name=?", (lane,)
            ).fetchone()
            active = db.execute("SELECT count(*) FROM leases WHERE lane=?", (lane,)).fetchone()[0]
            if now < cooldown or active >= target:
                db.rollback()
                return None
            token = uuid.uuid4().hex
            db.execute("INSERT INTO leases VALUES(?,?,?)", (token, lane, now + 600))
            db.execute("UPDATE lanes SET peak=max(peak,?) WHERE name=?", (active + 1, lane))
            db.commit()
            return token

    def acquire(self, lane, timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            token = self.try_acquire(lane)
            if token:
                return token
            time.sleep(random.uniform(0.08, 0.16))
        raise TimeoutError("Provider budget queue expired")

    def finish(self, token, lane, status, delay=0):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM leases WHERE id=?", (token,))
            if status in TRANSIENT or status == 0:
                # Only one reduction per second across all services, avoiding a
                # simultaneous failure wave reducing the target dozens of times.
                db.execute(
                    """UPDATE lanes SET
                    target=CASE WHEN adjusted<? THEN max(1,target/2) ELSE target END,
                    adjusted=?, cooldown=max(cooldown,?), errors=errors+1 WHERE name=?""",
                    (now - 1, now, now + max(1, delay), lane),
                )
            elif 200 <= status < 300:
                db.execute(
                    """UPDATE lanes SET
                    target=CASE WHEN adjusted<? THEN min(ceiling,target+1) ELSE target END,
                    adjusted=CASE WHEN adjusted<? THEN ? ELSE adjusted END,
                    ok=ok+1 WHERE name=?""",
                    (now - 5, now - 5, now, lane),
                )
            else:
                db.execute("UPDATE lanes SET errors=errors+1 WHERE name=?", (lane,))
            db.commit()

    def snapshot(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return {row["name"]: dict(row) for row in db.execute("SELECT * FROM lanes")}


def retry_delay(response, attempt):
    try:
        advertised = float(response.headers.get("Retry-After", "0")) if response else 0
    except ValueError:
        advertised = 0
    return min(60, max(advertised, min(16, 2**attempt) + random.uniform(0, 1)))


class BudgetTransport(httpx.BaseTransport):
    def __init__(self, budget, attempts=4, inner=None):
        self.budget, self.attempts = budget, attempts
        self.inner = inner or httpx.HTTPTransport(
            limits=httpx.Limits(
                max_connections=128, max_keepalive_connections=64, keepalive_expiry=30
            )
        )

    def handle_request(self, request):
        lane = "embedding" if request.url.path.endswith("/embeddings") else "llm"
        for attempt in range(self.attempts):
            started = time.monotonic()
            token = self.budget.acquire(lane)
            response = None
            code = 0
            try:
                response = self.inner.handle_request(request)
                response.read()
                code = response.status_code
            except httpx.TransportError:
                if attempt + 1 == self.attempts:
                    raise
            finally:
                delay = retry_delay(response, attempt)
                self.budget.finish(token, lane, code, delay)
            logger.info(
                "provider lane=%s status=%s attempt=%s seconds=%.3f",
                lane,
                code,
                attempt + 1,
                time.monotonic() - started,
            )
            if response is not None and (code not in TRANSIENT or attempt + 1 == self.attempts):
                return response
            if response is not None:
                response.close()
            time.sleep(delay)
        raise RuntimeError("Provider attempts exhausted")

    def close(self):
        self.inner.close()


class AsyncBudgetTransport(httpx.AsyncBaseTransport):
    def __init__(self, budget, attempts=4, inner=None):
        self.budget, self.attempts = budget, attempts
        self.inner = inner or httpx.AsyncHTTPTransport(
            limits=httpx.Limits(
                max_connections=128, max_keepalive_connections=64, keepalive_expiry=30
            )
        )

    async def handle_async_request(self, request):
        lane = "embedding" if request.url.path.endswith("/embeddings") else "llm"
        for attempt in range(self.attempts):
            started = time.monotonic()
            deadline = time.monotonic() + 240
            token = None
            while token is None:
                # Short SQLite transactions; no thread is occupied while queued.
                token = self.budget.try_acquire(lane)
                if token is None:
                    if time.monotonic() >= deadline:
                        raise httpx.PoolTimeout("Provider budget queue expired")
                    await asyncio.sleep(random.uniform(0.08, 0.16))
            response = None
            code = 0
            try:
                response = await self.inner.handle_async_request(request)
                await response.aread()
                code = response.status_code
            except httpx.TransportError:
                if attempt + 1 == self.attempts:
                    raise
            finally:
                delay = retry_delay(response, attempt)
                self.budget.finish(token, lane, code, delay)
            logger.info(
                "provider lane=%s status=%s attempt=%s seconds=%.3f",
                lane,
                code,
                attempt + 1,
                time.monotonic() - started,
            )
            if response is not None and (code not in TRANSIENT or attempt + 1 == self.attempts):
                return response
            if response is not None:
                await response.aclose()
            await asyncio.sleep(delay)
        raise RuntimeError("Provider attempts exhausted")

    async def aclose(self):
        await self.inner.aclose()


def create_hy_client(settings, budget):
    from importlib.metadata import version

    from hy_memory import HyMemoryClient
    from hy_memory.agent.reconciler import MemoryReconciler
    from hy_memory.config import MemoryConfig
    from hy_memory.core.embed_service import EmbedService

    from app.hy_transport import install_shared_pool_fix

    if version("hy-memory") != "2.0.3":
        raise RuntimeError("Review compatibility hooks before upgrading Hy-Memory")
    install_shared_pool_fix()
    # Embeddings had the same shared-client eviction bug as the LLM path.
    EmbedService._evict_client = lambda self, *args, **kwargs: None
    original = MemoryReconciler.reconcile
    if not getattr(original, "_strict_reconcile", False):

        async def strict_reconcile(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            if getattr(result, "degraded", False):
                result.success = False
                result.error = "Reconciliation unavailable; retry the logical write"
            return result

        strict_reconcile._strict_reconcile = True
        MemoryReconciler.reconcile = strict_reconcile
    config = MemoryConfig.from_env()
    config.http_transport_factory = lambda: AsyncBudgetTransport(budget, settings.provider_attempts)
    return HyMemoryClient(config=config, mode=settings.memory_mode)
