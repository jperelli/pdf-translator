"""Regenerate the landing page showcase: two pages of the Stark VARG manual translated with the real models.

    python scripts/landing_showcase.py            # needs OPENAI_API_KEY; about $0.10 for the four languages

Writes app/static/landing/p{1,2}-{en,de,es,ar,zh}.webp and prints the scores to copy into
app/static/landing.js (RUNS).
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import set_default_openai_client  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from app.ai.setup import configure_sdk  # noqa: E402
from app.config import Settings  # noqa: E402
from app.pipeline import TranslationPipeline  # noqa: E402

PAGES = (3, 8)  # 0-based: "The Ambition" and the table of contents
LANGS = ("de", "es", "ar", "zh")
OUT = ROOT / "app" / "static" / "landing"


def webp(pdf: Path, lang: str) -> None:
    for i, page in enumerate(pymupdf.open(pdf)):
        img = Image.open(io.BytesIO(page.get_pixmap(dpi=40).tobytes("png"))).convert("RGB")
        img.save(OUT / f"p{i + 1}-{lang}.webp", "WEBP", quality=80, method=6)


def main() -> None:
    settings = Settings()
    if settings.backend != "openai":
        sys.exit("Set OPENAI_API_KEY: the showcase must come from the real models.")
    configure_sdk(settings)
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    src = pymupdf.open(ROOT / "samples" / "09-stark-varg-manual-20p.pdf")
    excerpt = pymupdf.open()
    for p in PAGES:
        excerpt.insert_pdf(src, from_page=p, to_page=p)
    excerpt.save(tmp / "en.pdf", garbage=4, deflate=True)
    webp(tmp / "en.pdf", "en")
    for lang in LANGS:
        set_default_openai_client(AsyncOpenAI(api_key=settings.openai_api_key), use_for_tracing=False)
        res = asyncio.run(TranslationPipeline(settings, lambda e: None, purpose="quality", job_id=f"landing-{lang}")
                          .run(tmp / "en.pdf", tmp / f"{lang}.pdf", lang, "showcase.pdf"))
        webp(tmp / f"{lang}.pdf", lang)
        r = res["report"]
        print(lang, json.dumps({"score": r["score"], "translation": r["translation_score"], "layout": r["layout_score"],
                                "overlaps": r["collisions"] + r["output_overlaps"],
                                "cost": round(sum(c["cost_usd"] for c in res["calls"]), 4),
                                "seconds": round(r["timings"]["total"], 1)}))


if __name__ == "__main__":
    main()
