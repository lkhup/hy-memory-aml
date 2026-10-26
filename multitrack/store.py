import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class TrackStore:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "evidence.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, digest TEXT, user_id TEXT, payload TEXT,
                    state TEXT, updated REAL);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, request_id TEXT, user_id TEXT, content TEXT,
                    created_at TEXT);
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, doc_id TEXT, user_id TEXT, request_id TEXT,
                    text TEXT, hy_done INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS chunk_user ON chunks(user_id);
                CREATE INDEX IF NOT EXISTS chunk_request ON chunks(request_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
                    id UNINDEXED, user_id UNINDEXED, text, tokenize='unicode61');
                CREATE TABLE IF NOT EXISTS descriptions (id TEXT PRIMARY KEY, text TEXT);
            """)
            # Only one application worker owns this track's journal.
            db.execute("UPDATE requests SET state='failed' WHERE state='processing'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def reserve(self, payload):
        raw = payload.model_dump_json(exclude_none=True)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT digest,state FROM requests WHERE id=?", (payload.request_id,)
            ).fetchone()
            if row is not None:
                if row["digest"] != digest:
                    return "conflict"
                if row["state"] in {"succeeded", "processing"}:
                    return row["state"]
                db.execute(
                    "UPDATE requests SET state='processing',updated=? WHERE id=?",
                    (time.time(), payload.request_id),
                )
            else:
                db.execute(
                    "INSERT INTO requests VALUES(?,?,?,?,?,?)",
                    (payload.request_id, digest, payload.user_id, raw, "processing", time.time()),
                )
            db.commit()
        return "new"

    def state(self, request_id, state):
        with self.connect() as db:
            db.execute(
                "UPDATE requests SET state=?,updated=? WHERE id=?", (state, time.time(), request_id)
            )

    def cached_description(self, key, text=None):
        with self.connect() as db:
            if text is not None:
                db.execute("INSERT OR IGNORE INTO descriptions VALUES(?,?)", (key, text))
                return text
            row = db.execute("SELECT text FROM descriptions WHERE id=?", (key,)).fetchone()
            return row[0] if row else None

    def put_document(self, identifier, payload, message, created_at):
        content = json.dumps(message.model_dump(mode="json")["content"], ensure_ascii=False)
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?)",
                (identifier, payload.request_id, payload.user_id, content, created_at),
            )

    def put_chunk(self, identifier, doc_id, payload, text):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute("SELECT hy_done FROM chunks WHERE id=?", (identifier,)).fetchone()
            if exists:
                return bool(exists[0])
            db.execute(
                "INSERT INTO chunks VALUES(?,?,?,?,?,0)",
                (identifier, doc_id, payload.user_id, payload.request_id, text),
            )
            db.execute(
                "INSERT INTO evidence_fts VALUES(?,?,?)", (identifier, payload.user_id, text)
            )
            db.commit()
        return False

    def mark_chunk(self, identifier):
        with self.connect() as db:
            db.execute("UPDATE chunks SET hy_done=1 WHERE id=?", (identifier,))

    def lexical(self, user, query, limit):
        terms = list(dict.fromkeys(re.findall(r"[\w]+", query, re.UNICODE)))[:64]
        if not terms:
            return []
        expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        with self.connect() as db:
            return [
                r[0]
                for r in db.execute(
                    """
                SELECT f.id FROM evidence_fts f
                JOIN requests r ON r.id=(SELECT request_id FROM chunks WHERE id=f.id)
                WHERE evidence_fts MATCH ? AND f.user_id=? AND r.state='succeeded'
                ORDER BY bm25(evidence_fts) LIMIT ?
            """,
                    (expression, user, limit),
                )
            ]

    def record(self, identifier, user, multimodal):
        with self.connect() as db:
            row = db.execute(
                """
                SELECT c.text,d.content,d.created_at,d.id FROM chunks c
                JOIN documents d ON d.id=c.doc_id JOIN requests r ON r.id=c.request_id
                WHERE c.id=? AND c.user_id=? AND r.state='succeeded'
            """,
                (identifier, user),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"] if multimodal else identifier,
            "content": json.loads(row["content"]) if multimodal else row["text"],
            "created_at": row["created_at"],
        }
