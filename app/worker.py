"""Queue workers: each translation (and each redo) is a Huey task backed by SQLite.

Run the consumer next to the web app (scripts/start.sh does both):

    huey_consumer app.worker.huey -k process -w 2

Progress is not kept in Huey: every pipeline event goes to the `events` table, which the web app streams to
the browser. A task is a plain function that runs the async pipeline in its own event loop (asyncio.run).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from huey import SqliteHuey, crontab

from app.ai.setup import configure_sdk
from app.config import get_settings
from app.formats import InvalidDocument
from app.observability import recorder
from app.pipeline import PipelineError, TranslationPipeline
from app.store import Store

log = logging.getLogger(__name__)
settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
huey = SqliteHuey("translator", filename=str(settings.data_dir / "queue.sqlite3"), immediate=settings.queue_immediate,
                  results=False)


@lru_cache
def store() -> Store:
    """One connection per process (workers are separate processes)."""
    return Store(settings.data_dir / "translator.sqlite3")


def job_dir(job_id: str) -> Path:
    return settings.data_dir / "jobs" / job_id


def output_path(job_id: str, version: int) -> Path:
    return job_dir(job_id) / f"translated.v{version}.pdf"


@huey.task()
def translate_job(job_id: str, version: int, attempt: int) -> None:
    run_job(job_id, version, attempt)


@huey.periodic_task(crontab(minute="*"))
def recover_lost_jobs() -> None:
    recover(time.time())


def worker_token(pid: int | None = None) -> str:
    """host:pid:start-time. The start time (from /proc) tells a live worker from a new process reusing its pid."""
    pid = pid or os.getpid()
    return f"{socket.gethostname()}:{pid}:{_start_time(pid)}"


def _start_time(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]  # field 22, after the "(comm)" that may contain spaces
    except (OSError, IndexError):
        return ""


def worker_alive(token: str | None) -> bool | None:
    """True/False when the worker ran on this host; None when we cannot tell (another host, no /proc)."""
    try:
        host, pid, start = (token or "").split(":")
    except ValueError:
        return None
    if host != socket.gethostname() or not start:
        return None
    return _start_time(int(pid)) == start


def lost(job: dict[str, Any], now: float) -> bool:
    """A long MuPDF call can hold the GIL for most of a minute, so the heartbeat thread is only a fallback:
    on the same host we ask the OS whether the worker process still exists."""
    alive = worker_alive(job.get("worker"))
    if alive is not None:
        return not alive
    return (job.get("heartbeat") or 0) < now - settings.stale_after_s


def recover(now: float) -> list[str]:
    """Jobs whose worker died (container restart, OOM, crash) go back to the queue once, then fail."""
    db, out = store(), []
    for job in (j for j in db.running() if lost(j, now)):
        if job["attempt"] < settings.max_attempts:
            attempt = db.requeue(job["id"], job["attempt"])
            if attempt is not None:
                log.warning("job %s lost its worker, retrying (attempt %s)", job["id"], attempt)
                db.add_event(job["id"], job["version"], attempt, {
                    "type": "warning", "t": 0, "v": job["version"],
                    "message": "The worker processing this job stopped; starting it again."})
                translate_job(job["id"], job["version"], attempt)
                out.append(job["id"])
        else:
            db.fail(job["id"], job["attempt"], "The worker processing this job stopped twice. Please try again.")
    return out


def run_job(job_id: str, version: int, attempt: int) -> None:
    db = store()
    if not db.start(job_id, version, attempt, worker_token()):
        log.info("job %s v%s attempt %s superseded, skipping", job_id, version, attempt)
        return
    row = db.job(job_id)
    started = time.time()

    def emit(event: dict[str, Any]) -> None:
        db.add_event(job_id, version, attempt, {"t": round(time.time() - started, 2), "v": version, **event})

    stop = threading.Event()

    def beat() -> None:  # proves the worker is alive even during a long render without events
        while not stop.wait(settings.worker_heartbeat_s):
            db.heartbeat(job_id, attempt)

    threading.Thread(target=beat, daemon=True, name=f"heartbeat-{job_id[:8]}").start()
    configure_sdk(settings)
    recorder.register(job_id, emit)
    result, error = None, None
    try:
        result = asyncio.run(_execute(row, version, emit))
    except (InvalidDocument, PipelineError, ValueError) as exc:
        error = str(exc)
    except Exception as exc:  # never leak a stack trace to the user, keep it in the logs
        log.exception("job %s failed", job_id)
        error = f"Unexpected error while translating ({type(exc).__name__}). Please try again."
    finally:
        stop.set()
        recorder.unregister(job_id)
    final = {"type": "done", "report": result["report"]} if result else {"type": "error", "message": error}
    final = {"t": round(time.time() - started, 2), "v": version, **final}
    db.finish(job_id, version, attempt, result, error, final)


async def _execute(row: dict[str, Any], version: int, emit) -> dict[str, Any]:
    if settings.backend == "openai":
        # an async client belongs to the event loop that created it: one per task, never shared across loops
        from agents import set_default_openai_client
        from openai import AsyncOpenAI

        set_default_openai_client(AsyncOpenAI(api_key=settings.openai_api_key), use_for_tracing=False)
    job_id = row["id"]
    pipeline = TranslationPipeline(settings, emit, job_id=job_id, purpose=row["purpose"], version=version)
    src, out = job_dir(job_id) / "source.pdf", output_path(job_id, version)
    if version == 1:
        return await pipeline.run(src, out, row["target"], row["filename"])
    db = store()
    current = db.version(job_id, version)
    previous = db.version(job_id, version - 1, with_result=True) or db.version(job_id, with_result=True)
    if not previous or not previous["result"]:
        raise PipelineError("There is no finished version to redo.")
    return await pipeline.redo(src, out, row["target"], row["filename"], previous["result"],
                               (current or {}).get("decisions") or {})
