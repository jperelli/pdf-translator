"""Rebuild embedded PDF fonts so they can be reused for translated text. Pure code, zero tokens.

PDF fonts are usually subsets: they list characters whose outlines were stripped, carry ligature rules
pointing at empty glyphs, and miss the accented letters of the target language. For each embedded font we:

1. read every glyph outline (TrueType, OpenType/CFF or bare CFF) and keep only the characters that really
   have an outline (exact coverage, no guessing by rendering);
2. build missing accented letters from pieces of the same font: base letter + mark (Ä = A + dieresis); when
   the font has no dieresis, two of its own periods are used;
3. write a plain TrueType font without GSUB, so no ligature can map to an empty glyph.
"""

from __future__ import annotations

import io
import time
import unicodedata
from dataclasses import dataclass, field

from fontTools.agl import toUnicode
from fontTools.cffLib import CFFFontSet
from fontTools.fontBuilder import FontBuilder
from fontTools.misc.psCharStrings import T2WidthExtractor
from fontTools.pens.boundsPen import ControlBoundsPen
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen, RecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

# combining mark -> spacing glyph names that can serve as the mark (first one present wins)
MARKS = {
    "\u0308": ("dieresis", "uni00A8", "uni0308"),
    "\u0301": ("acute", "uni00B4", "uni0301"),
    "\u0300": ("grave", "uni0060", "uni0300"),
    "\u0302": ("circumflex", "uni02C6", "uni0302"),
    "\u0303": ("tilde", "uni02DC", "uni0303"),
    "\u030a": ("ring", "uni02DA", "uni030A"),
    "\u0327": ("cedilla", "uni00B8", "uni0327"),
    "\u030c": ("caron", "uni02C7", "uni030C"),
    "\u0304": ("macron", "uni00AF", "uni0304"),
    "\u0328": ("ogonek", "uni02DB", "uni0328"),
    "\u0307": ("dotaccent", "uni02D9", "uni0307"),
    "\u030b": ("hungarumlaut", "uni02DD", "uni030B"),
}
BELOW = {"\u0327", "\u0328"}
SPECIAL = {"ß": None, "ø": None, "Ø": None, "ł": None, "Ł": None, "œ": None, "Œ": None, "æ": None, "Æ": None}


@dataclass
class Glyph:
    name: str
    width: float
    pen: RecordingPen
    cubic: bool

    @property
    def bounds(self) -> tuple[float, float, float, float] | None:
        bp = ControlBoundsPen(None)
        self.pen.replay(bp)
        return bp.bounds


