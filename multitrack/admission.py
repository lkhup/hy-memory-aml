import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import HTTPException


class Admission:
    """Wait on async primitives before occupying any worker thread."""

    def __init__(self, capacity: int, pending: int, timeout: float):
        self.slots = asyncio.Semaphore(capacity)
        self.maximum = capacity + pending
        self.timeout = timeout
        self.inflight = self.active = self.peak = 0
        self.rejected = self.completed = 0
        self.queue_seconds = 0.0
        self.users: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def enter(self, user: str | None = None):
        if self.inflight >= self.maximum:
            self.rejected += 1
            raise HTTPException(429, "Request queue is full", headers={"Retry-After": "5"})
        self.inflight += 1
        started = time.monotonic()
        user_lock = None
        if user is not None:
            user_lock, count = self.users.get(user, (asyncio.Lock(), 0))
            self.users[user] = user_lock, count + 1
        locked = slotted = False

        async def acquire():
            nonlocal locked, slotted
            if user_lock is not None:
                await user_lock.acquire()
                locked = True
            await self.slots.acquire()
            slotted = True

        try:
            try:
                await asyncio.wait_for(acquire(), timeout=self.timeout)
            except asyncio.TimeoutError as exc:
                self.rejected += 1
                raise HTTPException(
                    429, "Request queue wait expired", headers={"Retry-After": "5"}
                ) from exc
            self.queue_seconds += time.monotonic() - started
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                yield
                self.completed += 1
            finally:
                self.active -= 1
        finally:
            if slotted:
                self.slots.release()
            if locked:
                user_lock.release()
            if user is not None:
                lock, count = self.users[user]
                if count == 1:
                    del self.users[user]
                else:
                    self.users[user] = lock, count - 1
            self.inflight -= 1

    def snapshot(self):
        return {
            "active": self.active,
            "queued": self.inflight - self.active,
            "peak": self.peak,
            "completed": self.completed,
            "rejected": self.rejected,
            "queue_seconds": round(self.queue_seconds, 3),
        }
