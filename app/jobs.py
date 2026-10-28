"""Jobs: background asyncio tasks streaming events to SSE clients, persisted in SQLite with one row per version."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.config import Settings
from app.formats import InvalidDocument
from app.observability import recorder
from app.pipeline import PipelineError, TranslationPipeline
from app.store import Store

log = logging.getLogger(__name__)
Status = Literal["queued", "running", "done", "error"]


@dataclass
class Job:
    id: str
    filename: str
    target: str
    purpose: str
    dir: Path
    status: Status = "queued"
    version: int = 1
    created: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    listeners: set[asyncio.Queue] = field(default_factory=set)
    started: float = field(default_factory=time.time)

    @property
    def source(self) -> Path:
        return self.dir / "source.pdf"

    def output(self, version: int | None = None) -> Path:
        return self.dir / f"translated.v{version or self.version}.pdf"

    def publish(self, event: dict[str, Any]) -> None:
        event = {"t": round(time.time() - self.started, 2), "v": self.version, **event}
        self.events.append(event)
        for q in list(self.listeners):
            q.put_nowait(event)

    def summary(self) -> dict[str, Any]:
        last = next((e for e in reversed(self.events) if "percent" in e), None)
        return {"id": self.id, "filename": self.filename, "target": self.target, "purpose": self.purpose,
                "status": self.status, "version": self.version, "created": self.created,
                "percent": last["percent"] if last else 0, "error": self.error}


class JobStore:
    def __init__(self, settings: Settings):
        self.s = settings
        self.jobs: dict[str, Job] = {}  # live (or recently used) jobs; the rest is loaded from SQLite on demand
        self.root = settings.data_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = Store(settings.data_dir / "translator.sqlite3")
        self.running = asyncio.Semaphore(2)  # documents translated at the same time

    # ------------------------------------------------------------------ lifecycle

    def create(self, filename: str, target: str, data: bytes, purpose: str = "publish") -> Job:
        job_id = uuid.uuid4().hex
        d = self.root / job_id
        d.mkdir(parents=True)
        job = Job(id=job_id, filename=filename, target=target, purpose=purpose, dir=d)
        job.source.write_bytes(data)
        self.jobs[job_id] = job
        self.db.create_job(job_id, filename, target, purpose)
        job.publish({"type": "stage", "stage": "queued", "message": "Waiting for a free translator", "percent": 0})
        asyncio.create_task(self._run(job, None))
        return job

    def redo(self, job: Job, decisions: dict[str, Any]) -> Job:
        if job.status not in ("done", "error") or not job.result:
            raise ValueError("Only a finished translation can be redone.")
        previous = job.result
        job.version += 1
        job.status, job.error, job.events, job.started = "queued", None, [], time.time()
        job.publish({"type": "stage", "stage": "queued", "message": "Applying your decisions", "percent": 0})
        asyncio.create_task(self._run(job, (previous, decisions)))
        return job

    def get(self, job_id: str) -> Job | None:
        if job_id in self.jobs:
            return self.jobs[job_id]
        row = self.db.job(job_id)
        if not row:
            return None
        v = self.db.version(job_id)
        job = Job(id=job_id, filename=row["filename"], target=row["target"], purpose=row["purpose"],
                  dir=self.root / job_id, status=row["status"], version=(v or {}).get("version", 1),
                  created=row["created"], events=(v or {}).get("events") or [], result=(v or {}).get("result"),
                  error=row["error"])
        self.jobs[job_id] = job
        return job

    def delete(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)
        self.db.delete(job_id)
        shutil.rmtree(self.root / job_id, ignore_errors=True)

    async def _run(self, job: Job, redo: tuple[dict, dict] | None) -> None:
        async with self.running:
            job.status = "running"
            job.started = time.time()
            self.db.set_status(job.id, "running")
            recorder.register(job.id, job.publish)
            decisions = redo[1] if redo else None
            try:
                pipeline = TranslationPipeline(self.s, job.publish, job_id=job.id, purpose=job.purpose,
                                               version=job.version)
                if redo:
                    job.result = await pipeline.redo(job.source, job.output(), job.target, job.filename, *redo)
                else:
                    job.result = await pipeline.run(job.source, job.output(), job.target, job.filename)
                job.status = "done"
                job.publish({"type": "done", "report": job.result["report"]})
            except (InvalidDocument, PipelineError, ValueError) as exc:
                self._fail(job, str(exc))
            except Exception as exc:  # never leak a stack trace to the user, keep it in the logs
                log.exception("job %s failed", job.id)
                self._fail(job, f"Unexpected error while translating ({type(exc).__name__}). Please try again.")
            finally:
                recorder.unregister(job.id)
                self.db.save_version(job.id, job.version, job.result if job.status == "done" else None,
                                     job.events, decisions)
                self.db.set_status(job.id, job.status, job.error)
                for q in list(job.listeners):
                    q.put_nowait(None)

    @staticmethod
    def _fail(job: Job, message: str) -> None:
        job.status = "error"
        job.error = message
        job.publish({"type": "error", "message": message})

    async def cleanup_loop(self) -> None:
        while True:
            for job_id in self.db.expired(time.time() - self.s.job_ttl_hours * 3600):
                if job_id not in self.jobs or self.jobs[job_id].status in ("done", "error"):
                    self.delete(job_id)
            await asyncio.sleep(600)
