"""PDF -> TextUnits. Pure code, no AI: geometry and styles come straight from PyMuPDF."""

from __future__ import annotations

import re
import statistics
import subprocess
from dataclasses import dataclass, field
from html import escape

import pymupdf

from app.models import Style, TextUnit

LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
RTL_CHAR = re.compile(r"[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")
CJK_CHAR = re.compile(r"[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF]")


def is_rtl(text: str) -> bool:
    letters = LETTER.findall(text)
    return bool(letters) and len(RTL_CHAR.findall(text)) > len(letters) / 2


def word_count(text: str) -> int:
    return max(len(text.split()), len(CJK_CHAR.findall(text)) // 2)


SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


def font_family(name: str, flags: int) -> str:
    n = name.lower()
    if any(k in n for k in ("mono", "courier", "code", "consol")):
        return "monospace"
    if "sans" in n or any(k in n for k in ("helvetica", "arial", "nimbus", "verdana", "roboto", "inter")):
        return "sans-serif"
    if any(k in n for k in ("serif", "times", "roman", "garamond", "georgia", "charis", "tiro", "minion")):
        return "serif"
    if flags & pymupdf.TEXT_FONT_MONOSPACED:
        return "monospace"
    return "serif" if flags & pymupdf.TEXT_FONT_SERIFED else "sans-serif"


def is_bold(span: dict) -> bool:
    n = span["font"].lower()
    return bool(span["flags"] & pymupdf.TEXT_FONT_BOLD) or any(
        k in n for k in ("bold", "black", "heavy", "semibold", "-600", "-700", "-800", "-900"))


def is_italic(span: dict) -> bool:
    return bool(span["flags"] & pymupdf.TEXT_FONT_ITALIC) or "italic" in span["font"].lower() or \
        "oblique" in span["font"].lower()


def hex_color(c: int) -> str:
    return f"#{c:06x}"


def needs_translation(text: str) -> bool:
    """Numbers, page labels, bullets and punctuation are copied as-is."""
    return len(LETTER.findall(text)) >= 2


def _needs_space(prev: dict, cur: dict) -> bool:
    """Many PDFs (CV builders, browsers) store words as separate objects with no space character."""
    if not prev["text"] or not cur["text"] or prev["text"][-1].isspace() or cur["text"][0].isspace():
        return False
    return cur["bbox"][0] - prev["bbox"][2] > 0.15 * min(prev["size"], cur["size"])


def join_spans(spans: list[dict], render) -> str:
    out = []
    for i, s in enumerate(spans):
        if i and _needs_space(spans[i - 1], s):
            out.append(" ")
        out.append(render(s))
    return "".join(out)


@dataclass
class Segment:
    """A horizontal run of spans on one line (a line split at tabs / big gaps)."""

    spans: list[dict]
    bbox: pymupdf.Rect
    full_line: bool  # the segment is the whole line

    @property
    def text(self) -> str:
        return join_spans(self.spans, lambda s: s["text"]).replace("\t", " ").strip()

    @property
    def size(self) -> float:
        return max(s["size"] for s in self.spans)


@dataclass
class Group:
    segments: list[Segment] = field(default_factory=list)

    @property
    def bbox(self) -> pymupdf.Rect:
        r = pymupdf.Rect(self.segments[0].bbox)
        for s in self.segments[1:]:
            r |= s.bbox
        return r


def split_line(line: dict) -> list[Segment]:
    spans = [s for s in line["spans"] if s["text"].strip()]
    if not spans:
        return []
    out: list[list[dict]] = [[spans[0]]]
    for prev, cur in zip(spans, spans[1:]):
        gap = cur["bbox"][0] - prev["bbox"][2]
        if prev["text"].endswith("\t") or gap > 2.2 * max(prev["size"], cur["size"]):
            out.append([cur])
        else:
            out[-1].append(cur)
    segs = []
    for group in out:
        r = pymupdf.Rect(group[0]["bbox"])
        for s in group[1:]:
            r |= s["bbox"]
        segs.append(Segment(group, r, full_line=len(out) == 1))
    return segs


def _continues(group: Group, seg: Segment, rules: list[pymupdf.Rect]) -> bool:
    """Does `seg` (next line) belong to the same paragraph as `group`?"""
    last = group.segments[-1]
    if not (last.full_line and seg.full_line):
        return False
    ratio = seg.size / last.size
    if not 0.85 <= ratio <= 1.18:
        return False
    if is_bold(last.spans[0]) != is_bold(seg.spans[0]) and len(group.segments) == 1 and \
            len(last.text) < 60:
        return False  # bold heading followed by body text
    gap = seg.bbox.y0 - last.bbox.y1
    if gap > 0.9 * last.size or gap < -0.5 * last.size:
        return False
    first = group.segments[0].bbox
    overlap = min(first.x1, seg.bbox.x1) - max(first.x0, seg.bbox.x0)
    if overlap < 0.3 * min(first.width, seg.bbox.width):
        return False
    # an indented first line after a full line starts a new paragraph (mirrored for RTL scripts)
    indent = first.x1 - seg.bbox.x1 if is_rtl(seg.text) else seg.bbox.x0 - first.x0
    right_aligned = all(abs(x.bbox.x1 - seg.bbox.x1) < 0.5 * last.size for x in group.segments)
    if indent > 1.0 * last.size and len(group.segments) > 1 and not right_aligned:
        return False
    # a separator line between the two lines ends the paragraph
    for r in rules:
        if last.bbox.y1 - 1 <= r.y0 <= seg.bbox.y0 + 1 and min(r.x1, seg.bbox.x1) - max(r.x0, seg.bbox.x0) > 0 \
                and not any(underline_for(sp, [r]) for sp in last.spans):
            return False
    # a change of indentation after the first lines (list item vs. sub-item) starts a new unit
    if len(group.segments) > 1 and abs(seg.bbox.x0 - last.bbox.x0) > last.size and \
            abs(seg.bbox.x1 - last.bbox.x1) > last.size and not is_rtl(seg.text):
        return False
    if len(group.segments) == 1 and seg.bbox.x0 - last.bbox.x0 > last.size and \
            abs(seg.bbox.x1 - last.bbox.x1) > last.size:
        return False  # the second line is indented more than the first: a sub-item, not a wrap
    if last.bbox.x0 - seg.bbox.x0 > last.size and last.text.rstrip()[-1:] in ".:;!?" and not is_rtl(seg.text):
        return False  # back to a smaller indent after a finished sentence: next item
    # a short single line in another font/colour (heading, label, footer) is its own unit
    a, b = last.spans[0], seg.spans[0]
    if len(group.segments) == 1 and len(last.text) < 60 and (a["font"], a["color"]) != (b["font"], b["color"]):
        return False
    return True


def underline_for(span: dict, rules: list[pymupdf.Rect]) -> pymupdf.Rect | None:
    """A thin horizontal vector line just below the span's baseline, covering most of its width."""
    base, size = span["origin"][1], span["size"]
    x0, x1 = span["bbox"][0], span["bbox"][2]
    for r in rules:
        if base - 0.15 * size <= r.y0 <= base + 0.45 * size and min(r.x1, x1) - max(r.x0, x0) >= 0.6 * (x1 - x0):
            return r
    return None


def _markup(spans: list[dict], bold: bool, italic: bool, rules: list[pymupdf.Rect], used: list) -> str:
    def render(s: dict) -> str:
        t = escape(s["text"].replace("\t", " "), quote=False)
        if is_bold(s) and not bold:
            t = f"<b>{t}</b>"
        if is_italic(s) and not italic:
            t = f"<i>{t}</i>"
        u = underline_for(s, rules) if t.strip() else None
        if u is not None:
            used.append(u)
            t = f"<u>{t}</u>"
        return t
    return re.sub(r"</u>(\s*)<u>", r"\1", join_spans(spans, render))


def _join_lines(lines: list[str], breaks: list[bool] | None = None) -> str:
    """Join wrapped lines; `breaks[i]` marks an intentional line break after line i (kept as <br>)."""
    text = lines[0].strip()
    for i, nxt in enumerate(lines[1:]):
        nxt = nxt.strip()
        if breaks and breaks[i]:
            text = f"{text}<br>{nxt}"
        elif text.endswith("-") and nxt[:1].islower():
            text = text[:-1] + nxt
        else:
            text = f"{text} {nxt}"
    return re.sub(r"[ \t]+", " ", text).strip()


def _hard_breaks(group: Group, align: str) -> list[bool]:
    """A line that ends early although the next line's first word would have fitted was broken on purpose
    (addresses, list items, a summary written line by line)."""
    bb = group.bbox
    if align in ("right", "center"):  # ragged-left text (addresses, contact blocks) is broken by hand
        return [True] * (len(group.segments) - 1)
    out = []
    for seg, nxt in zip(group.segments, group.segments[1:]):
        words = nxt.text.split()
        if not words or not nxt.text:
            out.append(False)
            continue
        char_w = nxt.bbox.width / max(1, len(nxt.text))
        need = (len(words[0]) + 1) * char_w
        free = {"right": seg.bbox.x0 - bb.x0, "center": bb.width - seg.bbox.width}.get(align, bb.x1 - seg.bbox.x1)
        out.append(free > need + 0.5 * seg.size)
    return out


def _align(group: Group) -> str:
    rtl = is_rtl(" ".join(s.text for s in group.segments))
    if len(group.segments) < 2:
        return "right" if rtl else "left"
    bb = group.bbox
    lefts = [abs(s.bbox.x0 - bb.x0) for s in group.segments]
    rights = [abs(bb.x1 - s.bbox.x1) for s in group.segments]
    tol = group.segments[0].size * 0.6
    # justified only when several full lines end exactly at the same edge (two can match by chance)
    if len(group.segments) >= 4 and all(x < tol for x in lefts[:-1]) and \
            all(x < group.segments[0].size * 0.25 for x in rights[:-1]):
        return "justify"
    if all(x < tol for x in (rights if rtl else lefts)):
        return "right" if rtl else "left"
    if all(x < tol for x in (lefts if rtl else rights)):
        return "left" if rtl else "right"
    centers = [abs((s.bbox.x0 + s.bbox.x1) / 2 - (bb.x0 + bb.x1) / 2) for s in group.segments]
    return "center" if all(c < tol for c in centers) else "left"


def _drop_duplicates(groups: list[Group]) -> list[Group]:
    """Some PDFs draw the same text twice (link styling, fake bold): keep one copy."""
    out: list[Group] = []
    for g in groups:
        text = " ".join(s.text for s in g.segments)
        bb = g.bbox
        dup = False
        for h in out:
            hb = h.bbox
            inter = bb & hb
            if inter.is_empty:
                continue
            ht = " ".join(s.text for s in h.segments)
            if (text in ht or ht in text) and inter.get_area() > 0.6 * min(bb.get_area(), hb.get_area()):
                if len(text) > len(ht):  # keep the longer copy
                    out[out.index(h)] = g
                dup = True
                break
        if not dup:
            out.append(g)
    return out


def rules_on(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Thin horizontal vector lines: separators and underlines."""
    out = []
    for d in page.get_drawings():
        r = d.get("rect")
        if r is not None and r.height <= 2.5 and r.width >= 4:
            r = pymupdf.Rect(r)
            if r.height < 0.5:  # hairlines are zero-height rects, which PyMuPDF treats as empty
                cy = (r.y0 + r.y1) / 2
                r.y0, r.y1 = cy - 0.25, cy + 0.25
            out.append(r)
    return out


def _containers(page: pymupdf.Page) -> list[pymupdf.Rect]:
    rects = []
    for d in page.get_drawings():
        r = d.get("rect")
        if r is None or r.is_empty or r.width < 8 or r.height < 6:
            continue
        if r.width > page.rect.width * 0.95 and r.height > page.rect.height * 0.95:
            continue  # page background
        if d.get("fill") is not None or d.get("closePath") or any(i[0] in ("re", "qu") for i in d["items"]):
            rects.append(r)
    return rects


def _container_for(bbox: pymupdf.Rect, containers: list[pymupdf.Rect]) -> pymupdf.Rect | None:
    inner = pymupdf.Rect(bbox) + (1, 1, -1, -1)
    hits = [c for c in containers if c.contains(inner)]
    return min(hits, key=lambda c: c.width * c.height) if hits else None


def is_scanned(page: pymupdf.Page) -> bool:
    if len(page.get_text("text").strip()) > 20:
        return False
    area = page.rect.width * page.rect.height
    covered = sum(pymupdf.Rect(i["bbox"]).intersect(page.rect).get_area() for i in page.get_image_info())
    if covered <= 0.5 * area:
        return False
    # a scanned document is mostly paper-white; a full-page photo (cover, product shot) is not worth OCR
    pix = page.get_pixmap(dpi=20, colorspace=pymupdf.csGRAY)
    return sum(b > 200 for b in pix.samples) > 0.4 * len(pix.samples)


OCR_DPI = 300


def _ocr_units(page: pymupdf.Page, language: str) -> list[TextUnit]:
    """Scanned page: Tesseract paragraphs become units. Low-confidence noise (photos) is dropped."""
    png = page.get_pixmap(dpi=OCR_DPI).tobytes("png")
    try:
        proc = subprocess.run(["tesseract", "stdin", "stdout", "-l", language, "--psm", "3", "tsv"],
                              input=png, capture_output=True, check=True, timeout=120)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"OCR failed (is tesseract installed?): {exc}") from exc
    k = 72 / OCR_DPI
    paras: dict[tuple, list[dict]] = {}
    prev: dict | None = None
    column = 0
    for row in proc.stdout.decode().splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        conf = float(cols[10])
        if conf < 45:
            continue
        x, y, w, h = (int(v) * k for v in cols[6:10])
        word = {"line": cols[2:5], "text": cols[11], "rect": pymupdf.Rect(x, y, x + w, y + h), "conf": conf}
        if prev and prev["line"] == word["line"] and x - prev["rect"].x1 > 2.5 * max(h, prev["rect"].height):
            column += 1
        elif not prev or prev["line"][:2] != word["line"][:2]:
            column = 0
        paras.setdefault((cols[2], cols[3], column), []).append(word)
        prev = word
    units = []
    for words in paras.values():
        text = " ".join(w["text"] for w in words)
        if not needs_translation(text) or statistics.mean(w["conf"] for w in words) < 60:
            continue
        lines: dict[tuple, pymupdf.Rect] = {}
        for w in words:
            key = tuple(w["line"])
            lines[key] = lines[key] | w["rect"] if key in lines else pymupdf.Rect(w["rect"])
        rects = list(lines.values())
        bbox = pymupdf.Rect(rects[0])
        for r in rects[1:]:
            bbox |= r
        size = statistics.median(r.height for r in rects) * 0.95
        units.append(TextUnit(
            id=f"p{page.number + 1}-b{len(units) + 1}", page=page.number, bbox=tuple(bbox),
            line_rects=[tuple(r) for r in rects], text=escape(text, quote=False),
            style=Style(font="ocr", family="sans-serif", size=round(size, 1),
                        line_height=(bbox.height / len(rects)) / size if len(rects) > 1 else 1.2),
            role="heading" if size > 16 else "label" if word_count(text) <= 4 else "body", ocr=True))
    return units


def extract_page(page: pymupdf.Page, ocr_language: str = "eng") -> list[TextUnit]:
    if is_scanned(page):
        return _ocr_units(page, ocr_language)
    data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES & ~pymupdf.TEXT_PRESERVE_LIGATURES)
    containers = _containers(page)
    rules = rules_on(page)

    groups: list[Group] = []
    current: Group | None = None
    for block in data["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            if abs(line["dir"][1]) > 0.01 or line.get("wmode", 0):
                continue  # rotated / vertical text is left untouched (documented limitation)
            for seg in split_line(line):
                if current and _continues(current, seg, rules) and \
                        _container_for(seg.bbox, containers) == _container_for(current.bbox, containers):
                    current.segments.append(seg)
                else:
                    current = Group([seg])
                    groups.append(current)

    groups = _drop_duplicates(groups)
    sizes = [s.size for g in groups for s in g.segments]
    body_size = statistics.median(sizes) if sizes else 10
    units = []
    for g in groups:
        spans = [s for seg in g.segments for s in seg.spans]
        plain = _join_lines([seg.text for seg in g.segments])
        if not needs_translation(plain):
            continue
        weights: dict[tuple, int] = {}
        for s in spans:
            key = (s["font"], round(s["size"], 1), s["color"], is_bold(s), is_italic(s), s["flags"])
            weights[key] = weights.get(key, 0) + len(s["text"])
        font, size, color, bold, italic, flags = max(weights, key=weights.get)
        align = _align(g)
        breaks = _hard_breaks(g, align)
        decorations: list[pymupdf.Rect] = []
        text = _join_lines([_markup(seg.spans, bold, italic, rules, decorations) for seg in g.segments], breaks)
        baselines = [seg.spans[0]["origin"][1] for seg in g.segments]
        lh = statistics.median([b - a for a, b in zip(baselines, baselines[1:])]) / size if len(baselines) > 1 else 1.2
        bbox = g.bbox
        container = _container_for(bbox, containers)
        words = word_count(plain)
        role = "heading" if size >= body_size * 1.25 or (bold and words <= 12 and len(g.segments) <= 2) else \
            "label" if words <= 4 else "body"
        units.append(TextUnit(
            id=f"p{page.number + 1}-b{len(units) + 1}",
            page=page.number,
            bbox=tuple(bbox),
            line_rects=[tuple(seg.bbox) for seg in g.segments],
            text=text,
            style=Style(
                font=SUBSET_PREFIX.sub("", font), family=font_family(font, flags), size=size,
                color=hex_color(color), bold=bold, italic=italic, align=align,
                line_height=min(max(lh, 1.0), 2.2),
            ),
            role=role,
            container=tuple(container) if container else None,
            decorations=[tuple(r) for r in decorations],
        ))
    return units

