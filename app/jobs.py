"""Web side of the jobs: create and redo enqueue Huey tasks; status, results and events are read from SQLite."""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.store import TERMINAL, Store
from app.worker import job_dir, output_path, translate_job

Status = Literal["queued", "running", "done", "error"]


@dataclass
class Job:
    id: str
    filename: str
    target: str
    purpose: str
    status: Status
    version: int  # the newest version (queued, running or finished)
    attempt: int
    created: float
    error: str | None
    percent: float
    result: dict[str, Any] | None  # the newest finished result (a failed redo keeps the previous one)
    result_version: int | None

    @property
    def dir(self) -> Path:
        return job_dir(self.id)

    @property
    def source(self) -> Path:
        return self.dir / "source.pdf"

    def output(self, version: int | None = None) -> Path:
        return output_path(self.id, version or self.version)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "filename": self.filename, "target": self.target, "purpose": self.purpose,
                "status": self.status, "version": self.version, "created": self.created, "percent": self.percent,
                "error": self.error}


class JobStore:
    def __init__(self, settings: Settings):
        self.s = settings
        self.root = settings.data_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = Store(settings.data_dir / "translator.sqlite3")

    async def create(self, filename: str, target: str, data: bytes, purpose: str = "publish") -> Job:
        job_id = uuid.uuid4().hex
        d = self.root / job_id
        d.mkdir(parents=True)
        (d / "source.pdf").write_bytes(data)
        self.db.create_job(job_id, filename, target, purpose)
        self.db.add_event(job_id, 1, 1, {"type": "stage", "stage": "queued", "t": 0, "v": 1, "percent": 0,
                                         "message": "Waiting for a free translator"})
        await run_in_threadpool(translate_job, job_id, 1, 1)  # off the event loop: immediate mode runs it here
        return self.get(job_id)

    async def redo(self, job: Job, decisions: dict[str, Any]) -> Job:
        if job.result is None:
            raise ValueError("Only a finished translation can be redone.")
        version = self.db.new_version(job.id, decisions)
        self.db.add_event(job.id, version, 1, {"type": "stage", "stage": "queued", "t": 0, "v": version,
                                               "percent": 0, "message": "Applying your decisions"})
        await run_in_threadpool(translate_job, job.id, version, 1)
        return self.get(job.id)

    def get(self, job_id: str) -> Job | None:
        row = self.db.job(job_id)
        if not row:
            return None
        done = self.db.version(job_id, with_result=True)
        return Job(id=job_id, filename=row["filename"], target=row["target"], purpose=row["purpose"],
                   status=row["status"], version=row["version"], attempt=row["attempt"], created=row["created"],
                   error=row["error"], percent=self.db.last_percent(job_id, row["version"], row["attempt"]),
                   result=done["result"] if done else None, result_version=done["version"] if done else None)

    def events(self, job: Job, version: int | None = None) -> list[dict[str, Any]]:
        v = self.db.version(job.id, version or job.version)
        return v["events"] if v else []

    def delete(self, job_id: str) -> None:
        self.db.delete(job_id)
        shutil.rmtree(self.root / job_id, ignore_errors=True)

    async def stream(self, job_id: str, poll: float = 0.25, keepalive: float = 15):
        """Events of the job's newest version as they are written by the worker. `PRAGMA data_version` tells
        us when another connection committed, so idle polls do not query the events table."""
        row = self.db.job(job_id)
        version, attempt, after, seen, idle = row["version"], row["attempt"], 0, None, 0.0
        while True:
            dv = self.db.data_version()
            if dv != seen:
                seen = dv
                row = self.db.job(job_id)  # read the status first: the final event is committed with it
                if row is None:
                    return
                if row["version"] != version or row["attempt"] != attempt:
                    version, attempt, after = row["version"], row["attempt"], 0  # requeued or redone
                for eid, event in self.db.events(job_id, version, attempt, after):
                    after = eid
                    idle = 0.0
                    yield event
                if row["status"] in TERMINAL:
                    return
            await asyncio.sleep(poll)
            idle += poll
            if idle >= keepalive:
                idle = 0.0
                yield None

    async def cleanup_loop(self) -> None:
        while True:
            for job_id in self.db.expired(time.time() - self.s.job_ttl_hours * 3600):
                self.delete(job_id)
            await asyncio.sleep(600)
