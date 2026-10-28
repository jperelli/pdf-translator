from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pymupdf

from app.formats.base import InvalidDocument
from app.formats.pdf.extract import extract_page, is_scanned
from app.formats.pdf.render import MUPDF, FontBook, PageGeometry, Probe, erase, plan_fit, probe, write_unit
from app.languages import Language
from app.models import Fit, TextUnit
from app.models import Issue
from app.quality.checks import images_preserved
from app.quality.layout import overlapping_output_lines, page_issues

pymupdf.TOOLS.mupdf_display_errors(False)


class PdfDocument:
    def __init__(self, path: Path, *, max_pages: int, ocr_language: str, min_font_scale: float):
        try:
            self.doc = pymupdf.open(path, filetype="pdf")
        except Exception as exc:
            raise InvalidDocument("The file is not a readable PDF (it may be corrupted or truncated).") from exc
        if self.doc.needs_pass:
            raise InvalidDocument("The PDF is password protected. Remove the password and upload it again.")
        if self.doc.page_count == 0:
            raise InvalidDocument("The PDF has no pages.")
        if self.doc.page_count > max_pages:
            raise InvalidDocument(f"The PDF has {self.doc.page_count} pages; the limit is {max_pages}.")
        try:
            self.doc[self.doc.page_count - 1].get_text("text")  # touch the last page: catches broken xrefs
        except Exception as exc:
            raise InvalidDocument("The PDF structure is damaged and cannot be read.") from exc
        self.path = path
        self.repaired = bool(getattr(self.doc, "is_repaired", False))
        self.page_count = self.doc.page_count
        self.ocr_language = ocr_language
        self.min_scale = min_font_scale
        self._units: dict[int, list[TextUnit]] = {}
        self._by_id: dict[str, TextUnit] = {}
        self._geom: dict[int, PageGeometry] = {}
        self.scanned_pages = [p.number for p in self.doc if is_scanned(p)]
        self.fonts: FontBook | None = None
        self.min_scale_overrides: dict[str, float] = {}  # review decision "let it shrink more"
        self._translated: pymupdf.Document | None = None

    def units(self, page: int) -> list[TextUnit]:
        with MUPDF:
            if page not in self._units:
                units = extract_page(self.doc[page], self.ocr_language)
                self._units[page] = units
                self._by_id.update({u.id: u for u in units})
            return self._units[page]

    def prepare(self, target: Language) -> None:
        with MUPDF:
            self.fonts = FontBook(self.doc)
            texts: dict[str, list[str]] = defaultdict(list)
            for units in self._units.values():
                for u in units:
                    texts[u.style.font].append(u.text)
            self.fonts.decide_for_target(texts, target)

    def _geometry(self, page: int) -> PageGeometry:
        if page not in self._geom:
            self._geom[page] = PageGeometry.from_page(self.doc[page], self.units(page))
        return self._geom[page]

    def measure(self, unit: TextUnit, text: str, target: Language) -> Fit:
        with MUPDF:
            return plan_fit(unit, text, target, self._geometry(unit.page), self.fonts,
                            self.min_scale_overrides.get(unit.id, self.min_scale))

    def layout_page(self, page: int, texts: dict[str, str], target: Language
                    ) -> dict[str, tuple[Fit, Probe, list[Issue]]]:
        """Lay out a whole page in reading order: each unit claims the space its text really uses, so no two
        units can grow into the same area. Returns the fit, MuPDF's real line boxes/fonts and layout issues."""
        with MUPDF:
            geom = self._geometry(page)
            geom.claims.clear()
            units = self.units(page)
            fits: dict[str, Fit] = {}
            probes: dict[str, Probe] = {}
            for u in units:
                if u.id not in texts:
                    continue
                fit = plan_fit(u, texts[u.id], target, geom, self.fonts, self.min_scale_overrides.get(u.id, self.min_scale))
                pr = probe(u, texts[u.id], fit, target, self.fonts, self.doc[page].rect)
                fits[u.id], probes[u.id] = fit, pr
                if pr.lines:
                    geom.claims[u.id] = pr.ink + (-0.5, -0.5, 0.5, 0.5)
            issues = page_issues(units, probes, fits, geom, self.fonts, texts, target, self.min_scale)
            return {uid: (fits[uid], probes[uid], issues.get(uid, [])) for uid in fits}

    def measure_by_id(self, unit_id: str, text: str, target: Language) -> Fit:
        return self.measure(self._by_id[unit_id], text, target)

    def page_sizes(self) -> list[tuple[float, float]]:
        with MUPDF:
            return [(p.rect.width, p.rect.height) for p in self.doc]

    def unit(self, unit_id: str) -> TextUnit:
        return self._by_id[unit_id]

    def page_png(self, page: int, dpi: int = 72, translated: bool = False) -> bytes:
        with MUPDF:
            doc = self._translated if translated and self._translated else self.doc
            return doc[page].get_pixmap(dpi=dpi).tobytes("png")

    def render(self, translations: dict[str, str], target: Language, out: Path) -> dict:
        with MUPDF:
            result = pymupdf.open(self.path)
            scales, fits, issues, overlaps = {}, {}, {}, {}
            for pno in range(self.page_count):
                page = result[pno]
                texts = {u.id: translations[u.id] for u in self.units(pno) if u.id in translations}
                layout = self.layout_page(pno, texts, target)
                units = [u for u in self.units(pno) if u.id in texts]
                erase(page, units)
                for u in units:
                    fit, _pr, iss = layout[u.id]
                    scales[u.id] = write_unit(page, u, texts[u.id], fit, target, self.fonts)
                    fits[u.id], issues[u.id] = fit, iss
                before = overlapping_output_lines(self.doc[pno])
                after = overlapping_output_lines(page)
                new = [pair for pair in after if not any(abs(pair[0].y0 - b[0].y0) < 1 and abs(pair[0].x0 - b[0].x0) < 1
                                                         for b in before)]
                if new:
                    overlaps[pno + 1] = len(new)
            result.set_metadata({**(self.doc.metadata or {}), "producer": "Stark Document Translator (PyMuPDF)"})
            result.save(out, garbage=3, deflate=True)
            result.close()
            self._translated = pymupdf.open(out)
            ok, moved = images_preserved(self.doc, self._translated)
            preserved = {u: self.fonts.use_original(self._by_id[u].style.font, translations[u])
                         for u in translations} if self.fonts else {}
            return {"images_preserved": ok, "pages_with_moved_images": moved, "scales": scales,
                    "font_preserved": preserved, "fits": fits, "layout_issues": issues,
                    "output_overlaps": overlaps, "glyphs": self.fonts.stats() if self.fonts else {}}

    def close(self) -> None:
        self.doc.close()
        if self._translated:
            self._translated.close()


class PdfFormat:
    name = "pdf"
    extensions = (".pdf",)
    media_type = "application/pdf"

    def sniff(self, head: bytes) -> bool:
        return b"%PDF-" in head[:1024]

    def load(self, path: Path, *, max_pages: int, ocr_language: str, min_font_scale: float) -> PdfDocument:
        return PdfDocument(path, max_pages=max_pages, ocr_language=ocr_language, min_font_scale=min_font_scale)
