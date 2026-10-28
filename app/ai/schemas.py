"""Structured outputs of the agents. Every field is required: the SDK sends them as strict JSON schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProtectedTerm(BaseModel):
    term: str = Field(description="Exact surface form as it appears in the document")
    kind: Literal["brand", "product", "model", "person", "place", "code", "unit", "url", "other"]
    reason: str


class JargonTerm(BaseModel):
    term: str
    translation: str = Field(description="Recommended rendering in the target language (may equal the term)")
    note: str


class DocumentAnalysis(BaseModel):
    source_language: str = Field(description="ISO 639-1 code of the main language of the document")
    source_language_name: str
    tone: Literal["technical", "formal", "legal", "marketing", "casual", "mixed"]
    domain: str = Field(description="Subject area in a few words, e.g. 'electric motocross owner manual'")
    summary: str = Field(description="Two sentences on what the document is, for translators")
    style_notes: str = Field(description="Voice, person (you/the user), imperative vs descriptive, formality")
    protected_terms: list[ProtectedTerm] = Field(description="Never translate: brands, product names, codes")
    jargon: list[JargonTerm] = Field(description="Domain slang / technical terms with a recommended translation")


class TranslatedBlock(BaseModel):
    id: str
    text: str


class TranslationBatch(BaseModel):
    translations: list[TranslatedBlock]


class JudgeError(BaseModel):
    severity: Literal["minor", "major", "critical"]
    category: Literal["accuracy", "omission", "addition", "mistranslation", "untranslated", "terminology",
                      "fluency", "grammar", "style", "locale", "markup"]
    span: str = Field(description="The offending words of the translation (of the source for omissions)")
    explanation: str = Field(description="At most 15 words")
    suggestion: str = Field(description="Just the corrected words, or an empty string")


class JudgedBlock(BaseModel):
    id: str
    errors: list[JudgeError]


class JudgeReport(BaseModel):
    blocks: list[JudgedBlock] = Field(description="Only the blocks that have errors; omit correct blocks")
