"""Find a complete font that looks like an original one (for blocks whose original font lacks glyphs).

Candidates come first from fonts bundled with the app (app/fonts, OFL licensed); only if none of them is a
good match the Google Fonts API is queried at runtime. Fonts are compared on metrics measured from their
outlines on the characters both fonts have: width, cap height, x-height and stroke density.
"""

from __future__ import annotations

import base64
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from fontTools.pens.areaPen import AreaPen

from app.formats.pdf.glyphs import RebuiltFont

log = logging.getLogger(__name__)
FONT_DIR = Path(__file__).resolve().parents[2] / "fonts"
BUNDLED: dict[str, list[int]] = {
    "Inter": [400, 700], "Roboto": [400, 700], "Open Sans": [400, 700], "Montserrat": [400, 700],
    "Archivo": [400, 700], "Poppins": [400, 700], "Noto Sans": [400, 700], "Noto Serif": [400, 700],
    "Roboto Mono": [400, 700], "Michroma": [400],
}
GOOGLE: dict[str, str] = {  # family -> generic class, queried only when no bundled font is close enough
    "Lato": "sans-serif", "Source Sans 3": "sans-serif", "Work Sans": "sans-serif", "Raleway": "sans-serif",
    "Nunito Sans": "sans-serif", "IBM Plex Sans": "sans-serif", "DM Sans": "sans-serif", "Manrope": "sans-serif",
    "Barlow": "sans-serif", "Rubik": "sans-serif", "Karla": "sans-serif", "Mulish": "sans-serif",
    "Exo 2": "sans-serif", "Oswald": "sans-serif", "Archivo Narrow": "sans-serif", "Sora": "sans-serif",
    "Merriweather": "serif", "Source Serif 4": "serif", "Lora": "serif", "PT Serif": "serif",
    "Libre Baskerville": "serif", "EB Garamond": "serif", "Crimson Pro": "serif",
    "IBM Plex Mono": "monospace", "Source Code Pro": "monospace", "JetBrains Mono": "monospace",
}
GENERIC = {"Noto Serif": "serif", "Roboto Mono": "monospace"}
SAMPLE = "HOxnaeiostrEAIRNSdl"
GOOD_ENOUGH = 0.12


def css_url(family: str, weights: list[int]) -> str:
    return "https://fonts.googleapis.com/css2?family=" + urllib.parse.quote(family) + ":wght@" + \
        ";".join(map(str, weights))


@dataclass
class Metrics:
    width: float
    cap: float | None
    xh: float | None
    density: float


def metrics(rf: RebuiltFont, chars: str) -> Metrics | None:
    widths, dens = [], []
    for ch in chars:
        g = rf._glyph_for(ch)
        if not g or not g.bounds:
            continue
        widths.append(g.width / rf.upm)
        x0, y0, x1, y1 = g.bounds
        ap = AreaPen()
        try:
            g.pen.replay(ap)
        except Exception:
            continue
        box = max(1.0, (x1 - x0) * (y1 - y0))
        dens.append(abs(ap.value) / box)
    if not widths:
        return None

    def height(ch: str) -> float | None:
        g = rf._glyph_for(ch)
        return (g.bounds[3] / rf.upm) if g and g.bounds else None

    return Metrics(sum(widths) / len(widths), height("H") or height("E") or height("I"),
                   height("x") or height("o") or height("n"), sum(dens) / max(1, len(dens)))


_QUICK: dict[tuple[str, str, str], Metrics | None] = {}


def _quick_metrics(path: Path, chars: str, needed: str) -> Metrics | None:
    """Metrics of a candidate file without decoding all its glyphs (Noto has thousands)."""
    key = (str(path), chars, needed)
    if key in _QUICK:
        return _QUICK[key]
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    from fontTools.ttLib import TTFont

    from app.formats.pdf.glyphs import Glyph

    result = None
    try:
        f = TTFont(path, lazy=True)
        cmap = f.getBestCmap() or {}
        if all(c.isspace() or ord(c) in cmap for c in needed):
            gs = f.getGlyphSet()
            glyphs, sub = {}, {}
            for ch in set(chars + "HExonI"):
                g = cmap.get(ord(ch))
                if g and g not in glyphs:
                    pen = DecomposingRecordingPen(gs)
                    gs[g].draw(pen)
                    glyphs[g] = Glyph(g, gs[g].width, pen, "CFF " in f)
                    sub[ord(ch)] = g
            rf = RebuiltFont(path.stem, f["head"].unitsPerEm, 0, 0, glyphs, sub)
            result = metrics(rf, chars)
    except Exception as exc:
        log.info("cannot measure %s: %s", path.name, exc)
    _QUICK[key] = result
    return result


