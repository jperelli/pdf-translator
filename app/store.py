"""SQLite persistence for jobs and their versions (history screen, downloads after a restart)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    target TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'publish',
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
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created DESC);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            # a restart interrupts running jobs: mark them so the history does not show them as running forever
            self._db.execute("UPDATE jobs SET status='error', error='Interrupted by a server restart.' "
                             "WHERE status IN ('queued', 'running')")

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def create_job(self, job_id: str, filename: str, target: str, purpose: str) -> None:
        now = time.time()
        self._exec("INSERT INTO jobs (id, filename, target, purpose, status, created, updated) "
                   "VALUES (?, ?, ?, ?, 'queued', ?, ?)", (job_id, filename, target, purpose, now, now))

    def set_status(self, job_id: str, status: str, error: str | None = None) -> None:
        self._exec("UPDATE jobs SET status=?, error=?, updated=? WHERE id=?", (status, error, time.time(), job_id))

    def save_version(self, job_id: str, version: int, result: dict[str, Any] | None, events: list[dict],
                     decisions: dict | None = None) -> None:
        self._exec("INSERT OR REPLACE INTO versions (job_id, version, created, decisions, result, events) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   (job_id, version, time.time(), json.dumps(decisions) if decisions else None,
                    json.dumps(result, ensure_ascii=False) if result else None, json.dumps(events, ensure_ascii=False)))
        if result:
            r = result["report"]
            tokens = sum(c["input_tokens"] + c["output_tokens"] for c in result.get("calls", []))
            cost = sum(c["cost_usd"] for c in result.get("calls", []))
            self._exec("UPDATE jobs SET versions=MAX(versions, ?), score=?, translation_score=?, layout_score=?, "
                       "pages=?, tokens=tokens+?, cost_usd=cost_usd+?, updated=? WHERE id=?",
                       (version, r["score"], r["translation_score"], r["layout_score"], len(r["pages"]), tokens,
                        cost, time.time(), job_id))

    def job(self, job_id: str) -> dict[str, Any] | None:
        row = self._exec("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def version(self, job_id: str, version: int | None = None) -> dict[str, Any] | None:
        if version is None:
            row = self._exec("SELECT * FROM versions WHERE job_id=? ORDER BY version DESC LIMIT 1", (job_id,)).fetchone()
        else:
            row = self._exec("SELECT * FROM versions WHERE job_id=? AND version=?", (job_id, version)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("decisions", "result", "events"):
            d[k] = json.loads(d[k]) if d[k] else None
        return d

    def list_versions(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._exec("SELECT job_id, version, created, decisions, result FROM versions WHERE job_id=? "
                          "ORDER BY version", (job_id,)).fetchall()
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
