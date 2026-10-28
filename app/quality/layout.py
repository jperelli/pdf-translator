"""Layout QA: what the rendered page really looks like. Deterministic, zero tokens.

Works on `Probe`s (MuPDF's actual line boxes and fonts for each unit, see render.probe) and, after the
final render, on the output page itself. Overlapping text is critical in every mode.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pymupdf

from app.languages import Language
from app.models import Fit, Issue, TextUnit

if TYPE_CHECKING:
    from app.formats.pdf.render import FontBook, PageGeometry, Probe

LAYOUT_CATEGORIES = {"layout", "collision", "missing_glyphs", "font_substituted", "structure"}
TOL = 1.0
OBSTACLE_NAMES = {"text": "untranslated text", "img": "an image", "rule": "a separator line"}


def _hits(a: pymupdf.Rect, b: pymupdf.Rect, thin_ok: bool = False) -> bool:
    """Do two boxes collide visibly? Line boxes include the font's full ascender/descender, so neighbouring
    lines may touch by a point or two without any glyph touching: require a real share of the line height."""
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    if w <= TOL:
        return False
    if thin_ok:  # a hairline crosses the text if it runs through the middle 60% of the line box
        line, rule = (a, b) if a.height >= b.height else (b, a)
        mid = (rule.y0 + rule.y1) / 2
        return line.y0 + 0.2 * line.height < mid < line.y1 - 0.2 * line.height
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    return h > max(TOL, 0.3 * min(a.height, b.height))


def _family(font: str) -> str:
    return re.sub(r"[-_ ]?(Regular|Bold|Italic|Oblique|Medium|Light|BoldItalic)$", "", font.split("+")[-1]).lower()


def fit_issues(fit: Fit, min_scale: float) -> list[Issue]:
    if fit.status == "overflow":
        return [Issue(origin="check", severity="major", category="layout",
                      message=f"Does not fit its box even at {int(min_scale * 100)}% font size "
                              f"(forced to {int(fit.scale * 100)}%).",
                      suggestion="Write a shorter version that keeps the full meaning.")]
    if fit.status == "shrunk" and fit.scale < 0.85:
        return [Issue(origin="check", severity="minor", category="layout",
                      message=f"Font had to shrink to {int(fit.scale * 100)}% to fit.")]
    return []


def page_issues(units: list[TextUnit], probes: dict[str, Probe], fits: dict[str, Fit], geom: PageGeometry,
                fonts: FontBook | None, texts: dict[str, str], target: Language,
                min_scale: float) -> dict[str, list[Issue]]:
    out: dict[str, list[Issue]] = {u.id: fit_issues(fits[u.id], min_scale) for u in units if u.id in probes}
    by_id = {u.id: u for u in units}
    # untranslated units keep their original lines
    lines = {u.id: probes[u.id].lines if u.id in probes else [pymupdf.Rect(r) for r in u.line_rects] for u in units}
    ids = list(lines)

    def add(uid: str, msg: str, sugg: str) -> None:
        if uid in out and not any(i.message == msg for i in out[uid]):
            out[uid].append(Issue(origin="check", severity="critical", category="collision", message=msg,
                                  suggestion=sugg))

    orig = {u.id: [pymupdf.Rect(r) for r in u.line_rects] for u in units}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a not in probes and b not in probes:
                continue
            if any(_hits(la, lb) for la in orig[a] for lb in orig[b]):
                continue  # they already overlapped in the original (e.g. a link label drawn over its text)
            if any(_hits(la, lb) for la in lines[a] for lb in lines[b]):
                add(a, f"Overlaps block {b}.", "Shorten the text or let it shrink; it must not cover other text.")
                add(b, f"Overlaps block {a}.", "Shorten the text or let it shrink; it must not cover other text.")
    for uid, ls in lines.items():
        if uid not in probes:
            continue
        u = by_id[uid]
        orig = pymupdf.Rect(u.bbox) + (-1, -1, 1, 1)
        for kind, r in geom.obstacles:
            if kind not in OBSTACLE_NAMES or r.intersects(orig):
                continue  # it already overlapped in the original (e.g. text printed on a photo)
            if any(_hits(ln, r, thin_ok=kind == "rule") for ln in ls):
                add(uid, f"Overlaps {OBSTACLE_NAMES[kind]}.", "Shorten the text so it stays in its area.")
        if u.container:
            c = pymupdf.Rect(u.container)

            def spills(ln: pymupdf.Rect) -> bool:
                # strict sideways; vertically the line box includes the font's full ascender/descender (tall in
                # Arabic or CJK fonts), so allow a share of the line height as long as the line sits inside
                slack = max(TOL, 0.3 * ln.height)
                return ln.x0 < c.x0 - TOL or ln.x1 > c.x1 + TOL or ln.y0 < c.y0 - slack or ln.y1 > c.y1 + slack
            if any(spills(ln) for ln in ls):
                add(uid, "Spills out of its frame (cell, button or box).", "Shorten the text.")
        if fonts is not None and target.script == "latin":
            out[uid] += font_issues(u, probes[uid], fonts, texts.get(uid, ""))
    return out


def font_issues(u: TextUnit, pr: Probe, fonts: FontBook, text: str) -> list[Issue]:
    if not pr.fonts:
        return []
    fams = {}
    for f, t in pr.fonts:
        fams[_family(f)] = fams.get(_family(f), 0) + len(t)
    main = max(fams, key=fams.get)
    if len(fams) > 1:
        odd = "".join(sorted({c for f, t in pr.fonts if _family(f) != main for c in t if not c.isspace()}))[:20]
        letters = any(c.isalpha() for c in odd)
        return [Issue(origin="check", severity="major" if letters else "minor", category="missing_glyphs",
                      message=f'Font "{u.style.font}" has no glyph for {odd!r}; MuPDF mixed in another font.',
                      suggestion="Review: use a similar complete font, rewrite with available characters, "
                                 "or transliterate.")]
    if fonts.usable.get(u.style.font) and not fonts.use_original(u.style.font, text):
        miss = "".join(sorted(fonts.missing(u.style.font, text)))[:20]
        return [Issue(origin="check", severity="minor", category="font_substituted",
                      message=f'Original font "{u.style.font}" lacks {miss!r}: this block uses a generic '
                              f'{u.style.family} font.',
                      suggestion="Review: use a similar complete font for the whole document.")]
    return []


def overlapping_output_lines(page: pymupdf.Page) -> list[tuple[pymupdf.Rect, pymupdf.Rect]]:
    """Ground truth on the written PDF: text lines of different blocks whose boxes overlap. Lines of one
    block are one paragraph (tall fonts like Naskh legitimately overlap their own line boxes)."""
    lines = []
    for bi, b in enumerate(page.get_text("dict")["blocks"]):
        for ln in b.get("lines", []):
            if "".join(s["text"] for s in ln["spans"]).strip() and abs(ln["dir"][1]) < 0.01:
                lines.append((bi, pymupdf.Rect(ln["bbox"])))
    out = []
    for i, (ba, a) in enumerate(lines):
        for bb, b in lines[i + 1:]:
            if ba == bb:
                continue
            inter = a & b
            if inter.is_empty:
                continue
            if inter.width > 2 and inter.height > 0.35 * min(a.height, b.height):
                out.append((a, b))
    return out
