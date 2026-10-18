from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.schemas import AddRequest, MemoryRecord

_WORD_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)


@dataclass(frozen=True)
class Reservation:
    state: Literal["new", "cached", "busy", "conflict"]


def payload_digest(request: AddRequest) -> str:
    payload = request.model_dump(mode="json")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso_timestamp(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


class AdapterStore:
    """Small durable control plane for idempotency and verbatim evidence."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS add_requests (
                    request_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('processing', 'succeeded', 'failed')),
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS raw_evidence (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES add_requests(request_id)
                );

                CREATE INDEX IF NOT EXISTS idx_raw_evidence_user
                ON raw_evidence(user_id);
                """
            )
            # A crashed process cannot safely claim that an unfinished write succeeded.
            connection.execute(
                """
                UPDATE add_requests
                SET state = 'failed', updated_at = ?
                WHERE state = 'processing'
                """,
                (time.time(),),
            )

    def reserve(self, request: AddRequest) -> Reservation:
        digest = payload_digest(request)
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_hash, state FROM add_requests WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO add_requests
                        (request_id, payload_hash, user_id, session_id, state, updated_at)
                    VALUES (?, ?, ?, ?, 'processing', ?)
                    """,
                    (
                        request.request_id,
                        digest,
                        request.user_id,
                        request.session_id,
                        now,
                    ),
                )
                connection.commit()
                return Reservation("new")
            if row["payload_hash"] != digest:
                connection.rollback()
                return Reservation("conflict")
            if row["state"] == "succeeded":
                connection.rollback()
                return Reservation("cached")
            if row["state"] == "processing":
                connection.rollback()
                return Reservation("busy")
            connection.execute(
                "UPDATE add_requests SET state = 'processing', updated_at = ? WHERE request_id = ?",
                (now, request.request_id),
            )
            connection.commit()
            return Reservation("new")

    def complete(self, request: AddRequest) -> None:
        rows = [
            (
                f"raw:{request.request_id}:{index}",
                request.request_id,
                request.user_id,
                request.session_id,
                message.role,
                message.content,
                _iso_timestamp(message.timestamp),
                index,
            )
            for index, message in enumerate(request.messages)
        ]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                INSERT OR IGNORE INTO raw_evidence
                    (id, request_id, user_id, session_id, role, content, created_at, ordinal)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                "UPDATE add_requests SET state = 'succeeded', updated_at = ? WHERE request_id = ?",
                (time.time(), request.request_id),
            )
            connection.commit()

    def fail(self, request_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE add_requests SET state = 'failed', updated_at = ? WHERE request_id = ?",
                (time.time(), request_id),
            )

    def search_raw(self, user_id: str, query: str, limit: int) -> list[MemoryRecord]:
        if limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, created_at
                FROM raw_evidence
                WHERE user_id = ?
                ORDER BY created_at DESC, ordinal DESC
                LIMIT 4000
                """,
                (user_id,),
            ).fetchall()

        query_terms = set(_WORD_RE.findall(query.lower()))
        if not query_terms:
            return []
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            document_terms = _WORD_RE.findall(row["content"].lower())
            if not document_terms:
                continue
            overlap = sum(1 for term in document_terms if term in query_terms)
            if overlap == 0:
                continue
            unique_overlap = len(query_terms.intersection(document_terms))
            density = overlap / math.sqrt(len(document_terms) + 1)
            # Hy-Memory's default semantic threshold is 0.4. Keep lexical fallback
            # scores below that boundary so the two score families remain ordered.
            score = min(0.39, 0.05 + 0.06 * unique_overlap + 0.02 * density)
            scored.append((score, row))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            MemoryRecord(
                id=row["id"],
                content=f"{row['role']}: {row['content']}",
                score=round(score, 6),
                created_at=row["created_at"],
            )
            for score, row in scored[:limit]
        ]

