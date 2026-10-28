"""MCP server: tools through the SDK's own client, in-process and over real Streamable HTTP (token, Origin,
progress on the SSE response), agent uploads, and the URL fetcher's SSRF guards."""

import asyncio
import base64
import contextlib
import http.server
import socket
import threading
import time
import uuid


import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app import intake
from app.config import Settings
from app.main import app
from app.mcp_server import server, svc

TOKEN = "test-mcp-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def bound():
    with TestClient(app) as client:  # the lifespan binds the tools to the job store
        yield client


def call(name, args=None, progress=None):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args or {}, progress_callback=progress)
    return asyncio.run(go())


def queued_job(samples):
    """A job that stays queued (immediate mode would run translate_pdf right away)."""
    jid = uuid.uuid4().hex
    d = svc.jobs.root / jid
    d.mkdir(parents=True)
    (d / "source.pdf").write_bytes((samples / "01-simple-en.pdf").read_bytes())
    svc.jobs.db.create_job(jid, "01-simple-en.pdf", "de", "fast")
    return jid


def feed(jid, delay=0.4):
    """Write progress events like a worker would (its own connection: readers watch PRAGMA data_version, which
    only changes for other connections' commits), then end the job."""
    from app.store import Store

    def run():
        db = Store(svc.settings.data_dir / "translator.sqlite3")
        for pct, msg in ((20, "Translating page 1 of 2"), (60, "Translating page 2 of 2"), (90, "Rebuilding the PDF")):
            time.sleep(delay)
            db.add_event(jid, 1, 1, {"type": "stage", "percent": pct, "message": msg})
        time.sleep(delay)
        db.cancel(jid, "stopped by the test")
    threading.Thread(target=run, daemon=True).start()


def test_tools_are_listed(bound):
    async def go():
        async with Client(server) as c:
            return {t.name: t for t in (await c.list_tools()).tools}
    tools = asyncio.run(go())
    assert {"translate_pdf", "wait_for_translation", "get_translation", "cancel_translation",
            "list_languages", "list_samples"} <= set(tools)
    props = tools["translate_pdf"].input_schema["properties"]
    assert {"target_language", "pdf_url", "upload_id", "pdf_base64", "sample", "mode"} <= set(props)


def test_translate_wait_and_get(bound, samples):
    r = call("translate_pdf", {"target_language": "es", "sample": "08-jargon-en.pdf"})
    assert not r.is_error, r.content[0].text
    jid = r.structured_content["job_id"]
    done = call("wait_for_translation", {"job_id": jid, "max_seconds": 30})
    s = done.structured_content
    assert s["status"] == "done" and s["score"] > 80 and s["download_url"].endswith(f"/api/jobs/{jid}/download")
    links = [c for c in done.content if c.type == "resource_link"]
    assert links and links[0].mime_type == "application/pdf"
    got = call("get_translation", {"job_id": jid}).structured_content
    assert got["score"] == s["score"] and "issues_to_review" in got


def test_pdf_base64_and_bad_input(bound, samples):
    pdf = base64.b64encode((samples / "01-simple-en.pdf").read_bytes()).decode()
    r = call("translate_pdf", {"target_language": "de", "pdf_base64": pdf, "filename": "mine.pdf"})
    assert not r.is_error and r.structured_content["filename"] == "mine.pdf"
    for args, msg in [({"target_language": "de"}, "exactly one"),
                      ({"target_language": "de", "sample": "x.pdf", "pdf_url": "http://a"}, "exactly one"),
                      ({"target_language": "xx", "sample": "01-simple-en.pdf"}, "Unsupported"),
                      ({"target_language": "de", "pdf_base64": "%%%"}, "base64"),
                      ({"target_language": "de", "pdf_base64": base64.b64encode(b"hello").decode()}, "PDF"),
                      ({"target_language": "de", "upload_id": "0" * 32}, "upload_id")]:
        r = call("translate_pdf", args)
        assert r.is_error and msg in r.content[0].text, (args, r.content[0].text)
    assert call("get_translation", {"job_id": "nope"}).is_error


def test_wait_streams_progress_and_times_out(bound, samples):
    jid = queued_job(samples)
    feed(jid)
    seen = []

    async def progress(p, total, message):
        seen.append((p, total, message))
    r = call("wait_for_translation", {"job_id": jid, "max_seconds": 30}, progress)
    assert [p for p, *_ in seen] == [20, 60, 90] and seen[0][1] == 100 and "page 1" in seen[0][2]
    assert r.structured_content["status"] == "error"  # the test ended it with a cancel
    idle = queued_job(samples)
    t0 = time.time()
    r = call("wait_for_translation", {"job_id": idle, "max_seconds": 5})
    assert r.structured_content["status"] == "queued" and 4 < time.time() - t0 < 15
    assert "wait_for_translation" in r.content[0].text  # tells the agent to call again
    assert call("cancel_translation", {"job_id": idle}).structured_content["status"] == "error"


