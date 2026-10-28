from __future__ import annotations

from app.formats.base import FormatHandler, InvalidDocument
from app.formats.pdf import PdfFormat

FORMATS: list[FormatHandler] = [PdfFormat()]


def detect(filename: str, head: bytes) -> FormatHandler:
    """Pick a handler by content (magic bytes), not by the user-controlled file name."""
    for fmt in FORMATS:
        if fmt.sniff(head):
            return fmt
    names = ", ".join(e for f in FORMATS for e in f.extensions)
    hint = " The file name says PDF but the content is not." if filename.lower().endswith(".pdf") else ""
    raise InvalidDocument(f"Unsupported file type. Supported: {names}.{hint}")


__all__ = ["FORMATS", "detect", "InvalidDocument"]
