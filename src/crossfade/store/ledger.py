"""The experiment ledger: append-only SQLite.

- `objects` holds canonical objects (one row per object, inserted once).
- `events` is the append-only history. Each event carries the hash of the previous one, so any edit or
  deletion breaks the chain and `verify_chain` reports it. SQLite triggers reject UPDATE and DELETE on both
  tables, so ordinary code paths cannot rewrite history.
- `spans` is a query index over normalized traces (the traces themselves live in the artifact store).

Hash chaining detects accidental or casual modification. It is not tamper-proof against someone with
write access to the file; exported evidence bundles can be signed or anchored externally for that.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from ..models import Event, Span, new_id, now

T = TypeVar("T", bound=BaseModel)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  project_id TEXT,
  schema_version INTEGER NOT NULL,
  created_at REAL NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS objects_kind ON objects(kind, created_at);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE NOT NULL,
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  at REAL NOT NULL,
  project_id TEXT,
  subject TEXT,
  payload TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_subject ON events(subject);
CREATE TABLE IF NOT EXISTS spans (
  run_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  span_id TEXT NOT NULL,
  parent_id TEXT,
  name TEXT NOT NULL,
  service TEXT,
  start REAL, end_ REAL, status TEXT, source TEXT,
  attributes TEXT,
  PRIMARY KEY (run_id, span_id)
);
CREATE INDEX IF NOT EXISTS spans_name ON spans(name);
CREATE TRIGGER IF NOT EXISTS objects_no_update BEFORE UPDATE ON objects BEGIN SELECT RAISE(ABORT, 'objects are immutable'); END;
CREATE TRIGGER IF NOT EXISTS objects_no_delete BEFORE DELETE ON objects BEGIN SELECT RAISE(ABORT, 'objects are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


def _event_hash(prev: str, body: dict[str, Any]) -> str:
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, default=str)).encode()).hexdigest()


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._local = threading.local()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)

    @property
    def db(self) -> sqlite3.Connection:
        """One connection per thread (the API serves requests from a thread pool); writes are serialized by _lock."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    # ----- objects -----

    def put(self, obj: BaseModel, actor: str = "system", event: str | None = None) -> BaseModel:
        data = obj.model_dump(mode="json")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT INTO objects (id, kind, project_id, schema_version, created_at, data) VALUES (?,?,?,?,?,?)",
                    (data["id"], data["kind"], data.get("project_id") or (data["id"] if data["kind"] == "project" else None),
                     data["schema_version"], data["created_at"], json.dumps(data, sort_keys=True)),
                )
                self._append_locked(event or f"{data['kind']}.created", actor, data.get("project_id"), data["id"], {"kind": data["kind"]})
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return obj

    def get(self, oid: str, cls: type[T]) -> T:
        row = self.db.execute("SELECT data FROM objects WHERE id=?", (oid,)).fetchone()
        if not row:
            raise KeyError(oid)
        return cls.model_validate_json(row["data"])

    def find(self, oid_prefix: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT data FROM objects WHERE id=? OR id LIKE ? ORDER BY created_at LIMIT 2", (oid_prefix, oid_prefix + "%")).fetchall()
        return json.loads(row[0]["data"]) if len(row) == 1 else None

    def list(self, kind: str, cls: type[T], project_id: str | None = None, limit: int = 100, offset: int = 0) -> builtins.list[T]:
        q, args = "SELECT data FROM objects WHERE kind=?", builtins.list[Any]([kind])
        if project_id:
            q += " AND project_id=?"
            args.append(project_id)
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [cls.model_validate_json(r["data"]) for r in self.db.execute(q, args)]

    def count(self, kind: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM objects WHERE kind=?", (kind,)).fetchone()[0]

    # ----- events -----

    def _append_locked(self, type_: str, actor: str, project_id: str | None, subject: str | None, payload: dict[str, Any]) -> Event:
        last = self.db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else ""
        ev = Event(id=new_id("ev"), type=type_, actor=actor, at=now(), project_id=project_id, subject=subject, payload=payload, prev_hash=prev)
        body = ev.model_dump(exclude={"seq", "hash"})
        ev.hash = _event_hash(prev, body)
        self.db.execute(
            "INSERT INTO events (id, type, actor, at, project_id, subject, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (ev.id, ev.type, ev.actor, ev.at, ev.project_id, ev.subject, json.dumps(payload, sort_keys=True, default=str), ev.prev_hash, ev.hash),
        )
        return ev

    def append(self, type_: str, actor: str = "system", project_id: str | None = None, subject: str | None = None, **payload: Any) -> Event:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                ev = self._append_locked(type_, actor, project_id, subject, payload)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return ev

    def events(self, subject: str | None = None, limit: int = 500) -> builtins.list[Event]:
        q, args = "SELECT * FROM events", builtins.list[Any]()
        if subject:
            q += " WHERE subject=?"
            args.append(subject)
        q += " ORDER BY seq LIMIT ?"
        args.append(limit)
        return [Event(seq=r["seq"], id=r["id"], type=r["type"], actor=r["actor"], at=r["at"], project_id=r["project_id"],
                      subject=r["subject"], payload=json.loads(r["payload"]), prev_hash=r["prev_hash"], hash=r["hash"]) for r in self.db.execute(q, args)]

    def verify_chain(self) -> tuple[int, builtins.list[int]]:
        """Recompute the hash chain. Returns (events checked, seqs that don't match)."""
        bad: builtins.list[int] = []
        prev, n = "", 0
        for r in self.db.execute("SELECT * FROM events ORDER BY seq"):
            n += 1
            ev = Event(id=r["id"], type=r["type"], actor=r["actor"], at=r["at"], project_id=r["project_id"], subject=r["subject"],
                       payload=json.loads(r["payload"]), prev_hash=r["prev_hash"])
            if r["prev_hash"] != prev or _event_hash(prev, ev.model_dump(exclude={"seq", "hash"})) != r["hash"]:
                bad.append(r["seq"])
            prev = r["hash"]
        return n, bad

    # ----- spans index -----

    def index_spans(self, run_id: str, spans: Iterable[Span]) -> None:
        with self._lock, self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, s.trace_id, s.span_id, s.parent_id, s.name, s.service, s.start, s.end, s.status, s.source,
                  json.dumps(s.attributes, default=str)) for s in spans],
            )

    def spans(self, run_id: str) -> builtins.list[Span]:
        return [Span(trace_id=r["trace_id"], span_id=r["span_id"], parent_id=r["parent_id"], name=r["name"], service=r["service"] or "",
                     start=r["start"], end=r["end_"], status=r["status"], source=r["source"], attributes=json.loads(r["attributes"] or "{}"))
                for r in self.db.execute("SELECT * FROM spans WHERE run_id=? ORDER BY start", (run_id,))]

    def span_search(self, name_like: str = "%", status: str | None = None, limit: int = 200) -> builtins.list[dict[str, Any]]:
        q, args = "SELECT run_id, name, status, (end_-start)*1000 AS ms FROM spans WHERE name LIKE ?", builtins.list[Any]([name_like])
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY start DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(q, args)]
