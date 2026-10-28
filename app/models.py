"""Domain objects shared by the format handlers, the agents and the quality layer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Rect = tuple[float, float, float, float]
Severity = Literal["minor", "major", "critical"]


class Style(BaseModel):
    font: str  # original font name, e.g. "UniversalSansDisplay-300"
    family: Literal["sans-serif", "serif", "monospace"] = "sans-serif"
    size: float
    color: str = "#000000"
    bold: bool = False
    italic: bool = False
    align: Literal["left", "center", "right", "justify"] = "left"
    line_height: float = 1.2


class TextUnit(BaseModel):
    """One translatable piece of a page: a paragraph, a heading, a table cell, a label."""

    id: str
    page: int
    bbox: Rect
    line_rects: list[Rect]
    text: str  # may contain <b>/<i>/<u> inline markup and <br> line breaks
    style: Style
    role: Literal["heading", "body", "label", "caption"] = "body"
    container: Rect | None = None  # enclosing box (table cell, button, frame) we must stay inside
    decorations: list[Rect] = Field(default_factory=list)  # underlines drawn as vector lines, moved with the text
    ocr: bool = False

    @property
    def max_chars_hint(self) -> int:
        """Rough capacity of the box in characters at the original size (used as a length budget)."""
        w = self.bbox[2] - self.bbox[0]
        h = self.bbox[3] - self.bbox[1]
        if self.container:
            w = max(w, self.container[2] - self.container[0] - 4)
            h = max(h, self.container[3] - self.container[1] - 4)
        lines = max(1, int(h / (self.style.size * self.style.line_height) + 0.25))
        per_line = w / (self.style.size * 0.5)
        return max(4, int(lines * per_line))


class Issue(BaseModel):
    origin: Literal["check", "judge"]
    severity: Severity
    category: str
    message: str
    suggestion: str | None = None


class Fit(BaseModel):
    status: Literal["fit", "expanded", "shrunk", "overflow"]
    scale: float = 1.0
    rect: Rect | None = None


class Attempt(BaseModel):
    iteration: int
    stage: Literal["translate", "refine", "escalate", "redo", "edit", "previous"]
    text: str
    score: float
    fit: Fit
    issues: list[Issue] = Field(default_factory=list)


class BlockResult(BaseModel):
    id: str
    page: int
    source: str
    translation: str
    score: float  # the lower of translation and layout
    translation_score: float = 100.0
    layout_score: float = 100.0
    font: str = ""
    fit: Fit
    issues: list[Issue] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    converged: bool = True
    font_preserved: bool = False


class Term(BaseModel):
    term: str
    kind: str
    reason: str = ""


class Jargon(BaseModel):
    term: str
    translation: str
    note: str = ""


class Analysis(BaseModel):
    source_language: str
    source_language_name: str
    tone: str  # register: technical, formal, legal, marketing, casual, mixed
    domain: str
    summary: str
    style_notes: str = ""
    protected_terms: list[Term] = Field(default_factory=list)
    jargon: list[Jargon] = Field(default_factory=list)


class Usage(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


class PageReport(BaseModel):
    page: int
    width: float = 0
    height: float = 0
    ocr: bool = False
    blocks: int = 0
    score: float = 100.0
    translation_score: float = 100.0
    layout_score: float = 100.0
    overflow: int = 0
    collisions: int = 0
    images_moved: int = 0


class CallRecord(BaseModel):
    """One agent run: what it cost and what it was for (drives the Stats tab)."""

    agent: str
    model: str
    stage: str  # analysis | translate | judge | refine | escalate | redo
    page: int | None = None
    round: int = 0
    blocks: int = 0
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    version: int = 1


class QualityReport(BaseModel):
    score: float  # headline: the lower of translation_score and layout_score
    translation_score: float
    layout_score: float
    threshold: float
    passed: bool
    fail_reasons: list[str] = Field(default_factory=list)
    collisions: int = 0
    output_overlaps: int = 0  # overlapping text lines found by reading the final PDF back
    missing_glyph_blocks: int = 0
    font_substituted_blocks: int = 0
    purpose: str = "fast"
    blocks_total: int
    blocks_below_threshold: int
    blocks_refined: int
    blocks_escalated: int
    overflow_blocks: int
    shrunk_blocks: int
    font_preserved_ratio: float
    images_preserved: bool
    judge_coverage: float  # share of blocks the LLM judge actually reviewed (1.0 = all)
    issues_by_severity: dict[str, int]
    pages: list[PageReport]
    usage_by_agent: dict[str, Usage]
    timings: dict[str, float]
    backend: str
    models: dict[str, str]
