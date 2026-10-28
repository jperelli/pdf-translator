"""The seam between the translation pipeline and file formats.

The pipeline only knows TextUnits, Fits and these two protocols. Supporting a new format (DOCX, PPTX,
Markdown...) means writing one `FormatHandler` + `LoadedDocument` pair and registering it in
`app/formats/__init__.py`; nothing in the agents or the quality loop changes. Formats without fixed boxes
can return Fit(status="fit") from `measure`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.languages import Language
from app.models import Fit, TextUnit


class InvalidDocument(ValueError):
    """The upload is not a document we can process. The message is safe to show to the user."""


class LoadedDocument(Protocol):
    page_count: int
    scanned_pages: list[int]

    def units(self, page: int) -> list[TextUnit]: ...

    def prepare(self, target: Language) -> None:
        """Called once after extraction, before any measurement (e.g. pick reusable fonts)."""

    def measure(self, unit: TextUnit, text: str, target: Language) -> Fit: ...

    def page_png(self, page: int, dpi: int = 72, translated: bool = False) -> bytes: ...

    def render(self, translations: dict[str, tuple[str, Fit]], target: Language, out: Path) -> dict: ...

    def close(self) -> None: ...


class FormatHandler(Protocol):
    name: str
    extensions: tuple[str, ...]
    media_type: str

    def sniff(self, head: bytes) -> bool: ...

    def load(self, path: Path, *, max_pages: int, ocr_language: str, min_font_scale: float) -> LoadedDocument: ...
