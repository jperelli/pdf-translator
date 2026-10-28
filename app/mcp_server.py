"""MCP server: agents translate PDFs through the same Huey queue as the web app.

Mounted at /mcp (Streamable HTTP, protocol 2026-07-28, stateless). A translation takes from seconds to
minutes, so the tools follow a job pattern that works in every MCP client:

    translate_pdf            enqueue, returns a job_id right away
    wait_for_translation     answers as an SSE stream of notifications/progress (one per pipeline step) and
                             returns when the job ends or after max_seconds; call it again to keep waiting
    get_translation          scores, problems to review, cost and download links
    cancel_translation       stop a queued or running job

The job keeps running if the agent disconnects; any later call with the job_id picks it up.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import mcp.types as t
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.config import PURPOSES, Settings
from app.formats import InvalidDocument
from app.intake import Uploads, fetch_pdf, safe_name, validate_pdf
from app.jobs import JobStore
from app.languages import LANGUAGES
from app.store import TERMINAL

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
JOB_HINT = "Call wait_for_translation with this job_id to follow it, or get_translation later."


class Service:
    """What the tools need from the web app (set in the app's lifespan)."""

    jobs: JobStore
    uploads: Uploads
    settings: Settings


svc = Service()

server = MCPServer(
    "pdf-translator",
    title="Stark Document Translator",
    version="1.0",
    instructions=(
        "Translates PDFs into another language keeping the layout, fonts and images. "
        "Start with translate_pdf (a public pdf_url, an upload_id from POST /api/uploads, a small pdf_base64, "
        "or a sample from list_samples), then call wait_for_translation with the job_id until status is "
        "'done' or 'error', then get_translation for the scores and the download link. "
        "'fast' is cheap; 'quality' checks styling thoroughly and fixes layout mismatches."
    ),
)


def _base_url(ctx: Context | None) -> str:
    if svc.settings.public_url:
        return svc.settings.public_url.rstrip("/")
    headers = {k.lower(): v for k, v in ((ctx.headers if ctx else None) or {}).items()}
    host = headers.get("x-forwarded-host") or headers.get("host")
    return f"{headers.get('x-forwarded-proto', 'http')}://{host}" if host else ""


def _job(job_id: str):
    job = svc.jobs.get(job_id) if isinstance(job_id, str) and len(job_id) == 32 else None
    if not job:
        raise ToolError(f"Unknown job_id {job_id!r} (jobs expire after {svc.settings.job_ttl_hours} h).")
    return job


def _result(data: dict[str, Any], text: str | None = None, links: list[t.ResourceLink] | None = None):
    return t.CallToolResult(content=[t.TextContent(type="text", text=text or json.dumps(data, ensure_ascii=False)),
                                     *(links or [])], structured_content=data)


def _status(job) -> dict[str, Any]:
    return {"job_id": job.id, "status": job.status, "percent": round(job.percent or 0, 1), "filename": job.filename,
            "target_language": job.target, "mode": job.purpose, "version": job.version, "error": job.error}


def _summary(job, ctx: Context | None) -> t.CallToolResult:
    data = _status(job)
    links: list[t.ResourceLink] = []
    if job.result:
        r, res = job.result["report"], job.result
        calls = res.get("calls", [])
        issues = [{"block": b["id"], "page": b["page"], "severity": i["severity"], "category": i["category"],
                   "message": i["message"]}
                  for b in res["blocks"] for i in b["issues"] if i["severity"] in ("major", "critical")]
        base = _base_url(ctx)
        data |= {
            "score": r["score"], "translation_score": r["translation_score"], "layout_score": r["layout_score"],
            "passed": r["passed"], "fail_reasons": r["fail_reasons"], "pages": len(r["pages"]),
            "blocks": r["blocks_total"], "overlapping_text": r["collisions"] + r["output_overlaps"],
            "font_fallbacks": r["missing_glyph_blocks"] + r["font_substituted_blocks"],
            "issues_to_review": issues[:20], "issues_to_review_total": len(issues),
            "review_items": [i["title"] for i in res.get("review", []) if i.get("options")],
            "cost_usd": round(sum(c["cost_usd"] for c in calls), 4),
            "tokens": sum(c["input_tokens"] + c["output_tokens"] for c in calls),
            "seconds": r["timings"].get("total"),
            "download_url": f"{base}/api/jobs/{job.id}/download",
            "report_url": f"{base}/api/jobs/{job.id}",
            "web_url": f"{base}/#job={job.id}",
        }
        stem = Path(job.filename).stem
        links = [
            t.ResourceLink(type="resource_link", uri=data["download_url"], name=f"{stem}.{job.target}.pdf",
                           title="Translated PDF", mime_type="application/pdf"),
            t.ResourceLink(type="resource_link", uri=data["web_url"], name="Review in the web app",
                           mime_type="text/html"),
        ]
        verdict = "passed" if r["passed"] else "needs review: " + "; ".join(r["fail_reasons"])
        text = (f"{job.filename} -> {job.target}: {job.status}. Score {r['score']:.1f} (translation "
                f"{r['translation_score']:.1f}, layout {r['layout_score']:.1f}), {verdict}. "
                f"{len(issues)} major/critical issue(s). Cost ${data['cost_usd']:.4f}. Download: {data['download_url']}")
        return _result(data, text, links)
    if job.status == "error":
        return _result(data, f"{job.filename} -> {job.target}: failed. {job.error}")
    return _result(data, f"{job.filename} -> {job.target}: {job.status}, {data['percent']:.0f}%. {JOB_HINT}")


@server.tool(annotations=t.ToolAnnotations(read_only_hint=True))
def list_languages() -> t.CallToolResult:
    """Target languages the translator supports (ISO 639-1 codes)."""
    langs = [{"code": lang.code, "name": lang.name, "native": lang.native, "rtl": lang.rtl}
             for lang in LANGUAGES.values()]
    return _result({"languages": langs}, ", ".join(f"{lang['code']} ({lang['name']})" for lang in langs))


@server.tool(annotations=t.ToolAnnotations(read_only_hint=True))
def list_samples() -> t.CallToolResult:
    """Built-in sample PDFs (use their name as `sample` in translate_pdf to try the service)."""
    manifest = json.loads((SAMPLES / "manifest.json").read_text())
    items = [{"name": k, "pages": v["pages"], "description": v["description"]}
             for k, v in manifest.items() if (SAMPLES / k).exists()]
    return _result({"samples": items}, "\n".join(f"{s['name']} ({s['pages']} p.): {s['description']}" for s in items))


@server.tool()
async def translate_pdf(
    target_language: Annotated[str, Field(description="ISO 639-1 code of the language to translate into, e.g. 'es', "
                                                      "'de', 'ar', 'zh' (see list_languages)")],
    pdf_url: Annotated[str | None, Field(description="Public http(s) URL of the PDF")] = None,
    upload_id: Annotated[str | None, Field(description="Id returned by POST /api/uploads (multipart field "
                                                       "'file', same bearer token as /mcp)")] = None,
    pdf_base64: Annotated[str | None, Field(description="The PDF itself, base64 encoded. Only for small files: "
                                                        "it travels inside the tool call")] = None,
    sample: Annotated[str | None, Field(description="Name of a built-in sample (see list_samples)")] = None,
    filename: Annotated[str | None, Field(description="File name to show for pdf_base64")] = None,
    mode: Annotated[Literal["fast", "quality"], Field(description="'fast': fast and cheap. 'quality': slower and "
                                                                  "more expensive, checks styling thoroughly and "
                                                                  "fixes layout mismatches")] = "fast",
) -> t.CallToolResult:
    """Start translating a PDF. Give exactly one of pdf_url, upload_id, pdf_base64 or sample.

    Returns a job_id immediately; translating takes from seconds to several minutes. Then call
    wait_for_translation (progress while you wait) and get_translation (scores and download link)."""
    if target_language not in LANGUAGES:
        raise ToolError(f"Unsupported target_language {target_language!r}; see list_languages.")
    if mode not in PURPOSES:
        raise ToolError("mode must be 'fast' or 'quality'.")
    given = [k for k, v in {"pdf_url": pdf_url, "upload_id": upload_id, "pdf_base64": pdf_base64,
                            "sample": sample}.items() if v]
    if len(given) != 1:
        raise ToolError("Give exactly one of pdf_url, upload_id, pdf_base64 or sample.")
    try:
        if pdf_url:
            name, data = await fetch_pdf(pdf_url, svc.settings)
        elif upload_id:
            name, data = svc.uploads.take(upload_id)
        elif pdf_base64:
            try:
                data = base64.b64decode(pdf_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise InvalidDocument("pdf_base64 is not valid base64.") from exc
            name = safe_name(filename or "document.pdf")
        else:
            path = SAMPLES / Path(sample).name
            if not path.is_file() or path.suffix != ".pdf":
                raise InvalidDocument(f"Unknown sample {sample!r}; see list_samples.")
            name, data = path.name, path.read_bytes()
        validate_pdf(name, data, svc.settings)
    except InvalidDocument as exc:
        raise ToolError(str(exc)) from exc
    job = await svc.jobs.create(name, target_language, data, mode)
    data_out = _status(job)
    return _result(data_out, f"Queued {name} -> {target_language} ({mode}) as job {job.id}. {JOB_HINT}")


@server.tool(annotations=t.ToolAnnotations(read_only_hint=True))
async def wait_for_translation(
    job_id: Annotated[str, Field(description="The job_id returned by translate_pdf")],
    ctx: Context,
    max_seconds: Annotated[int, Field(description="Return after this long even if not finished (5-600); "
                                                  "call again to keep waiting", ge=5, le=600)] = 240,
) -> t.CallToolResult:
    """Wait for a translation, streaming progress (step, page, percent) as MCP progress notifications.

    Returns the final result when the job ends, or its current status after max_seconds (the job keeps
    running: call this again with the same job_id)."""
    job = _job(job_id)
    deadline = time.monotonic() + max_seconds
    last = -1.0
    if job.status not in TERMINAL:
        stream = svc.jobs.stream(job_id, keepalive=1.0)
        try:
            async for event in stream:
                if event and event.get("percent") is not None and event["percent"] >= last:
                    last = event["percent"]
                    await ctx.report_progress(round(last, 1), 100, _describe(event))
                if time.monotonic() >= deadline:
                    break
        finally:
            await stream.aclose()
    return _summary(_job(job_id), ctx)


def _describe(event: dict[str, Any]) -> str:
    if event.get("message"):
        return event["message"]
    if event.get("type") in ("page", "page_step") and event.get("page"):
        return f"Page {event['page']}: {event.get('step') or 'translated'}"
    return str(event.get("type", "working"))


@server.tool(annotations=t.ToolAnnotations(read_only_hint=True))
def get_translation(
    job_id: Annotated[str, Field(description="The job_id returned by translate_pdf")],
    ctx: Context,
) -> t.CallToolResult:
    """Status of a translation and, once done: score (the lower of translation quality and layout fidelity),
    major/critical issues to review, cost, and links to download the PDF and review it in the web app."""
    return _summary(_job(job_id), ctx)


@server.tool(annotations=t.ToolAnnotations(destructive_hint=True, idempotent_hint=True))
def cancel_translation(
    job_id: Annotated[str, Field(description="The job_id returned by translate_pdf")],
) -> t.CallToolResult:
    """Cancel a queued or running translation (a running one stops at its next step)."""
    job = _job(job_id)
    if job.status in TERMINAL:
        return _summary(job, None)
    try:
        svc.jobs.db.cancel(job_id, "Canceled through MCP.")
    except ValueError as exc:  # finished in the meantime
        raise ToolError(str(exc)) from exc
    return _summary(_job(job_id), None)


def bind(jobs: JobStore, uploads: Uploads, settings: Settings) -> None:
    svc.jobs, svc.uploads, svc.settings = jobs, uploads, settings


__all__ = ["server", "bind", "svc"]
