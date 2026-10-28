"""TextUnits + translations -> PDF. Pure code: geometry, fonts and the anti-overflow ladder.

Overflow ladder for each unit (stop at the first rung that fits):
  1. original box, original size                       -> "fit"
  2. box grown into free space below/right of it       -> "expanded"
  3. grown box, font shrunk down to MIN_SCALE          -> "shrunk"
  4. nothing fits                                      -> "overflow" (the quality loop asks the
     translator for a shorter version; if it still does not fit it is force-shrunk and reported)
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from html import escape

import pymupdf

from app.formats.pdf.extract import rules_on, split_line
from app.formats.pdf.glyphs import RebuiltFont
from app.languages import Language
from app.models import Fit, TextUnit

MUPDF = threading.RLock()  # MuPDF is not thread-safe; every document operation goes through this lock
SCALES = (1.0, 0.94, 0.88, 0.82, 0.76, 0.7, 0.64, 0.58, 0.52)
ALLOWED_TAGS = re.compile(r"</?(?:b|i|u)>|<br\s*/?>")
PAIRED = re.compile(r"<(/?)(b|i|u)>")
SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
PAD = 1.5


def sanitize_markup(text: str) -> str:
    """Escape everything except balanced <b>/<i>/<u> and <br>; drop paired tags if the model broke them."""
    text = re.sub(r"<br\s*/?>", "<br>", text)
    tags = ALLOWED_TAGS.findall(text)
    opened = PAIRED.findall(text)
    depth: dict[str, int] = {"b": 0, "i": 0, "u": 0}
    balanced = True
    for closing, tag in opened:
        depth[tag] += -1 if closing else 1
        balanced &= depth[tag] >= 0
    balanced &= all(v == 0 for v in depth.values())
    placeholder = {}
    if tags and balanced:
        def keep(m: re.Match) -> str:
            key = f"\x00{len(placeholder)}\x00"
            placeholder[key] = m.group(0)
            return key
        text = ALLOWED_TAGS.sub(keep, text)
    else:
        text = PAIRED.sub("", text)
        text = re.sub(r"<br>", "\x01", text)
    text = escape(text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"), quote=False)
    for key, tag in placeholder.items():
        text = text.replace(key, tag)
    return text.replace("\x01", "<br>")


def plain_text(markup: str) -> str:
    return re.sub(r"\s*<br\s*/?>\s*", " ", PAIRED.sub("", markup)).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


# --------------------------------------------------------------------------- fonts

@dataclass
class FontBook:
    """Original embedded fonts, rebuilt (app/formats/pdf/glyphs.py) so they can be reused for translations.

    Coverage is exact (only glyphs with a real outline count), missing accented letters are built from the
    font's own pieces, and the rebuilt fonts have no ligature table. A font is only used for a block when it
    covers every character of that block; otherwise the block falls back to the closest generic family and
    the layout QA reports it.
    """

    doc: pymupdf.Document
    fonts: dict[str, RebuiltFont] = field(default_factory=dict)  # clean name -> rebuilt font
    usable: dict[str, bool] = field(default_factory=dict)
    archive: pymupdf.Archive = field(default_factory=pymupdf.Archive)
    _versions: dict[str, str] = field(default_factory=dict)  # font -> archive file of its latest build
    substitutes: dict[str, str] = field(default_factory=dict)  # font -> similar font chosen in review

    def __post_init__(self) -> None:
        seen: set[int] = set()
        for page in self.doc:
            for xref, ext, _type, basefont, *_ in page.get_fonts(full=True):
                name = SUBSET_PREFIX.sub("", basefont)
                if xref in seen or ext not in ("ttf", "otf", "cff"):
                    continue
                seen.add(xref)
                try:
                    _n, _e, _t, buf = self.doc.extract_font(xref)
                except Exception:
                    continue
                rf = RebuiltFont.from_pdf_font(name, buf) if buf else None
                if not rf or not rf.cmap:
                    continue
                if name in self.fonts:
                    self.fonts[name].merge(rf)
                else:
                    self.fonts[name] = rf

    def decide_for_target(self, texts_by_font: dict[str, list[str]], target: Language) -> None:
        """Before translating (fit measurements depend on it): build the target language's extra letters
        and keep a font when its script matches the target."""
        for font, texts in texts_by_font.items():
            rf = self.fonts.get(font)
            if rf is None or (target.script != "latin" and target.code != "en"):
                self.usable[font] = False
                continue
            used = {c for t in texts for c in plain_text(t) if not c.isspace()}
            rf.ensure(rf.missing("".join(used) + target.probe))
            # usable when most source blocks are covered; uncovered blocks fall back one by one (and are reported)
            covered = sum(not rf.missing(plain_text(t)) for t in texts)
            self.usable[font] = bool(texts) and covered >= 0.6 * len(texts)

    def covers(self, font: str, text: str) -> bool:
        rf = self.fonts.get(font)
        return rf is not None and not rf.missing(plain_text(text))

    def use_original(self, font: str, text: str) -> bool:
        rf = self.fonts.get(font)
        if not self.usable.get(font) or rf is None:
            return False
        miss = rf.missing(plain_text(text))
        if miss:
            rf.ensure(miss)
        return not rf.missing(plain_text(text))

    def missing(self, font: str, text: str) -> set[str]:
        rf = self.fonts.get(font)
        return rf.missing(plain_text(text)) if rf else set()

    def override(self, font: str, path) -> bool:
        """Review decision: replace an original font by a similar complete font for the whole document."""
        rf = RebuiltFont.from_pdf_font(font, path.read_bytes())
        if rf is None:
            return False
        self.fonts[font] = rf
        self.usable[font] = True
        self.substitutes[font] = path.stem
        self._versions.pop(font, None)
        return True

    @staticmethod
    def css_name(font: str) -> str:
        return "orig-" + re.sub(r"[^A-Za-z0-9]", "", font)

    def _file(self, font: str) -> str:
        rf = self.fonts[font]
        name = f"{self.css_name(font)}-{self.substitutes.get(font, 'orig')}-{len(rf.built)}.ttf"
        if self._versions.get(font) != name:
            self.archive.add(rf.ttf(), name)
            self._versions[font] = name
        return name

    def font_face_css(self) -> str:
        return "".join(f"@font-face {{font-family: {self.css_name(f)}; src: url({self._file(f)});}}\n"
                       for f, ok in self.usable.items() if ok and f in self.fonts)

    def stats(self) -> dict:
        return {"fonts": {n: {"built": "".join(rf.built), "ms": round(rf.build_ms, 1), "glyphs": len(rf.cmap),
                              "substitute": self.substitutes.get(n)}
                          for n, rf in self.fonts.items()},
                "glyphs_built": sum(len(rf.built) for rf in self.fonts.values()),
                "ms": round(sum(rf.build_ms for rf in self.fonts.values()), 1), "tokens": 0}


# --------------------------------------------------------------------------- html

def unit_html(unit: TextUnit, text: str, lang: Language, fonts: FontBook | None) -> tuple[str, str]:
    s = unit.style
    original = fonts is not None and fonts.use_original(s.font, text)
    family = f"{fonts.css_name(s.font)}, {s.family}" if original else s.family
    align = s.align
    if lang.rtl:
        align = {"left": "right", "justify": "justify"}.get(align, align)
    weight = "normal" if original else ("bold" if s.bold else "normal")
    style = "normal" if original else ("italic" if s.italic else "normal")
    css = (f"body, div {{margin:0; padding:0;}} div {{font-family:{family}; font-size:{s.size:.2f}px; color:{s.color};"
           f" font-weight:{weight}; font-style:{style}; text-align:{align}; line-height:{s.line_height:.2f};}}")
    direction = "rtl" if lang.rtl else "ltr"
    return f'<div dir="{direction}">{sanitize_markup(text)}</div>', css


# --------------------------------------------------------------------------- fitting

@dataclass
class PageGeometry:
    """Where a unit may grow: free space not used by other text, images or its own container."""

    rect: pymupdf.Rect
    obstacles: list[tuple[str, pymupdf.Rect]]  # (unit id | "text" | "img" | "rule", rect)
    margin: pymupdf.Rect
    claims: dict[str, pymupdf.Rect] = field(default_factory=dict)  # space each unit's current text really uses

    @classmethod
    def from_page(cls, page: pymupdf.Page, units: list[TextUnit]) -> "PageGeometry":
        obs = [(u.id, pymupdf.Rect(u.bbox)) for u in units]
        mine = [pymupdf.Rect(r) for u in units for r in u.line_rects]
        if not any(u.ocr for u in units):  # text we do not translate (numbers, codes) is an obstacle too
            for b in page.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    for seg in split_line(line):  # per segment: a TOC page number shares the line with its entry
                        r = pymupdf.Rect(seg.bbox)
                        if seg.text and not any(r.intersects(m) for m in mine):
                            obs.append(("text", r))
        if not any(u.ocr for u in units):  # on scans the whole page is one image
            obs += [("img", pymupdf.Rect(i["bbox"])) for i in page.get_image_info()]
            # separators and frames' edges, except the underlines that move with their text
            decor = [pymupdf.Rect(d) for u in units for d in u.decorations]
            for r in rules_on(page):
                if not any(abs(r.y0 - d.y0) < 1 and r.intersects(d + (-1, -1, 1, 1)) for d in decor):
                    obs.append(("rule", r))
        content = pymupdf.Rect()
        for _, r in obs:
            content |= r
        pr = page.rect
        margin = pymupdf.Rect(min(content.x0, pr.width * 0.06), min(content.y0, pr.height * 0.04),
                              max(content.x1, pr.width * 0.94), max(content.y1, pr.height * 0.96)) \
            if not content.is_empty else pr
        return cls(pr, obs, margin & pr)

    def grown(self, unit: TextUnit) -> pymupdf.Rect:
        box = pymupdf.Rect(unit.bbox)
        limit = pymupdf.Rect(unit.container) + (PAD, PAD, -PAD, -PAD) if unit.container else self.margin
        others = [r for oid, r in self.obstacles if oid != unit.id and not r.intersects(box)]
        others += [r for oid, r in self.claims.items() if oid != unit.id and not r.intersects(box)]
        # grow down until the next thing below that shares our columns
        y1 = limit.y1
        for r in others:
            if r.x1 > box.x0 and r.x0 < box.x1 and r.y0 >= box.y1 - 0.5:
                y1 = min(y1, r.y0 - 2)
        box.y1 = max(box.y1, y1)
        # grow sideways (towards the reading direction's free side) without hitting anything
        x1, x0 = limit.x1, limit.x0
        for r in others:
            if r.y1 > box.y0 and r.y0 < box.y1:
                if r.x0 >= box.x1 - 0.5:
                    x1 = min(x1, r.x0 - 4)
                if r.x1 <= box.x0 + 0.5:
                    x0 = max(x0, r.x1 + 4)
        if unit.style.align == "center":
            grow = min(box.x0 - x0, x1 - box.x1)
            box.x0, box.x1 = box.x0 - max(grow, 0), box.x1 + max(grow, 0)
        elif unit.style.align == "right":
            box.x0 = min(box.x0, x0)
        else:
            box.x1 = max(box.x1, x1)
        # headings and labels should not balloon: cap growth to keep the look of the page
        if unit.role in ("heading", "label"):
            box.y1 = min(box.y1, unit.bbox[3] + unit.style.size * unit.style.line_height * 1.2)
        return box


def fits(rect: pymupdf.Rect, html: str, css: str, archive: pymupdf.Archive | None, scale: float) -> bool:
    r = pymupdf.Rect(0, 0, rect.width / scale, rect.height / scale)
    story = pymupdf.Story(html=html, user_css=css, archive=archive)
    more, filled = story.place(r)
    return not more and filled[2] <= r.width + 0.5  # a word wider than the box overflows sideways


def layout_box(unit: TextUnit) -> pymupdf.Rect:
    """The unit's glyph bbox plus CSS half-leading above and below, so text lands where it was."""
    heights = sorted(r[3] - r[1] for r in unit.line_rects)
    glyph_h = heights[len(heights) // 2]
    lead = max(0.0, unit.style.size * unit.style.line_height - glyph_h)
    return pymupdf.Rect(unit.bbox) + (0, -lead / 2, 1, lead / 2 + 0.5)


def _max_scale(rect, html, css, archive, lo: float, hi: float) -> float:
    """Largest scale in [lo, hi] that fits (what insert_htmlbox will pick), to 0.5% precision."""
    for _ in range(7):
        mid = (lo + hi) / 2
        if fits(rect, html, css, archive, mid):
            lo = mid
        else:
            hi = mid
    return lo


@dataclass
class Probe:
    """What MuPDF actually produces for one unit: line boxes in page coordinates and the fonts used."""

    lines: list[pymupdf.Rect]
    fonts: list[tuple[str, str]]  # (font name, text) per span
    scale: float

    @property
    def ink(self) -> pymupdf.Rect:
        r = pymupdf.Rect()
        for ln in self.lines:
            r |= ln
        return r


_SCRATCH: dict[tuple[float, float], pymupdf.Document] = {}


def probe(unit: TextUnit, text: str, fit: Fit, lang: Language, fonts: FontBook | None,
          page_rect: pymupdf.Rect) -> Probe:
    html, css = unit_html(unit, text, lang, fonts)
    css = (fonts.font_face_css() if fonts else "") + css
    rect = pymupdf.Rect(fit.rect) if fit.rect else layout_box(unit)
    key = (round(page_rect.width), round(page_rect.height))
    doc = _SCRATCH.setdefault(key, pymupdf.open())
    page = doc.new_page(width=max(page_rect.width, rect.x1 + 10), height=max(page_rect.height, rect.y1 + 10))
    try:
        _, scale = page.insert_htmlbox(rect, html, css=css, archive=fonts.archive if fonts else None,
                                       scale_low=_low(fit))
        lines, spans = [], []
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                txt = "".join(sp["text"] for sp in ln["spans"])
                if txt.strip():
                    lines.append(pymupdf.Rect(ln["bbox"]))
                    spans += [(sp["font"], sp["text"]) for sp in ln["spans"] if sp["text"].strip()]
        return Probe(lines, spans, scale)
    finally:
        doc.delete_page(page.number)


def _low(fit: Fit) -> float:
    return fit.scale if fit.status == "shrunk" else (0 if fit.status == "overflow" else 1)


def plan_fit(unit: TextUnit, text: str, lang: Language, geom: PageGeometry, fonts: FontBook | None,
             min_scale: float) -> Fit:
    html, css = unit_html(unit, text, lang, fonts)
    css = (fonts.font_face_css() if fonts else "") + css
    archive = fonts.archive if fonts else None
    box = layout_box(unit)
    if fits(box, html, css, archive, 1.0):
        return Fit(status="fit", rect=tuple(box))
    grown = geom.grown(unit) | box
    if fits(grown, html, css, archive, 1.0):
        return Fit(status="expanded", rect=tuple(grown))
    if fits(grown, html, css, archive, min_scale):
        s = _max_scale(grown, html, css, archive, min_scale, 1.0)
        return Fit(status="shrunk", scale=round(s, 3), rect=tuple(grown))
    for s in SCALES:
        if s < min_scale and fits(grown, html, css, archive, s):
            return Fit(status="overflow", scale=s, rect=tuple(grown))
    return Fit(status="overflow", scale=0.0, rect=tuple(grown))


# --------------------------------------------------------------------------- writing

def background_color(page: pymupdf.Page, rect: pymupdf.Rect) -> tuple[float, float, float]:
    """Median color of a thin frame around `rect`, used to paint over text on scanned pages."""
    frame = (pymupdf.Rect(rect) + (-3, -3, 3, 3)) & page.rect
    pix = page.get_pixmap(clip=frame, dpi=36, colorspace=pymupdf.csRGB)
    px = [pix.pixel(x, y) for x in range(pix.width) for y in (0, pix.height - 1)]
    px += [pix.pixel(x, y) for y in range(pix.height) for x in (0, pix.width - 1)]
    if not px:
        return (1, 1, 1)
    chans = list(zip(*px))
    return tuple(sorted(c)[len(c) // 2] / 255 for c in chans)  # type: ignore[return-value]


def erase(page: pymupdf.Page, units: list[TextUnit]) -> None:
    """Remove the original text of `units` without touching images or vector graphics."""
    if units and units[0].ocr:
        for u in units:
            for r in u.line_rects:
                rr = pymupdf.Rect(r) + (-1.5, -1.5, 1.5, 1.5)
                page.draw_rect(rr, color=None, fill=background_color(page, rr), overlay=True)
        return
    for u in units:
        for r in u.line_rects:
            rr = pymupdf.Rect(r)
            inset = rr.height * 0.2  # avoid touching glyphs of the lines above/below
            page.add_redact_annot(rr + (0, inset, 0, -inset), fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    # underlines are vector lines tied to the old text: remove them, CSS draws them under the new text
    decorations = [pymupdf.Rect(d) for u in units for d in u.decorations]
    if decorations:
        for d in decorations:
            page.add_redact_annot(d + (-0.5, -0.8, 0.5, 0.8), fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)


def write_unit(page: pymupdf.Page, unit: TextUnit, text: str, fit: Fit, lang: Language,
               fonts: FontBook | None) -> float:
    html, css = unit_html(unit, text, lang, fonts)
    css = (fonts.font_face_css() if fonts else "") + css
    rect = pymupdf.Rect(fit.rect) if fit.rect else layout_box(unit)
    low = _low(fit)
    spare, scale = page.insert_htmlbox(rect, html, css=css, archive=fonts.archive if fonts else None,
                                       scale_low=low)
    if spare < 0:  # should not happen, but never lose text: force fit
        spare, scale = page.insert_htmlbox(rect, html, css=css, archive=fonts.archive if fonts else None,
                                           scale_low=0)
    return scale
