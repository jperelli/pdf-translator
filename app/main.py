from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.ai.setup import configure_sdk
from app.config import PURPOSES, get_settings
from app.formats import InvalidDocument, detect
from app.jobs import JobStore
from app.languages import LANGUAGES
from app.models import BlockResult
from app.review import estimate_tokens

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()
ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT.parent / "samples"
JOB_ID = re.compile(r"^[0-9a-f]{32}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_sdk(settings)
    app.state.jobs = JobStore(settings)
    task = asyncio.create_task(app.state.jobs.cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="Stark Document Translator", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; frame-src 'self' blob:; object-src 'self' blob:")
    return response


def _job(request: Request, job_id: str):
    job = request.app.state.jobs.get(job_id) if JOB_ID.match(job_id) else None
    if not job:
        raise HTTPException(404, "Job not found (jobs expire after a while).")
    return job


def _safe_name(name: str | None) -> str:
    stem = Path(name or "document.pdf").name
    return re.sub(r"[^\w.\- ]", "_", stem)[:120] or "document.pdf"


@app.get("/api/health")
def health():
    from app.worker import huey

    return {"status": "ok", "queue": {"pending": huey.pending_count()}}


@app.get("/api/config")
def config():
    s = settings
    return {
        "backend": s.backend,
        "models": {"analyst": s.analyst_model, "translator": s.translator_model, "judge": s.judge_model,
                   "escalation": s.escalation_model} if s.backend == "openai" else {},
        "languages": [{"code": lang.code, "name": lang.name, "native": lang.native, "rtl": lang.rtl}
                      for lang in LANGUAGES.values()],
        "limits": {"max_upload_mb": s.max_upload_mb, "max_pages": s.max_pages},
        "quality": {"threshold": s.quality_threshold, "max_refine_iterations": s.max_refine_iterations},
    }


@app.get("/api/samples")
def samples():
    manifest = json.loads((SAMPLES / "manifest.json").read_text())
    return [{"name": k, **v} for k, v in manifest.items() if (SAMPLES / k).exists()]


@app.post("/api/jobs", status_code=202)
async def create_job(request: Request, target: str = Form(...), file: UploadFile | None = File(None),
                     sample: str | None = Form(None), purpose: str = Form("fast")):
    if target not in LANGUAGES:
        raise HTTPException(422, f"Unsupported target language: {target}")
    if purpose not in PURPOSES:
        raise HTTPException(422, "purpose must be 'fast' or 'quality'.")
    limit = settings.max_upload_mb * 1024 * 1024
    if sample:
        path = SAMPLES / Path(sample).name
        if not path.is_file() or path.suffix != ".pdf":
            raise HTTPException(404, "Unknown sample.")
        data, name = path.read_bytes(), path.name
    elif file is not None:
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"File too large: the limit is {settings.max_upload_mb} MB.")
        if not data:
            raise HTTPException(422, "The file is empty.")
        name = _safe_name(file.filename)
    else:
        raise HTTPException(422, "Upload a file or pick a sample.")
    try:
        detect(name, data[:2048])
        # fail fast on corrupted / encrypted files instead of after queueing
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise InvalidDocument("The PDF is password protected. Remove the password and upload it again.")
            if doc.page_count > settings.max_pages:
                raise InvalidDocument(f"The PDF has {doc.page_count} pages; the limit is {settings.max_pages}.")
    except InvalidDocument as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, "The file is not a readable PDF (it may be corrupted or truncated).") from exc
    job = await request.app.state.jobs.create(name, target, data, purpose)
    return job.summary()


@app.get("/api/jobs")
def list_jobs(request: Request, limit: int = 50, offset: int = 0):
    rows, total = request.app.state.jobs.db.list_jobs(max(1, min(limit, 200)), max(0, offset))
    return {"jobs": rows, "total": total}


@app.get("/api/jobs/{job_id}")
def get_job(request: Request, job_id: str, version: int | None = None):
    job = _job(request, job_id)
    jobs = request.app.state.jobs
    if version is None:  # the newest finished version; while a redo runs (or after it failed) the previous one
        version = job.result_version if job.result and job.status != "done" else job.version
    v = jobs.db.version(job_id, version)
    if not v:
        raise HTTPException(404, "No such version.")
    return {**job.summary(), "result": v["result"], "versions": jobs.db.list_versions(job_id),
            "events": v["events"], "shown_version": version}


@app.delete("/api/jobs/{job_id}")
def delete_job(request: Request, job_id: str):
    job = _job(request, job_id)
    if job.status in ("queued", "running"):
        raise HTTPException(409, "The job is still running.")
    request.app.state.jobs.delete(job_id)
    return {"deleted": job_id}


@app.post("/api/jobs/{job_id}/pin")
def pin_job(request: Request, job_id: str, pinned: bool = True):
    _job(request, job_id)
    request.app.state.jobs.db.pin(job_id, pinned)
    return {"pinned": pinned}


@app.post("/api/jobs/{job_id}/estimate")
async def estimate(request: Request, job_id: str):
    job = _job(request, job_id)
    if not job.result:
        raise HTTPException(409, "Nothing to redo yet.")
    decisions = await request.json()
    blocks = {b["id"]: BlockResult.model_validate(b) for b in job.result["blocks"]}
    tokens = estimate_tokens(decisions, blocks)
    model = settings.translator_model
    pin, pout = settings.price(model)
    return {"tokens": tokens, "cost_usd": round(tokens * 0.6 * pin / 1e6 + tokens * 0.4 * pout / 1e6, 4),
            "model": model if settings.backend == "openai" else "pseudo"}


@app.post("/api/jobs/{job_id}/redo", status_code=202)
async def redo(request: Request, job_id: str):
    job = _job(request, job_id)
    decisions = await request.json()
    if not isinstance(decisions, dict):
        raise HTTPException(422, "Decisions must be a JSON object.")
    try:
        job = await request.app.state.jobs.redo(job, decisions)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return job.summary()


@app.get("/api/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str):
    _job(request, job_id)

    async def stream():
        async for event in request.app.state.jobs.stream(job_id):
            yield ": keep-alive\n\n" if event is None else f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _output(job, version: int | None):
    v = version or job.version
    path = job.output(v)
    if not path.exists() and version is None:  # the latest version failed: fall back to the newest file
        for older in range(v - 1, 0, -1):
            if job.output(older).exists():
                return job.output(older), older
    return path, v


@app.get("/api/jobs/{job_id}/download")
def download(request: Request, job_id: str, inline: bool = False, version: int | None = None):
    job = _job(request, job_id)
    path, v = _output(job, version)
    if not path.exists():
        raise HTTPException(409, "The translation is not ready yet.")
    stem = Path(job.filename).stem
    suffix = f".v{v}" if v > 1 else ""
    return FileResponse(path, media_type="application/pdf", filename=f"{stem}.{job.target}{suffix}.pdf",
                        content_disposition_type="inline" if inline else "attachment")


@app.get("/api/jobs/{job_id}/pages/{page}.png")
def page_image(request: Request, job_id: str, page: int, which: str = "original", version: int | None = None,
               dpi: int = 80):
    job = _job(request, job_id)
    path = _output(job, version)[0] if which == "translated" else job.source
    if not path.exists():
        raise HTTPException(409, "Not available yet.")
    with pymupdf.open(path) as doc:
        if not 1 <= page <= doc.page_count:
            raise HTTPException(404, "No such page.")
        png = doc[page - 1].get_pixmap(dpi=max(30, min(dpi, 150))).tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