@dataclass
class RebuiltFont:
    name: str
    upm: int
    ascent: int
    descent: int
    glyphs: dict[str, Glyph]
    cmap: dict[int, str]  # only characters with a real outline (plus space)
    built: list[str] = field(default_factory=list)  # characters we constructed
    build_ms: float = 0.0
    _bytes: bytes | None = None

    # ------------------------------------------------------------------ loading

    @classmethod
    def from_pdf_font(cls, name: str, buf: bytes) -> "RebuiltFont | None":
        try:
            return cls._from_sfnt(name, buf)
        except Exception:
            pass
        try:
            return cls._from_bare_cff(name, buf)
        except Exception:
            return None

    @classmethod
    def _from_sfnt(cls, name: str, buf: bytes) -> "RebuiltFont":
        f = TTFont(io.BytesIO(buf), lazy=False)
        gs = f.getGlyphSet()
        cubic = "CFF " in f
        glyphs = {}
        for g in f.getGlyphOrder():
            pen = DecomposingRecordingPen(gs)
            try:
                gs[g].draw(pen)
            except Exception:
                continue
            glyphs[g] = Glyph(g, gs[g].width, pen, cubic)
        upm = f["head"].unitsPerEm
        hhea = f["hhea"] if "hhea" in f else None
        ascent = hhea.ascent if hhea else int(upm * 0.8)
        descent = hhea.descent if hhea else -int(upm * 0.2)
        cmap = {}
        for cp, g in (f.getBestCmap() or {}).items():
            cmap[cp] = g
        if not cmap:  # symbolic subset without a usable cmap: fall back to glyph names
            cmap = {cp: g for g in glyphs if (cp := _cp(g)) is not None}
        return cls._finish(name, upm, ascent, descent, glyphs, cmap)

    @classmethod
    def _from_bare_cff(cls, name: str, buf: bytes) -> "RebuiltFont":
        cff = CFFFontSet()
        cff.decompile(io.BytesIO(buf), None)
        top = cff[cff.fontNames[0]]
        cs = top.CharStrings
        priv = top.Private
        upm = round(1 / top.FontMatrix[0]) if top.FontMatrix and top.FontMatrix[0] else 1000
        glyphs = {}
        for g in cs.keys():
            c = cs[g]
            ex = T2WidthExtractor(c.private.Subrs if hasattr(c.private, "Subrs") else [], c.globalSubrs,
                                  getattr(priv, "nominalWidthX", 0), getattr(priv, "defaultWidthX", 0))
            ex.execute(c)
            pen = RecordingPen()
            c.draw(pen)
            glyphs[g] = Glyph(g, ex.width, pen, True)
        bbox = getattr(top, "FontBBox", [0, -200, 1000, 800])
        cmap = {cp: g for g in glyphs if (cp := _cp(g)) is not None}
        return cls._finish(name, upm, int(bbox[3]), int(bbox[1]), glyphs, cmap)

    @classmethod
    def _finish(cls, name, upm, ascent, descent, glyphs, cmap) -> "RebuiltFont":
        real = {cp: g for cp, g in cmap.items() if g in glyphs and (glyphs[g].bounds is not None or cp == 32)}
        if 32 not in real and "space" in glyphs:
            real[32] = "space"
        return cls(name, upm, ascent, descent, glyphs, real)

    def merge(self, other: "RebuiltFont") -> None:
        """PDFs often embed the same font several times as different subsets: pool their glyphs."""
        if other.upm != self.upm:
            return
        for cp, g in other.cmap.items():
            if cp in self.cmap:
                continue
            name = g if g not in self.glyphs else f"{g}.m{cp:X}"
            self.glyphs[name] = other.glyphs[g]
            self.cmap[cp] = name
        self._bytes = None

    # ------------------------------------------------------------------ coverage / building

    def has(self, ch: str) -> bool:
        return ch.isspace() or ord(ch) in self.cmap

    def missing(self, text: str) -> set[str]:
        return {c for c in set(text) if not self.has(c) and unicodedata.category(c)[0] != "C"}

    def ensure(self, chars: set[str]) -> set[str]:
        """Build what can be built; return the characters still missing."""
        t = time.perf_counter()
        left = set()
        for ch in sorted(chars):
            if self.has(ch):
                continue
            if not self._compose(ch):
                left.add(ch)
        self.build_ms += (time.perf_counter() - t) * 1000
        return left

    def _glyph_for(self, ch: str) -> Glyph | None:
        g = self.cmap.get(ord(ch))
        return self.glyphs.get(g) if g else None

    def _mark_glyph(self, mark: str) -> list[tuple[Glyph, float, float]] | None:
        """The mark as (glyph, dx, scale) pieces, centred on x=0, bottom at y=0."""
        for name in MARKS.get(mark, ()):
            g = self.glyphs.get(name)
            if g and g.bounds:
                x0, y0, x1, y1 = g.bounds
                return [(g, -(x0 + x1) / 2, 1.0, -y0)]
        if mark == "\u0308":  # dieresis from two of the font's own periods
            p = self._glyph_for(".")
            if p and p.bounds:
                x0, y0, x1, y1 = p.bounds
                w = x1 - x0
                s = 0.85
                off = w * s * 0.5 + w * s * 0.45
                return [(p, -(x0 + x1) / 2 - off / s, s, -y0), (p, -(x0 + x1) / 2 + off / s, s, -y0)]
        return None

    def _compose(self, ch: str) -> bool:
        if ch in SPECIAL:
            return False
        decomp = unicodedata.normalize("NFD", ch)
        if len(decomp) < 2:
            return False
        base = self._glyph_for(decomp[0])
        if not base or not base.bounds:
            return False
        bx0, by0, bx1, by1 = base.bounds
        if decomp[0] in "ij" and len(decomp) > 1:  # dotless forms are rarely in subsets: keep it simple
            dotless = self.glyphs.get("dotlessi" if decomp[0] == "i" else "uni0237")
            if dotless and dotless.bounds:
                base = dotless
                bx0, by0, bx1, by1 = base.bounds
        rec = RecordingPen()
        base.pen.replay(rec)
        top, bottom = by1, by0
        gap = self.upm * 0.06
        for mark in decomp[1:]:
            pieces = self._mark_glyph(mark)
            if not pieces:
                return False
            cx = (bx0 + bx1) / 2
            for glyph, dx, s, lift in pieces:
                gx0, gy0, gx1, gy1 = glyph.bounds
                h = (gy1 - gy0) * s
                if mark in BELOW:
                    y = bottom - gap * 0.3 - h
                    bottom = y
                else:
                    y = top + gap
                tp = TransformPen(rec, (s, 0, 0, s, cx + dx * s, y + lift * s))
                glyph.pen.replay(tp)
            if mark not in BELOW:
                top = top + gap + max((g.bounds[3] - g.bounds[1]) * s for g, _, s, _ in pieces)
        name = f"uni{ord(ch):04X}"
        self.glyphs[name] = Glyph(name, base.width, rec, base.cubic)
        self.cmap[ord(ch)] = name
        self.built.append(ch)
        self._bytes = None
        return True

    # ------------------------------------------------------------------ output

    def ttf(self) -> bytes:
        if self._bytes is not None:
            return self._bytes
        order = [".notdef"] + sorted({g for g in self.cmap.values()} - {".notdef"})
        fb = FontBuilder(self.upm, isTTF=True)
        fb.setupGlyphOrder(order)
        fb.setupCharacterMap(dict(self.cmap))
        tt = {}
        metrics = {}
        for g in order:
            pen = TTGlyphPen(None)
            src = self.glyphs.get(g)
            if src is not None:
                target = Cu2QuPen(pen, max_err=self.upm / 1000, reverse_direction=src.cubic)
                try:
                    src.pen.replay(target)
                except Exception:
                    pen = TTGlyphPen(None)
            tt[g] = pen.glyph()
            b = src.bounds if src else None
            metrics[g] = (int(round(src.width)) if src else self.upm // 2, int(b[0]) if b else 0)
        fb.setupGlyf(tt)
        fb.setupHorizontalMetrics(metrics)
        fb.setupHorizontalHeader(ascent=self.ascent, descent=self.descent)
        family = "".join(c for c in self.name if c.isalnum())[:28] or "Rebuilt"
        fb.setupNameTable({"familyName": family, "styleName": "Regular"})
        fb.setupOS2(sTypoAscender=self.ascent, sTypoDescender=self.descent, usWinAscent=max(self.ascent, 0),
                    usWinDescent=abs(min(self.descent, 0)))
        fb.setupPost()
        out = io.BytesIO()
        fb.save(out)
        self._bytes = out.getvalue()
        return self._bytes


def _cp(glyph_name: str) -> int | None:
    u = toUnicode(glyph_name)
    return ord(u) if len(u) == 1 else None