# ------------------------------------------------------------------ over HTTP

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_server():
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(5)


def http_call(base, name, args, progress=None):
    async def go():
        async with httpx2.AsyncClient(headers=AUTH, timeout=60) as hc:
            async with Client(streamable_http_client(f"{base}/mcp", http_client=hc)) as c:
                return await c.call_tool(name, args, progress_callback=progress)
    return asyncio.run(go())


def test_http_requires_token_and_checks_origin(http_server):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    hdr = {"Accept": "application/json, text/event-stream"}
    r = httpx2.post(f"{http_server}/mcp", json=body, headers=hdr)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    r = httpx2.post(f"{http_server}/mcp", json=body, headers={**hdr, "Authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = httpx2.post(f"{http_server}/mcp", json=body, headers={**hdr, **AUTH, "Origin": "https://evil.example"})
    assert r.status_code == 403
    r = http_call(http_server, "list_languages", {})
    assert not r.is_error and any(lang["code"] == "de" for lang in r.structured_content["languages"])


def test_http_progress_arrives_over_sse(http_server, samples):
    jid = queued_job(samples)
    feed(jid, delay=0.5)
    seen = []

    async def progress(p, total, message):
        seen.append((p, round(time.time(), 1)))
    r = http_call(http_server, "wait_for_translation", {"job_id": jid, "max_seconds": 30}, progress)
    assert [p for p, _ in seen] == [20, 60, 90]
    assert seen[-1][1] - seen[0][1] >= 0.7  # streamed as they happened, not bundled with the result
    assert r.structured_content["status"] == "error"


def test_upload_then_translate(http_server, samples):
    pdf = (samples / "10-cv-like-en.pdf").read_bytes()
    assert httpx2.post(f"{http_server}/api/uploads", files={"file": ("cv.pdf", pdf)}).status_code == 401
    up = httpx2.post(f"{http_server}/api/uploads", files={"file": ("cv.pdf", pdf)}, headers=AUTH)
    assert up.status_code == 201 and up.json()["pages"] == 1
    bad = httpx2.post(f"{http_server}/api/uploads", files={"file": ("x.pdf", b"not a pdf")}, headers=AUTH)
    assert bad.status_code == 422
    r = http_call(http_server, "translate_pdf", {"target_language": "de", "upload_id": up.json()["upload_id"]})
    assert not r.is_error and r.structured_content["filename"] == "cv.pdf"
    again = http_call(http_server, "translate_pdf", {"target_language": "de", "upload_id": up.json()["upload_id"]})
    assert again.is_error  # an upload is consumed by the job it created


# ------------------------------------------------------------------ fetching URLs

@pytest.mark.parametrize("url", ["http://127.0.0.1:1/x.pdf", "http://localhost/x.pdf", "http://169.254.169.254/latest",
                                 "http://[::1]/x.pdf", "http://10.0.0.5/x.pdf", "file:///etc/passwd",
                                 "ftp://example.com/x.pdf", "http://user:pw@example.com/x.pdf"])
def test_fetch_refuses_private_and_odd_urls(url):
    with pytest.raises(intake.InvalidDocument):
        asyncio.run(intake.fetch_pdf(url, Settings()))


@contextlib.contextmanager
def file_server(routes):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, body = routes.get(self.path, (404, {}, b""))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def test_fetch_pdf_follows_checked_redirects_and_validates(samples, monkeypatch):
    pdf = (samples / "01-simple-en.pdf").read_bytes()
    with file_server({"/doc.pdf": (200, {}, pdf), "/moved": (302, {"Location": "/doc.pdf"}, b""),
                      "/meta": (302, {"Location": "http://169.254.169.254/"}, b""),
                      "/html": (200, {}, b"<html>hi</html>"), "/big.pdf": (200, {}, b"%PDF" + b"0" * 1_200_000)}) as base:
        allowed = {"127.0.0.1"}
        real = intake._public_host

        def only_local_test_server(host, port):  # the test server is local; everything else stays checked
            if host not in allowed:
                real(host, port)
        monkeypatch.setattr(intake, "_public_host", only_local_test_server)
        name, data = asyncio.run(intake.fetch_pdf(f"{base}/moved", Settings()))
        assert name == "doc.pdf" and data == pdf
        with pytest.raises(intake.InvalidDocument, match="private"):
            asyncio.run(intake.fetch_pdf(f"{base}/meta", Settings()))  # redirect target re-checked
        with pytest.raises(intake.InvalidDocument, match="did not return a PDF"):
            asyncio.run(intake.fetch_pdf(f"{base}/html", Settings()))
        with pytest.raises(intake.InvalidDocument, match="too large"):
            asyncio.run(intake.fetch_pdf(f"{base}/big.pdf", Settings(max_upload_mb=1)))  # capped while streaming
