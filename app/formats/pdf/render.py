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
from dataclasses import dataclass, field
from html import escape

import pymupdf

from app.languages import Language
from app.models import Fit, TextUnit

SCALES = (1.0, 0.94, 0.88, 0.82, 0.76, 0.7, 0.64, 0.58, 0.52)
ALLOWED_TAGS = re.compile(r"</?(b|i)>")
SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
PAD = 1.5
LIGATURE = re.compile(r"f[filt]")


def sanitize_markup(text: str) -> str:
    """Escape everything except balanced <b>/<i>; drop tags entirely if the model broke them."""
    tags = ALLOWED_TAGS.findall(text)
    opened = [t for t in re.findall(r"<(/?)(b|i)>", text)]
    depth: dict[str, int] = {"b": 0, "i": 0}
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
        text = ALLOWED_TAGS.sub("", text)
    text = escape(text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"), quote=False)
    for key, tag in placeholder.items():
        text = text.replace(key, tag)
    return text


def plain_text(markup: str) -> str:
    return ALLOWED_TAGS.sub("", markup).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


# --------------------------------------------------------------------------- fonts

@dataclass
class FontBook:
    """Original embedded fonts, reused only when they really contain every glyph we need.

    Subset fonts lie: their cmap often lists accented letters whose outlines were stripped, so coverage
    is checked by rendering each character and looking for ink.
    """

    doc: pymupdf.Document
    fonts: dict[str, bytes] = field(default_factory=dict)  # clean name -> font file
    _ink: dict[tuple[str, str], bool] = field(default_factory=dict)
    usable: dict[str, bool] = field(default_factory=dict)
    archive: pymupdf.Archive = field(default_factory=pymupdf.Archive)

    def __post_init__(self) -> None:
        for page in self.doc:
            for xref, ext, _type, basefont, *_ in page.get_fonts(full=True):
                name = SUBSET_PREFIX.sub("", basefont)
                if name in self.fonts or ext not in ("ttf", "otf", "cff"):
                    continue
                try:
                    _n, _e, _t, buf = self.doc.extract_font(xref)
                except Exception:
                    continue
                if buf and self._has_ink(buf, "a"):
                    self.fonts[name] = buf

    @staticmethod
    def _has_ink(buf: bytes, ch: str) -> bool:
        if ch.isspace():
            return True
        tmp = pymupdf.open()
        page = tmp.new_page(width=40, height=40)
        try:
            page.insert_font(fontname="F0", fontbuffer=buf)
            page.insert_text((6, 30), ch, fontname="F0", fontsize=26)
        except Exception:
            return False
        pix = page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY)
        return any(b < 200 for b in pix.samples)

    @staticmethod
    def _story_ink(buf: bytes, text: str) -> int:
        arch = pymupdf.Archive()
        arch.add(buf, "f.font")
        tmp = pymupdf.open()
        page = tmp.new_page(width=120, height=40)
        page.insert_htmlbox(page.rect, f"<div>{text}</div>", archive=arch,
                            css="@font-face {font-family: f; src: url(f.font);} div {font-family: f; font-size: 26px}")
        return sum(1 for b in page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY).samples if b < 200)

    def ligatures_ok(self, font: str) -> bool:
        """MuPDF always applies f-ligatures, and subsets often carry an empty ligature glyph."""
        key = (font, "<lig>")
        if key not in self._ink:
            buf = self.fonts[font]
            joined, apart = self._story_ink(buf, "fi fl ff"), self._story_ink(buf, "f i f l f f")
            self._ink[key] = apart > 0 and joined >= 0.8 * apart
        return self._ink[key]

    def covers(self, font: str, text: str) -> bool:
        buf = self.fonts.get(font)
        if buf is None:
            return False
        if LIGATURE.search(text) and not self.ligatures_ok(font):
            return False
        for ch in set(text):
            key = (font, ch)
            if key not in self._ink:
                self._ink[key] = self._has_ink(buf, ch)
            if not self._ink[key]:
                return False
        return True

    def decide(self, texts_by_font: dict[str, list[str]], min_ratio: float = 0.8) -> None:
        """Keep an original font when it can render most of its translated blocks.

        Decided once per font for the whole document, so pages look consistent; the few blocks that need a
        missing glyph fall back to the closest generic family (see `use_original`).
        """
        for font, texts in texts_by_font.items():
            covered = sum(self.covers(font, plain_text(t)) for t in texts) if font in self.fonts else 0
            ok = bool(texts) and covered / len(texts) >= min_ratio
            self.usable[font] = ok
            if ok:
                self.archive.add(self.fonts[font], f"{self.css_name(font)}.font")

    def decide_for_target(self, texts_by_font: dict[str, list[str]], target: Language) -> None:
        """Decide before translating (fit measurements need it): a font is kept when it has ink for the
        letters its source text uses plus the target language's extra letters, and working ligatures."""
        for font, texts in texts_by_font.items():
            if font not in self.fonts or (target.script != "latin" and target.code != "en"):
                self.usable[font] = False
                continue
            used = {c for t in texts for c in plain_text(t) if c.isalpha()}
            probe = "".join(sorted(used | set(target.probe)))
            ok = self.covers(font, probe) and self.ligatures_ok(font)
            self.usable[font] = ok
            if ok:
                self.archive.add(self.fonts[font], f"{self.css_name(font)}.font")

    def use_original(self, font: str, text: str) -> bool:
        return self.usable.get(font, False) and self.covers(font, plain_text(text))

    @staticmethod
    def css_name(font: str) -> str:
        return "orig-" + re.sub(r"[^A-Za-z0-9]", "", font)

    def font_face_css(self) -> str:
        return "".join(
            f"@font-face {{font-family: {self.css_name(f)}; src: url({self.css_name(f)}.font);}}\n"
            for f, ok in self.usable.items() if ok)


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
    obstacles: list[tuple[str, pymupdf.Rect]]
    margin: pymupdf.Rect

    @classmethod
    def from_page(cls, page: pymupdf.Page, units: list[TextUnit]) -> "PageGeometry":
        obs = [(u.id, pymupdf.Rect(u.bbox)) for u in units]
        mine = [pymupdf.Rect(r) for u in units for r in u.line_rects]
        if not any(u.ocr for u in units):  # text we do not translate (numbers, codes) is an obstacle too
            for b in page.get_text("dict")["blocks"]:
                for line in b.get("lines", []):
                    r = pymupdf.Rect(line["bbox"])
                    if line["spans"] and "".join(s["text"] for s in line["spans"]).strip() and \
                            not any(r.intersects(m) for m in mine):
                        obs.append(("text", r))
        if not any(u.ocr for u in units):  # on scans the whole page is one image
            obs += [("img", pymupdf.Rect(i["bbox"])) for i in page.get_image_info()]
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
    for s in SCALES[1:]:
        if s < min_scale - 1e-6:
            break
        if fits(grown, html, css, archive, s):
            return Fit(status="shrunk", scale=s, rect=tuple(grown))
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


def write_unit(page: pymupdf.Page, unit: TextUnit, text: str, fit: Fit, lang: Language,
               fonts: FontBook | None) -> float:
    html, css = unit_html(unit, text, lang, fonts)
    css = (fonts.font_face_css() if fonts else "") + css
    rect = pymupdf.Rect(fit.rect) if fit.rect else layout_box(unit)
    low = fit.scale if fit.status in ("shrunk",) else (0 if fit.status == "overflow" else 1)
    spare, scale = page.insert_htmlbox(rect, html, css=css, archive=fonts.archive if fonts else None,
                                       scale_low=low)
    if spare < 0:  # should not happen, but never lose text: force fit
        spare, scale = page.insert_htmlbox(rect, html, css=css, archive=fonts.archive if fonts else None,
                                           scale_low=0)
    return scale
