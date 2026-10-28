"""Getting a PDF into the service: validation shared by the web form and MCP, uploads for agents, and fetching
a PDF from a URL without letting the server be used to reach internal addresses (SSRF)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx2 as httpx  # the HTTP client the openai and mcp packages already ship with
import pymupdf

from app.config import Settings
from app.formats import InvalidDocument, detect

UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
UPLOAD_TTL_S = 3600
MAX_REDIRECTS = 3


def safe_name(name: str | None) -> str:
    stem = Path(name or "document.pdf").name
    return re.sub(r"[^\w.\- ]", "_", stem)[:120] or "document.pdf"


def validate_pdf(name: str, data: bytes, settings: Settings) -> int:
    """Fail fast on what the worker could not translate. Returns the page count."""
    if not data:
        raise InvalidDocument("The file is empty.")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise InvalidDocument(f"File too large: the limit is {settings.max_upload_mb} MB.")
    detect(name, data[:2048])
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise InvalidDocument("The PDF is password protected. Remove the password and upload it again.")
            if doc.page_count > settings.max_pages:
                raise InvalidDocument(f"The PDF has {doc.page_count} pages; the limit is {settings.max_pages}.")
            return doc.page_count
    except InvalidDocument:
        raise
    except Exception as exc:
        raise InvalidDocument("The file is not a readable PDF (it may be corrupted or truncated).") from exc


# ------------------------------------------------------------------ uploads (for agents)

class Uploads:
    """Files uploaded with POST /api/uploads, waiting to be translated by an MCP `translate_pdf(upload_id)`."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.root = settings.data_dir / "uploads"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, data: bytes) -> dict:
        pages = validate_pdf(name, data, self.s)
        upload_id = uuid.uuid4().hex
        (self.root / f"{upload_id}.pdf").write_bytes(data)
        (self.root / f"{upload_id}.json").write_text(json.dumps({"filename": name, "pages": pages}))
        return {"upload_id": upload_id, "filename": name, "pages": pages, "expires_in_s": UPLOAD_TTL_S}

    def take(self, upload_id: str) -> tuple[str, bytes]:
        """The upload's file name and bytes; the upload is consumed."""
        pdf, meta = self.root / f"{upload_id}.pdf", self.root / f"{upload_id}.json"
        if not UPLOAD_ID.match(upload_id or "") or not pdf.exists():
            raise InvalidDocument("Unknown or expired upload_id (uploads are kept for an hour).")
        name, data = json.loads(meta.read_text())["filename"], pdf.read_bytes()
        pdf.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        return name, data

    def cleanup(self) -> None:
        cutoff = time.time() - UPLOAD_TTL_S
        for f in self.root.iterdir():
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)


# ------------------------------------------------------------------ fetching from a URL

def _public_host(host: str, port: int) -> None:
    """Every address the host resolves to must be public: no loopback, private, link-local (cloud metadata),
    multicast or reserved ranges, so a URL cannot reach the server's own network."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise InvalidDocument(f"Cannot resolve {host}.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise InvalidDocument("The URL points to a private or local address; only public URLs are fetched.")


def _check_url(url: str) -> tuple[str, int]:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise InvalidDocument("pdf_url must be an http(s) URL.")
    if u.username or u.password:
        raise InvalidDocument("pdf_url must not contain credentials.")
    return u.hostname, u.port or (443 if u.scheme == "https" else 80)


async def fetch_pdf(url: str, settings: Settings) -> tuple[str, bytes]:
    """Download a PDF from a public URL: redirects re-checked, size capped while streaming, %PDF required."""
    limit = settings.max_upload_mb * 1024 * 1024
    async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(20.0)) as client:
        for _ in range(MAX_REDIRECTS + 1):
            host, port = _check_url(url)
            await asyncio.to_thread(_public_host, host, port)
            async with client.stream("GET", url, headers={"User-Agent": "pdf-translator/1.0"}) as resp:
                if resp.is_redirect and "location" in resp.headers:
                    url = urljoin(url, resp.headers["location"])
                    continue
                if resp.status_code != 200:
                    raise InvalidDocument(f"Downloading the PDF failed: HTTP {resp.status_code}.")
                if int(resp.headers.get("content-length") or 0) > limit:
                    raise InvalidDocument(f"File too large: the limit is {settings.max_upload_mb} MB.")
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise InvalidDocument(f"File too large: the limit is {settings.max_upload_mb} MB.")
                    chunks.append(chunk)
                data = b"".join(chunks)
                if not data.startswith(b"%PDF"):
                    raise InvalidDocument("The URL did not return a PDF.")
                name = safe_name(Path(urlparse(url).path).name or "document.pdf")
                return (name if name.lower().endswith(".pdf") else name + ".pdf"), data
    raise InvalidDocument("Too many redirects.")
