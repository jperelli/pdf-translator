"""SQLite persistence: jobs, versions and the live event log shared by the web app and the queue workers."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    target TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'fast',
    status TEXT NOT NULL,
    error TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    pages INTEGER,
    versions INTEGER NOT NULL DEFAULT 0,
    score REAL,
    translation_score REAL,
    layout_score REAL,
    tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS versions (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    created REAL NOT NULL,
    decisions TEXT,
    result TEXT,
    events TEXT,
    PRIMARY KEY (job_id, version)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT NOT NULL,
    labels TEXT NOT NULL,
    value REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (name, labels)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created DESC);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, version, attempt, id);
"""
# columns added after the first release; ALTER TABLE keeps existing databases on /data usable
MIGRATIONS = {
    "jobs": {"version": "INTEGER NOT NULL DEFAULT 1", "attempt": "INTEGER NOT NULL DEFAULT 1", "heartbeat": "REAL",
             "worker": "TEXT", "queued_at": "REAL", "started": "REAL"},
    "versions": {"attempt": "INTEGER NOT NULL DEFAULT 1"},
}
TERMINAL = ("done", "error")
# Prometheus-style histogram buckets (seconds)
DURATION_BUCKETS = (10, 30, 60, 120, 300, 600, 1200, 1800, 3600)
WAIT_BUCKETS = (1, 5, 15, 60, 300, 900, 3600)


def _labels(labels: dict[str, Any] | None) -> str:
    return json.dumps(dict(sorted((labels or {}).items())), separators=(",", ":"))


def _count(db: sqlite3.Connection, name: str, labels: dict[str, Any] | None = None, by: float = 1) -> None:
    """Monotonic counters for /metrics: they survive restarts and the TTL cleanup of old jobs."""
    db.execute("INSERT INTO counters (name, labels, value) VALUES (?, ?, ?) "
               "ON CONFLICT(name, labels) DO UPDATE SET value=value+excluded.value", (name, _labels(labels), by))


