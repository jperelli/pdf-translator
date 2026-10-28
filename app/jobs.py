"""In-process job store: each upload becomes a background asyncio task streaming events to SSE clients."""

from __future__ import annotations

import asyncio
import json
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

log = logging.getLogger(__name__)
Status = Literal["queued", "running", "done", "error"]


@dataclass
class Job:
    id: str
    filename: str
    target: str
    dir: Path
    status: Status = "queued"
    created: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    listeners: set[asyncio.Queue] = field(default_factory=set)

    @property
    def source(self) -> Path:
        return self.dir / "source.pdf"

    @property
    def output(self) -> Path:
        return self.dir / "translated.pdf"

    def publish(self, event: dict[str, Any]) -> None:
        event = {"t": round(time.time() - self.created, 2), **event}
        self.events.append(event)
        for q in list(self.listeners):
            q.put_nowait(event)

    def summary(self) -> dict[str, Any]:
        last = next((e for e in reversed(self.events) if "percent" in e), None)
        return {"id": self.id, "filename": self.filename, "target": self.target, "status": self.status,
                "created": self.created, "percent": last["percent"] if last else 0, "error": self.error}


class JobStore:
    def __init__(self, settings: Settings):
        self.s = settings
        self.jobs: dict[str, Job] = {}
        self.root = settings.data_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.running = asyncio.Semaphore(2)  # documents translated at the same time

    def create(self, filename: str, target: str, data: bytes) -> Job:
        job_id = uuid.uuid4().hex
        d = self.root / job_id
        d.mkdir(parents=True)
        job = Job(id=job_id, filename=filename, target=target, dir=d)
        job.source.write_bytes(data)
        self.jobs[job_id] = job
        job.publish({"type": "stage", "stage": "queued", "message": "Waiting for a free translator", "percent": 0})
        asyncio.create_task(self._run(job))
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    async def _run(self, job: Job) -> None:
        async with self.running:
            job.status = "running"
            recorder.register(job.id, job.publish)
            try:
                pipeline = TranslationPipeline(self.s, job.publish, job_id=job.id)
                job.result = await pipeline.run(job.source, job.output, job.target, job.filename)
                (job.dir / "result.json").write_text(json.dumps(job.result, ensure_ascii=False))
                job.status = "done"
                job.publish({"type": "done", "report": job.result["report"]})
            except (InvalidDocument, PipelineError, ValueError) as exc:
                self._fail(job, str(exc))
            except Exception as exc:  # never leak a stack trace to the user, keep it in the logs
                log.exception("job %s failed", job.id)
                self._fail(job, f"Unexpected error while translating ({type(exc).__name__}). Please try again.")
            finally:
                recorder.unregister(job.id)
                for q in list(job.listeners):
                    q.put_nowait(None)

    @staticmethod
    def _fail(job: Job, message: str) -> None:
        job.status = "error"
        job.error = message
        job.publish({"type": "error", "message": message})

    async def cleanup_loop(self) -> None:
        while True:
            cutoff = time.time() - self.s.job_ttl_hours * 3600
            for job_id, job in list(self.jobs.items()):
                if job.created < cutoff and job.status in ("done", "error"):
                    shutil.rmtree(job.dir, ignore_errors=True)
                    self.jobs.pop(job_id, None)
            await asyncio.sleep(600)
