"""Durable state for change detection and alert de-duplication (SQLite).

SQLite keeps the service dependency-free and survives restarts when the file
sits on a persistent volume (Azure Files in Container Apps). If the state is
lost, the service re-emits `created` for active entities; downstream tables
are keyed on entity_key and take the latest record, so that is safe — just
noisier.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    entity_key     TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    entity_type    TEXT NOT NULL,
    status         TEXT NOT NULL,          -- active | resolved
    content_hash   TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    missed_polls   INTEGER NOT NULL DEFAULT 0,
    resolved_at    TEXT,
    last_event     TEXT NOT NULL           -- JSON of last published event (no raw)
);
CREATE INDEX IF NOT EXISTS ix_entities_type_status ON entities(entity_type, status);

-- Transactional outbox: events are enqueued in the same transaction that
-- updates entity state, and deleted only after a successful publish.
CREATE TABLE IF NOT EXISTS outbox (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL UNIQUE,
    payload    TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


@dataclass
class EntityState:
    entity_key: str
    source: str
    entity_type: str
    status: str
    content_hash: str
    first_seen_at: datetime
    last_seen_at: datetime
    missed_polls: int
    resolved_at: datetime | None
    last_event: dict[str, Any]


class StateStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # Rollback journal, not WAL: WAL needs shared memory, which SMB mounts
        # (Azure Files in Container Apps) do not provide. Write volume is tiny.
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.executescript(_SCHEMA)

    @contextmanager
    def transaction(self):
        self._conn.execute("BEGIN")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -------------------------------------------------------------- entities

    def get(self, entity_key: str) -> EntityState | None:
        row = self._conn.execute("SELECT * FROM entities WHERE entity_key=?", (entity_key,)).fetchone()
        return _row(row) if row else None

    def active(self, entity_type: str | None = None, source: str | None = None) -> list[EntityState]:
        sql = "SELECT * FROM entities WHERE status='active'"
        args: list[Any] = []
        if entity_type:
            sql += " AND entity_type=?"
            args.append(entity_type)
        if source:
            sql += " AND source=?"
            args.append(source)
        return [_row(r) for r in self._conn.execute(sql, args)]

    def entity_types(self) -> list[str]:
        """Every entity type the store has tracked, active or not."""
        return [r[0] for r in self._conn.execute("SELECT DISTINCT entity_type FROM entities ORDER BY 1")]

    def upsert(self, s: EntityState) -> None:
        self._conn.execute(
            """INSERT INTO entities VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(entity_key) DO UPDATE SET
                 status=excluded.status, content_hash=excluded.content_hash,
                 first_seen_at=excluded.first_seen_at, last_seen_at=excluded.last_seen_at,
                 missed_polls=excluded.missed_polls, resolved_at=excluded.resolved_at,
                 last_event=excluded.last_event""",
            (s.entity_key, s.source, s.entity_type, s.status, s.content_hash,
             s.first_seen_at.isoformat(), s.last_seen_at.isoformat(), s.missed_polls,
             s.resolved_at.isoformat() if s.resolved_at else None, json.dumps(s.last_event)),
        )

    def touch_all_active(self, entity_type: str, source: str, seen_at: datetime) -> None:
        self._conn.execute(
            "UPDATE entities SET last_seen_at=?, missed_polls=0 "
            "WHERE status='active' AND entity_type=? AND source=?",
            (seen_at.isoformat(), entity_type, source),
        )

    def purge_resolved_before(self, cutoff: datetime) -> int:
        cur = self._conn.execute(
            "DELETE FROM entities WHERE status='resolved' AND resolved_at < ?", (cutoff.isoformat(),))
        return cur.rowcount

    # -------------------------------------------------------------- outbox

    def enqueue(self, events: list[dict[str, Any]], now: datetime) -> int:
        """Idempotent: an event_id already queued is ignored."""
        cur = self._conn.executemany(
            "INSERT OR IGNORE INTO outbox(event_id, payload, enqueued_at) VALUES (?,?,?)",
            [(e["event_id"], json.dumps(e, default=str), now.isoformat()) for e in events],
        )
        return cur.rowcount

    def peek_outbox(self, limit: int) -> list[tuple[int, dict[str, Any]]]:
        rows = self._conn.execute("SELECT seq, payload FROM outbox ORDER BY seq LIMIT ?", (limit,))
        return [(r["seq"], json.loads(r["payload"])) for r in rows]

    def ack_outbox(self, seqs: list[int]) -> None:
        self._conn.executemany("DELETE FROM outbox WHERE seq=?", [(s,) for s in seqs])

    def nack_outbox(self, seqs: list[int]) -> None:
        self._conn.executemany("UPDATE outbox SET attempts=attempts+1 WHERE seq=?", [(s,) for s in seqs])

    def outbox_depth(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    # -------------------------------------------------------------- key/value

    def get_kv(self, k: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def set_kv(self, k: str, v: Any) -> None:
        self._conn.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                           (k, json.dumps(v, default=str)))

    def reset(self) -> None:
        with self.transaction():
            self._conn.execute("DELETE FROM entities")
            self._conn.execute("DELETE FROM outbox")
            self._conn.execute("DELETE FROM kv")

    def close(self) -> None:
        self._conn.close()


def _row(r: sqlite3.Row) -> EntityState:
    return EntityState(
        entity_key=r["entity_key"], source=r["source"], entity_type=r["entity_type"],
        status=r["status"], content_hash=r["content_hash"],
        first_seen_at=datetime.fromisoformat(r["first_seen_at"]),
        last_seen_at=datetime.fromisoformat(r["last_seen_at"]),
        missed_polls=r["missed_polls"],
        resolved_at=datetime.fromisoformat(r["resolved_at"]) if r["resolved_at"] else None,
        last_event=json.loads(r["last_event"]),
    )
