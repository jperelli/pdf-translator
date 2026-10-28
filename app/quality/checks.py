"""Deterministic quality checks: free, instant and never hallucinate. They run before the LLM judge."""

from __future__ import annotations

import re
from collections import Counter

import pymupdf

from app.formats.pdf.render import ALLOWED_TAGS, plain_text
from app.languages import SCRIPT_PATTERNS, Language
from app.models import Fit, Issue

NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
PENALTY = {"critical": 25.0, "major": 10.0, "minor": 2.0}


def _numbers(text: str) -> Counter:
    text = text.translate(EASTERN_DIGITS)
    # 1,000.5 / 1.000,5 / 1 000 -> compare digit runs only, separators vary by locale
    return Counter(re.sub(r"[.,]", "", n) for n in NUMBER.findall(text))


def check_block(source: str, translation: str, target: Language, protected: list[str], fit: Fit,
                min_scale: float) -> list[Issue]:
    issues: list[Issue] = []
    src, tr = plain_text(source).strip(), plain_text(translation).strip()
    if not tr:
        return [Issue(origin="check", severity="critical", category="omission", message="Empty translation.")]

    for term in protected:
        if term in src and term not in tr:
            issues.append(Issue(origin="check", severity="critical", category="terminology",
                                message=f'Protected term "{term}" is missing or was translated.',
                                suggestion=f'Keep "{term}" verbatim.'))

    missing = _numbers(src) - _numbers(tr)
    if missing:
        issues.append(Issue(origin="check", severity="major", category="omission",
                            message=f"Numbers missing in translation: {', '.join(sorted(missing))}.",
                            suggestion="Restore every number and unit from the source."))

    letters = re.findall(r"[^\W\d_]", tr)
    script = SCRIPT_PATTERNS.get(target.script)
    if script and len(letters) >= 4:
        in_script = len(script.findall(tr))
        protected_letters = sum(len(re.findall(r"[^\W\d_]", t)) * tr.count(t) for t in protected)
        translatable = len(letters) - protected_letters
        if translatable >= 4 and in_script < 0.3 * translatable:
            issues.append(Issue(origin="check", severity="critical", category="untranslated",
                                message=f"Translation is not written in {target.name} script."))

    if tr == src and len(src.split()) > 3 and not all(w in " ".join(protected) for w in src.split()):
        issues.append(Issue(origin="check", severity="major", category="untranslated",
                            message="Block is identical to the source."))

    if Counter(ALLOWED_TAGS.findall(source)) != Counter(ALLOWED_TAGS.findall(translation)):
        issues.append(Issue(origin="check", severity="minor", category="markup",
                            message="Bold/italic markup differs from the source.",
                            suggestion="Keep <b>/<i> around the corresponding words."))

    ratio = len(tr) / max(1, len(src))
    lo, hi = (0.2, 1.6) if target.script == "cjk" else (0.5, 3.5)
    if len(src) > 25 and not lo <= ratio <= hi:
        issues.append(Issue(origin="check", severity="minor", category="accuracy",
                            message=f"Unusual length ratio {ratio:.2f}: possible omission or addition."))

    if fit.status == "overflow":
        issues.append(Issue(origin="check", severity="major", category="layout",
                            message=f"Does not fit its box even at {int(min_scale * 100)}% font size.",
                            suggestion="Write a shorter version that keeps the full meaning."))
    elif fit.status == "shrunk" and fit.scale < 0.85:
        issues.append(Issue(origin="check", severity="minor", category="layout",
                            message=f"Font had to shrink to {int(fit.scale * 100)}% to fit."))
    return issues


def score(issues: list[Issue]) -> float:
    """MQM-style: start at 100, subtract weighted penalties (GEMBA-MQM weights 25/5/1, tuned to 25/10/2)."""
    return max(0.0, 100.0 - sum(PENALTY[i.severity] for i in issues))


def images_preserved(src: pymupdf.Document, out: pymupdf.Document, tol: float = 1.0) -> tuple[bool, list[int]]:
    """Every image of the source is still on the same page at the same position and size."""
    moved = []
    for pno in range(src.page_count):
        a = sorted(tuple(round(v / tol) for v in i["bbox"]) for i in src[pno].get_image_info())
        b = sorted(tuple(round(v / tol) for v in i["bbox"]) for i in out[pno].get_image_info())
        if a != b:
            moved.append(pno + 1)
    return not moved, moved
