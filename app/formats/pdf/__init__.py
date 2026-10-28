from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pymupdf

from app.formats.base import InvalidDocument
from app.formats.pdf.extract import extract_page, is_scanned
from app.formats.pdf.render import FontBook, PageGeometry, erase, plan_fit, write_unit
from app.languages import Language
from app.models import Fit, TextUnit
from app.quality.checks import images_preserved

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
        self._translated: pymupdf.Document | None = None

    def units(self, page: int) -> list[TextUnit]:
        if page not in self._units:
            units = extract_page(self.doc[page], self.ocr_language)
            self._units[page] = units
            self._by_id.update({u.id: u for u in units})
        return self._units[page]

    def prepare(self, target: Language) -> None:
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
        return plan_fit(unit, text, target, self._geometry(unit.page), self.fonts, self.min_scale)

    def measure_by_id(self, unit_id: str, text: str, target: Language) -> Fit:
        return self.measure(self._by_id[unit_id], text, target)

    def unit(self, unit_id: str) -> TextUnit:
        return self._by_id[unit_id]

    def page_png(self, page: int, dpi: int = 72, translated: bool = False) -> bytes:
        doc = self._translated if translated and self._translated else self.doc
        return doc[page].get_pixmap(dpi=dpi).tobytes("png")

    def render(self, translations: dict[str, tuple[str, Fit]], target: Language, out: Path) -> dict:
        result = pymupdf.open(self.path)
        scales = {}
        for pno in range(self.page_count):
            page = result[pno]
            units = [u for u in self.units(pno) if u.id in translations]
            erase(page, units)
            for u in units:
                text, fit = translations[u.id]
                scales[u.id] = write_unit(page, u, text, fit, target, self.fonts)
        result.set_metadata({**(self.doc.metadata or {}), "producer": "Stark Document Translator (PyMuPDF)"})
        result.save(out, garbage=3, deflate=True)
        result.close()
        self._translated = pymupdf.open(out)
        ok, moved = images_preserved(self.doc, self._translated)
        preserved = {u: self.fonts.use_original(self._by_id[u].style.font, translations[u][0])
                     for u in translations} if self.fonts else {}
        return {"images_preserved": ok, "pages_with_moved_images": moved, "scales": scales,
                "font_preserved": preserved}

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