def distance(a: Metrics, b: Metrics) -> float:
    d = 3 * abs(a.width - b.width) / a.width + 2 * abs(a.density - b.density) / max(a.density, 1e-3)
    if a.cap and b.cap:
        d += abs(a.cap - b.cap) / a.cap
    if a.xh and b.xh:
        d += abs(a.xh - b.xh) / a.xh
    return d / 7


@dataclass
class Candidate:
    family: str
    weight: int
    path: Path
    score: float  # 0 = identical metrics
    source: str  # bundled | google

    def font(self) -> RebuiltFont:
        rf = RebuiltFont.from_pdf_font(self.family, self.path.read_bytes())
        assert rf is not None
        return rf

    def preview(self, text: str, size: float = 15) -> str:
        """Small PNG (data URL) of `text` in this font, for the Review tab."""
        doc = pymupdf.open()
        page = doc.new_page(width=360, height=size * 1.8)
        arch = pymupdf.Archive()
        arch.add(self.path.read_bytes(), "f.ttf")
        page.insert_htmlbox(page.rect + (4, 3, -4, 0), f"<div>{_esc(text)}</div>", archive=arch, scale_low=0,
                            css=f"@font-face{{font-family:f;src:url(f.ttf);}} div{{font-family:f;font-size:{size}px}}")
        return "data:image/png;base64," + base64.b64encode(page.get_pixmap(dpi=96).tobytes("png")).decode()


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bundled(generic: str) -> list[tuple[str, int, Path]]:
    out = []
    for f in sorted(FONT_DIR.glob("*.ttf")):
        m = re.match(r"(.+)-(\d+)\.ttf$", f.name)
        if not m:
            continue
        family = next((k for k in BUNDLED if k.replace(" ", "") == m.group(1)), m.group(1))
        if GENERIC.get(family, "sans-serif") == generic or generic not in ("serif", "monospace", "sans-serif"):
            out.append((family, int(m.group(2)), f))
    return out


def _google(generic: str, weight: int, cache: Path) -> list[tuple[str, int, Path]]:
    cache.mkdir(parents=True, exist_ok=True)
    out = []
    for family, kind in GOOGLE.items():
        if kind != generic:
            continue
        dest = cache / f"{family.replace(' ', '')}-{weight}.ttf"
        if not dest.exists():
            try:
                css = urllib.request.urlopen(css_url(family, [weight]), timeout=10).read().decode()
                url = re.search(r"src: url\((\S+?\.ttf)\)", css)
                if not url:
                    continue
                dest.write_bytes(urllib.request.urlopen(url.group(1), timeout=20).read())
            except Exception as exc:  # offline or blocked: bundled candidates are still offered
                log.info("google font %s unavailable: %s", family, exc)
                continue
        out.append((family, weight, dest))
    return out


def rank(original: RebuiltFont, generic: str, bold: bool, needed: str, cache: Path,
         allow_google: bool = True, top: int = 3) -> list[Candidate]:
    """Best candidates that cover `needed`, closest metrics first."""
    chars = "".join(c for c in SAMPLE if original.has(c)) or "".join(chr(c) for c in list(original.cmap)[:30])
    target = metrics(original, chars)
    weight = 700 if bold else 400

    def score_all(pool: list[tuple[str, int, Path]], source: str) -> list[Candidate]:
        res = []
        for family, w, path in pool:
            if abs(w - weight) > 300 and any(x[0] == family and x[1] == weight for x in pool):
                continue
            m = _quick_metrics(path, chars, needed)
            if m is None or target is None:
                continue
            res.append(Candidate(family, w, path, round(distance(target, m) + abs(w - weight) / 10000, 4), source))
        return sorted(res, key=lambda c: c.score)

    cands = score_all(_bundled(generic), "bundled")
    if allow_google and (not cands or cands[0].score > GOOD_ENOUGH):
        cands = sorted(cands + score_all(_google(generic, weight, cache), "google"), key=lambda c: c.score)
    seen, out = set(), []
    for c in cands:
        if c.family not in seen:
            seen.add(c.family)
            out.append(c)
    return out[:top]
