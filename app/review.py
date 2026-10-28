"""Review items (what did not work, with alternatives) and the decisions that drive a redo."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from app.config import normalize_purpose
from app.formats.pdf.fontmatch import FONT_DIR, Candidate, rank
from app.formats.pdf.render import FontBook, plain_text
from app.models import Analysis, BlockResult

GERMAN = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "AE", "Ö": "OE", "Ü": "UE", "ß": "ss", "ẞ": "SS"}


def transliterate(text: str, missing: set[str]) -> str:
    """Replace only the characters the font lacks: German umlauts by their digraphs, others by the base letter."""
    def one(ch: str) -> str:
        if ch not in missing:
            return ch
        if ch in GERMAN:
            out = GERMAN[ch]
            return out
        base = unicodedata.normalize("NFD", ch)[0]
        return base if base.isascii() else ch
    out = "".join(one(c) for c in text)
    # an all-caps context keeps all-caps digraphs; a capitalised word gets "Ae", not "AE"
    return re.sub(r"\b(AE|OE|UE)(?=[a-zß])", lambda m: m.group(1).capitalize(), out)


def _has(b: BlockResult, *cats: str) -> bool:
    return any(i.category in cats for i in b.issues)


def build_items(blocks: list[BlockResult], analysis: Analysis, fonts: FontBook | None, threshold: float,
                purpose: str, cache: Path, styles: dict[str, tuple[str, bool]]) -> list[dict[str, Any]]:
    """`styles` maps an original font name to (generic family, bold) for font matching."""
    items: list[dict[str, Any]] = []

    coll = [b for b in blocks if _has(b, "collision")]
    if coll:
        items.append({
            "kind": "collision", "severity": "critical", "title": f"{len(coll)} block(s) overlap other content",
            "blocks": [b.id for b in coll], "pages": sorted({b.page for b in coll}),
            "options": [{"id": "shorten", "label": "Shorten the text (Refiner)", "tokens": True},
                        {"id": "shrink", "label": "Let the text shrink more (down to 50%)", "tokens": False}]
            + ([{"id": "accept", "label": "Accept (Fast mode only)", "tokens": False}] if normalize_purpose(purpose) == "fast" else []),
            "default": "shorten"})

    by_font: dict[str, list[BlockResult]] = {}
    for b in blocks:
        if _has(b, "missing_glyphs", "font_substituted"):
            by_font.setdefault(b.font, []).append(b)
    for font, bl in by_font.items():
        missing = set()
        for b in bl:
            missing |= fonts.missing(font, b.translation) if fonts else set()
        letters = "".join(sorted(c for c in missing if c.isalpha()))
        symbols = "".join(sorted(c for c in missing if not c.isalpha()))
        cands: list[Candidate] = []
        rf = fonts.fonts.get(font) if fonts else None
        generic, bold = styles.get(font, ("sans-serif", False))
        if rf is not None:
            needed = "".join(sorted({c for b in bl for c in plain_text(b.translation) if not c.isspace()}))
            cands = rank(rf, generic, bold, needed, cache)
        sample = plain_text(bl[0].translation)[:40]
        built = "".join(fonts.fonts[font].built) if fonts and font in fonts.fonts else ""
        options = [{"id": f"similar:{c.family}:{c.weight}:{c.source}", "label": f"Use {c.family} ({c.source})",
                    "tokens": False, "score": c.score, "preview": c.preview(sample)} for c in cands]
        options.append({"id": "rewrite", "label": "Rewrite these blocks using only available characters",
                        "tokens": True})
        if letters and all(c in GERMAN for c in letters):  # ñ -> n would change meaning (año -> ano)
            options.append({"id": "transliterate", "label": "Transliterate (Ä → AE, ß → ss)", "tokens": False})
        options.append({"id": "keep", "label": "Keep the generic fallback font", "tokens": False})
        items.append({
            "kind": "font", "severity": "warning", "font": font,
            "title": f'Font "{font}" lacks {letters or symbols!r} in {len(bl)} block(s)',
            "missing_letters": letters, "missing_symbols": symbols, "built_automatically": built,
            "blocks": [b.id for b in bl], "pages": sorted({b.page for b in bl}),
            "options": options, "default": options[0]["id"] if cands else "keep"})

    low = [b for b in blocks if b.translation_score < threshold or
           any(i.severity == "critical" and i.category not in ("collision", "layout") for i in b.issues)]
    if low:
        items.append({
            "kind": "quality", "severity": "warning", "title": f"{len(low)} block(s) below the quality threshold",
            "blocks": [b.id for b in low], "pages": sorted({b.page for b in low}),
            "options": [{"id": "retry_strong", "label": "Retry with the stronger model", "tokens": True},
                        {"id": "edit", "label": "Edit manually", "tokens": False},
                        {"id": "accept", "label": "Accept as is", "tokens": False}],
            "default": "retry_strong"})

    shrunk = [b for b in blocks if b.fit.status in ("overflow",) or (b.fit.status == "shrunk" and b.fit.scale < 0.85)]
    if shrunk:
        items.append({
            "kind": "layout", "severity": "warning", "title": f"{len(shrunk)} block(s) had to shrink their font a lot",
            "blocks": [b.id for b in shrunk], "pages": sorted({b.page for b in shrunk}),
            "options": [{"id": "shorten", "label": "Shorten the text instead (Refiner)", "tokens": True},
                        {"id": "accept", "label": "Accept the smaller font", "tokens": False}],
            "default": "accept"})

    items.append({
        "kind": "terms", "severity": "info", "title": "Terms: what to translate and what to keep",
        "jargon": [{"term": j.term, "translation": j.translation, "note": j.note} for j in analysis.jargon],
        "protected": [t.term for t in analysis.protected_terms],
        "options": [], "default": None})
    return items


def resolve_candidate(option: str, cache: Path) -> Path | None:
    """`similar:<family>:<weight>:<source>` -> font file (never a client-supplied path)."""
    try:
        _, family, weight, source = option.split(":")
    except ValueError:
        return None
    name = f"{re.sub(r'[^A-Za-z0-9]', '', family)}-{int(weight)}.ttf"
    path = (FONT_DIR if source == "bundled" else cache) / name
    return path if path.is_file() else None


def estimate_tokens(decisions: dict[str, Any], blocks: dict[str, BlockResult]) -> int:
    """Rough token estimate shown before redoing: ~3x the text of every block an agent will touch."""
    ids: set[str] = set()
    for key in ("collision", "layout"):
        d = decisions.get(key) or {}
        if d.get("action") == "shorten":
            ids |= set(d.get("blocks", []))
    for bid, d in (decisions.get("quality") or {}).items():
        if d.get("action") == "retry_strong":
            ids.add(bid)
    for font, d in (decisions.get("fonts") or {}).items():
        if d.get("action") == "rewrite":
            ids |= {b.id for b in blocks.values() if b.font == font and _has(b, "missing_glyphs", "font_substituted")}
    terms = decisions.get("terms") or {}
    changed = set(terms.get("keep_original", [])) | set(terms.get("translate", []))
    ids |= {b.id for b in blocks.values() if any(t.lower() in plain_text(b.source).lower() for t in changed)}
    chars = sum(len(blocks[i].source) + len(blocks[i].translation) for i in ids if i in blocks)
    return int(chars / 4 * 3) + 600 * bool(ids)