def _observe(db: sqlite3.Connection, name: str, value: float, buckets: tuple, labels: dict | None = None) -> None:
    for le in buckets:
        if value <= le:
            _count(db, f"{name}_bucket", {**(labels or {}), "le": str(le)})
    _count(db, f"{name}_bucket", {**(labels or {}), "le": "+Inf"})
    _count(db, f"{name}_sum", labels, value)
    _count(db, f"{name}_count", labels)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols.items():
                    if name not in have:
                        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            # rows from before jobs.version existed got 1: point them at their newest version (idempotent)
            self._db.execute("UPDATE jobs SET version=(SELECT MAX(version) FROM versions v WHERE v.job_id=jobs.id) "
                             "WHERE version < (SELECT MAX(version) FROM versions v WHERE v.job_id=jobs.id)")

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def data_version(self) -> int:
        """Changes whenever another connection (a worker) commits: a cheap "anything new?" check."""
        return self._exec("PRAGMA data_version").fetchone()[0]

    # ------------------------------------------------------------------ queue side

    def create_job(self, job_id: str, filename: str, target: str, purpose: str) -> None:
        now = time.time()
        with self._tx() as db:
            db.execute("INSERT INTO jobs (id, filename, target, purpose, status, created, updated, version, attempt, "
                       "queued_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, 1, 1, ?)",
                       (job_id, filename, target, purpose, now, now, now))
            _count(db, "jobs_created_total", {"kind": "translation"})
            db.execute("INSERT INTO versions (job_id, version, created, attempt) VALUES (?, 1, ?, 1)", (job_id, now))

    def new_version(self, job_id: str, decisions: dict[str, Any]) -> int:
        """Queue a redo: the next version number, with the user's decisions attached."""
        with self._tx() as db:
            row = db.execute("SELECT version, status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in TERMINAL:
                raise ValueError("Only a finished translation can be redone.")
            version = row["version"] + 1
            now = time.time()
            db.execute("INSERT INTO versions (job_id, version, created, decisions, attempt) VALUES (?, ?, ?, ?, 1)",
                       (job_id, version, now, json.dumps(decisions)))
            db.execute("UPDATE jobs SET version=?, attempt=1, status='queued', error=NULL, heartbeat=NULL, updated=?, "
                       "queued_at=? WHERE id=?", (version, now, now, job_id))
            _count(db, "jobs_created_total", {"kind": "redo"})
        return version

    def start(self, job_id: str, version: int, attempt: int, worker: str = "") -> bool:
        """A worker claims the job; False if it was superseded (requeued, canceled, deleted) in the meantime."""
        now = time.time()
        with self._tx() as db:  # fetchall: a RETURNING statement holds the write lock until it is fully read
            rows = db.execute("UPDATE jobs SET status='running', heartbeat=?, updated=?, worker=?, started=? "
                              "WHERE id=? AND version=? AND attempt=? AND status='queued' RETURNING queued_at",
                              (now, now, worker, now, job_id, version, attempt)).fetchall()
            if rows and rows[0]["queued_at"]:
                _observe(db, "queue_wait_seconds", now - rows[0]["queued_at"], WAIT_BUCKETS)
            return bool(rows)

    def heartbeat(self, job_id: str, attempt: int) -> None:
        self._exec("UPDATE jobs SET heartbeat=? WHERE id=? AND attempt=? AND status='running'",
                   (time.time(), job_id, attempt))

    def add_event(self, job_id: str, version: int, attempt: int, event: dict[str, Any]) -> int:
        cur = self._exec("INSERT INTO events (job_id, version, attempt, data) VALUES (?, ?, ?, ?)",
                         (job_id, version, attempt, json.dumps(event, ensure_ascii=False)))
        return cur.lastrowid

    def finish(self, job_id: str, version: int, attempt: int, result: dict[str, Any] | None, error: str | None,
               final_event: dict[str, Any]) -> bool:
        """Result, final event and status in one transaction, so a reader never sees "done" without them."""
        with self._tx() as db:
            row = db.execute("SELECT version, attempt FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or (row["version"], row["attempt"]) != (version, attempt):
                return False  # superseded by a requeue: a newer attempt owns the job
            db.execute("INSERT INTO events (job_id, version, attempt, data) VALUES (?, ?, ?, ?)",
                       (job_id, version, attempt, json.dumps(final_event, ensure_ascii=False)))
            now = time.time()
            info = db.execute("SELECT purpose, started FROM jobs WHERE id=?", (job_id,)).fetchone()
            status = "done" if result is not None else "error"
            _count(db, "jobs_finished_total", {"status": status, "purpose": info["purpose"]})
            if info["started"]:
                _observe(db, "job_duration_seconds", now - info["started"], DURATION_BUCKETS, {"status": status})
            for c in (result or {}).get("calls", []):
                lab = {"model": c["model"], "stage": c["stage"]}
                _count(db, "llm_input_tokens_total", lab, c["input_tokens"])
                _count(db, "llm_output_tokens_total", lab, c["output_tokens"])
                _count(db, "llm_cached_tokens_total", lab, c.get("cached_tokens", 0))
                _count(db, "llm_requests_total", lab, c["requests"])
                _count(db, "llm_cost_usd_total", lab, c["cost_usd"])
            if result is not None:
                _count(db, "pages_translated_total", None, len(result["report"]["pages"]))
                db.execute("UPDATE versions SET result=?, attempt=? WHERE job_id=? AND version=?",
                           (json.dumps(result, ensure_ascii=False), attempt, job_id, version))
                r = result["report"]
                tokens = sum(c["input_tokens"] + c["output_tokens"] for c in result.get("calls", []))
                cost = sum(c["cost_usd"] for c in result.get("calls", []))
                db.execute("UPDATE jobs SET status='done', error=NULL, versions=MAX(versions, ?), score=?, "
                           "translation_score=?, layout_score=?, pages=?, tokens=tokens+?, cost_usd=cost_usd+?, "
                           "updated=? WHERE id=?",
                           (version, r["score"], r["translation_score"], r["layout_score"], len(r["pages"]), tokens,
                            cost, now, job_id))
            else:
                db.execute("UPDATE versions SET attempt=? WHERE job_id=? AND version=?", (attempt, job_id, version))
                db.execute("UPDATE jobs SET status='error', error=?, updated=? WHERE id=?", (error, now, job_id))
        return True

    def running(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._exec("SELECT * FROM jobs WHERE status='running'").fetchall()]

    def requeue(self, job_id: str, attempt: int) -> int | None:
        """The worker running `attempt` died: hand the job to a new attempt (its events start over)."""
        with self._tx() as db:
            cur = db.execute("UPDATE jobs SET status='queued', attempt=attempt+1, heartbeat=NULL, updated=? "
                             "WHERE id=? AND attempt=? AND status='running' RETURNING version, attempt",
                             (time.time(), job_id, attempt))
            rows = cur.fetchall()
            if not rows:
                return None
            row = rows[0]
            db.execute("UPDATE versions SET attempt=? WHERE job_id=? AND version=?",
                       (row["attempt"], job_id, row["version"]))
            db.execute("UPDATE jobs SET queued_at=? WHERE id=?", (time.time(), job_id))
            _count(db, "jobs_retried_total", {"reason": "lost_worker"})
            return row["attempt"]

    def retry(self, job_id: str) -> tuple[int, int]:
        """Admin: run a failed job again (same version, new attempt). Returns (version, attempt)."""
        now = time.time()
        with self._tx() as db:
            rows = db.execute("UPDATE jobs SET status='queued', attempt=attempt+1, error=NULL, heartbeat=NULL, "
                              "worker=NULL, updated=?, queued_at=? WHERE id=? AND status='error' "
                              "RETURNING version, attempt", (now, now, job_id)).fetchall()
            if not rows:
                raise ValueError("Only a failed job can be retried.")
            v, a = rows[0]["version"], rows[0]["attempt"]
            db.execute("UPDATE versions SET attempt=? WHERE job_id=? AND version=?", (a, job_id, v))
            db.execute("INSERT INTO events (job_id, version, attempt, data) VALUES (?, ?, ?, ?)",
                       (job_id, v, a, json.dumps({"type": "stage", "stage": "queued", "t": 0, "v": v, "percent": 0,
                                                  "message": "Retried by an administrator"})))
            _count(db, "jobs_retried_total", {"reason": "admin"})
        return v, a

    def cancel(self, job_id: str, message: str = "Canceled by an administrator.") -> None:
        """Admin: stop a queued or running job. The attempt counter moves on, so the worker running the old
        attempt can no longer write its result (and stops at its next event, see app/worker.py)."""
        with self._tx() as db:
            row = db.execute("SELECT version, attempt, status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] in TERMINAL:
                raise ValueError("Only a queued or running job can be canceled.")
            db.execute("INSERT INTO events (job_id, version, attempt, data) VALUES (?, ?, ?, ?)",
                       (job_id, row["version"], row["attempt"],
                        json.dumps({"type": "error", "message": message, "t": 0, "v": row["version"]})))
            db.execute("UPDATE versions SET attempt=? WHERE job_id=? AND version=?",
                       (row["attempt"], job_id, row["version"]))  # the log stays on the canceled attempt
            db.execute("UPDATE jobs SET status='error', error=?, attempt=attempt+1, updated=? WHERE id=?",
                       (message, time.time(), job_id))
            _count(db, "jobs_canceled_total", {"was": row["status"]})

    # ------------------------------------------------------------------ admin / metrics

    def set_meta(self, key: str, value: Any) -> None:
        self._exec("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))

    def meta(self, key: str, default: Any = None) -> Any:
        row = self._exec("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def counters(self) -> list[tuple[str, dict[str, str], float]]:
        rows = self._exec("SELECT name, labels, value FROM counters ORDER BY name, labels").fetchall()
        return [(r["name"], json.loads(r["labels"]), r["value"]) for r in rows]

    def status_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self._exec("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()}

    def by_status(self, status: str, limit: int = 50) -> list[dict[str, Any]]:
        order = "updated DESC" if status in TERMINAL else "COALESCE(queued_at, created)"
        rows = self._exec(f"SELECT * FROM jobs WHERE status=? ORDER BY {order} LIMIT ?", (status, limit)).fetchall()
        return [dict(r) for r in rows]

    def last_event(self, job_id: str, version: int, attempt: int) -> dict[str, Any] | None:
        row = self._exec("SELECT data FROM events WHERE job_id=? AND version=? AND attempt=? AND "
                         "json_extract(data, '$.type') NOT IN ('trace', 'tool') ORDER BY id DESC LIMIT 1",
                         (job_id, version, attempt)).fetchone()
        return json.loads(row[0]) if row else None

    def fail(self, job_id: str, attempt: int, message: str) -> None:
        row = self.job(job_id)
        if row and row["attempt"] == attempt:
            self.finish(job_id, row["version"], attempt, None, message, {"type": "error", "message": message})

    # ------------------------------------------------------------------ reads

    def job(self, job_id: str) -> dict[str, Any] | None:
        row = self._exec("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def events(self, job_id: str, version: int, attempt: int, after: int = 0) -> list[tuple[int, dict[str, Any]]]:
        rows = self._exec("SELECT id, data FROM events WHERE job_id=? AND version=? AND attempt=? AND id>? "
                          "ORDER BY id", (job_id, version, attempt, after)).fetchall()
        return [(r["id"], json.loads(r["data"])) for r in rows]

    def last_percent(self, job_id: str, version: int, attempt: int) -> float:
        row = self._exec("SELECT json_extract(data, '$.percent') FROM events WHERE job_id=? AND version=? AND "
                         "attempt=? AND json_extract(data, '$.percent') IS NOT NULL ORDER BY id DESC LIMIT 1",
                         (job_id, version, attempt)).fetchone()
        return row[0] if row else 0

    def version(self, job_id: str, version: int | None = None, with_result: bool = False) -> dict[str, Any] | None:
        sql = "SELECT * FROM versions WHERE job_id=?"
        args: tuple = (job_id,)
        if version is not None:
            sql += " AND version=?"
            args += (version,)
        if with_result:
            sql += " AND result IS NOT NULL"
        row = self._exec(sql + " ORDER BY version DESC LIMIT 1", args).fetchone()
        if not row:
            return None
        d = dict(row)
        d["decisions"] = json.loads(d["decisions"]) if d["decisions"] else None
        d["result"] = json.loads(d["result"]) if d["result"] else None
        evs = [e for _, e in self.events(job_id, d["version"], d["attempt"])]
        d["events"] = evs or (json.loads(d["events"]) if d["events"] else [])  # older rows kept them inline
        return d

    def list_versions(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._exec("SELECT version, created, decisions, result FROM versions WHERE job_id=? ORDER BY version",
                          (job_id,)).fetchall()
        out = []
        for r in rows:
            res = json.loads(r["result"]) if r["result"] else None
            rep = res["report"] if res else None
            out.append({"version": r["version"], "created": r["created"],
                        "decisions": json.loads(r["decisions"]) if r["decisions"] else None,
                        "score": rep and rep["score"], "translation_score": rep and rep["translation_score"],
                        "layout_score": rep and rep["layout_score"],
                        "tokens": sum(c["input_tokens"] + c["output_tokens"] for c in res.get("calls", [])) if res else 0,
                        "cost_usd": round(sum(c["cost_usd"] for c in res.get("calls", [])), 4) if res else 0})
        return out

    def list_jobs(self, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        rows = self._exec("SELECT * FROM jobs ORDER BY created DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        total = self._exec("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return [dict(r) for r in rows], total

    def delete(self, job_id: str) -> None:
        self._exec("DELETE FROM jobs WHERE id=?", (job_id,))

    def pin(self, job_id: str, pinned: bool) -> None:
        self._exec("UPDATE jobs SET pinned=? WHERE id=?", (int(pinned), job_id))

    def expired(self, cutoff: float) -> list[str]:
        rows = self._exec("SELECT id FROM jobs WHERE pinned=0 AND updated < ? AND status IN ('done', 'error')",
                          (cutoff,)).fetchall()
        return [r[0] for r in rows]
