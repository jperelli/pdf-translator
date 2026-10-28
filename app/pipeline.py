"""The translation pipeline: code-orchestrated agents with an evaluator-optimizer loop.

    extract (code) -> analyze (Analyst agent) -> per page, in parallel:
        translate (Translator agent, batched)
        evaluate  (layout fit + deterministic checks + Judge agent)
        refine    (Refiner agent with a check_fit tool) on failing blocks, max N rounds
        escalate  (stronger model + page image) if it still did not converge
    -> render (code) -> document checks -> report
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents import trace

from app.ai.agents import AgentFactory, AgentRunError, TranslationContext, run_agent
from app.config import Settings
from app.formats import InvalidDocument, detect
from app.formats.pdf.extract import CJK_CHAR
from app.formats.pdf.render import plain_text
from app.languages import LANGUAGES, Language, get_language
from app.models import (
    Analysis,
    Attempt,
    BlockResult,
    Fit,
    Issue,
    PageReport,
    QualityReport,
    TextUnit,
    Usage,
)
from app.quality.checks import check_block, score

log = logging.getLogger(__name__)
Emit = Callable[[dict[str, Any]], None]


class PipelineError(RuntimeError):
    """A failure whose message is safe to show to the user."""


@dataclass
class BlockState:
    unit: TextUnit
    best: Attempt | None = None
    attempts: list[Attempt] = field(default_factory=list)

    def consider(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)
        rank = lambda a: (a.score, a.fit.status != "overflow", a.fit.scale)  # noqa: E731
        if self.best is None or rank(attempt) > rank(self.best):
            self.best = attempt


def _sample_for_analysis(pages: list[list[TextUnit]], budget: int = 9000) -> list[dict[str, str]]:
    """Round-robin over pages so the analyst sees the whole document, not just its first page."""
    out, used, depth = [], 0, 0
    while used < budget and any(depth < len(p) for p in pages):
        for units in pages:
            if depth < len(units) and used < budget:
                u = units[depth]
                out.append({"id": u.id, "text": plain_text(u.text)[:600]})
                used += len(out[-1]["text"])
        depth += 1
    return out


class TranslationPipeline:
    def __init__(self, settings: Settings, emit: Emit, job_id: str = "local"):
        self.s = settings
        self.emit = emit
        self.job_id = job_id
        self.factory = AgentFactory(settings)
        self.usage: dict[str, Usage] = {}
        self.timings: dict[str, float] = {}
        self.llm = asyncio.Semaphore(max(2, settings.max_concurrent_pages * 2))
        self.blocks: dict[str, BlockState] = {}
        self.analysis: Analysis | None = None
        self._done_pages = 0
        self.failed: set[str] = set()
        self.judged: set[str] = set()

    # ------------------------------------------------------------------ helpers

    def _stage(self, stage: str, message: str, percent: float) -> None:
        self.emit({"type": "stage", "stage": stage, "message": message, "percent": round(percent, 1)})

    def _ctx(self, target: Language, ids: list[str] | None = None) -> TranslationContext:
        return TranslationContext(target=target, analysis=self.analysis, expected_ids=ids or [])

    @property
    def protected(self) -> list[str]:
        return sorted({t.term for t in self.analysis.protected_terms}) if self.analysis else []

    async def _agent(self, agent, payload, ctx, **kw):
        async with self.llm:
            return await run_agent(agent, payload, ctx, self.usage, timeout=self.s.llm_timeout_seconds * 2, **kw)

    # ------------------------------------------------------------------ main

    async def run(self, src: Path, out: Path, target_code: str, filename: str = "document.pdf") -> dict[str, Any]:
        target = get_language(target_code)
        t_start = time.perf_counter()
        workflow = f"Translate {filename} -> {target.code}"
        with trace(workflow, group_id=self.job_id, metadata={"job_id": self.job_id, "target": target.code}):
            return await self._run(src, out, target, filename, t_start)

    async def _run(self, src: Path, out: Path, target: Language, filename: str, t_start: float) -> dict[str, Any]:
        self._stage("validate", "Checking the file", 1)
        head = src.read_bytes()[:2048]
        fmt = detect(filename, head)
        doc = await asyncio.to_thread(fmt.load, src, max_pages=self.s.max_pages, ocr_language=self.s.ocr_language,
                                      min_font_scale=self.s.min_font_scale)
        try:
            return await self._translate_document(doc, out, target, t_start)
        finally:
            doc.close()

    async def _translate_document(self, doc, out: Path, target: Language, t0: float) -> dict[str, Any]:
        n = doc.page_count
        ocr_note = f", {len(doc.scanned_pages)} scanned (OCR)" if doc.scanned_pages else ""
        self._stage("extract", f"Reading {n} page{'s' * (n > 1)}{ocr_note}", 3)
        t = time.perf_counter()
        pages = await asyncio.to_thread(lambda: [doc.units(p) for p in range(n)])
        self.timings["extract"] = time.perf_counter() - t
        total = sum(len(p) for p in pages)
        if getattr(doc, "repaired", False):
            self.emit({"type": "warning", "message": "The file was damaged and had to be repaired; some content "
                                                     "may be missing from the translation."})
        self.emit({"type": "extracted", "pages": n, "blocks": total, "scanned_pages": [p + 1 for p in doc.scanned_pages]})
        if total == 0:
            raise PipelineError("No translatable text was found in this document.")

        self._stage("analyze", "Detecting language, tone, brand names and jargon", 8)
        t = time.perf_counter()
        sample = _sample_for_analysis(pages)
        try:
            result = await self._agent(self.factory.analyst(), {"blocks": sample}, self._ctx(target))
            self.analysis = Analysis.model_validate(result.model_dump())
        except AgentRunError as exc:
            log.warning("analysis failed, continuing without it: %s", exc)
            self.emit({"type": "warning", "message": "Document analysis failed; translating without a glossary."})
            self.analysis = Analysis(source_language="auto", source_language_name="auto-detected",
                                     tone="mixed", domain="general", summary="")
        self.timings["analyze"] = time.perf_counter() - t
        self.emit({"type": "analysis", "analysis": self.analysis.model_dump()})
        if self.analysis.source_language == target.code:
            self.emit({"type": "warning", "message": f"The document already looks {target.name}; "
                                                     "only blocks in other languages will change."})

        await asyncio.to_thread(doc.prepare, target)
        self._stage("translate", "Translating and checking every page", 12)
        t = time.perf_counter()
        page_sem = asyncio.Semaphore(self.s.max_concurrent_pages)

        async def one(p: int) -> None:
            async with page_sem:
                await self._process_page(doc, p, pages[p], target, n)

        await asyncio.gather(*(one(p) for p in range(n)))
        self.timings["translate_and_qa"] = time.perf_counter() - t

        self._stage("render", "Rebuilding the PDF with the original layout", 92)
        t = time.perf_counter()
        final = {bid: (b.best.text, b.best.fit) for bid, b in self.blocks.items() if b.best}
        render_info = await asyncio.to_thread(doc.render, final, target, out)
        self.timings["render"] = time.perf_counter() - t
        self.timings["total"] = time.perf_counter() - t0
        report, blocks = self._report(pages, render_info)
        self._stage("done", "Translation ready", 100)
        return {"report": report.model_dump(), "blocks": [b.model_dump() for b in blocks],
                "analysis": self.analysis.model_dump(), "target": target.code}

    # ------------------------------------------------------------------ per page

    def _batches(self, units: list[TextUnit]) -> list[list[TextUnit]]:
        batches: list[list[TextUnit]] = [[]]
        size = 0
        for u in units:
            if batches[-1] and (size + len(u.text) > self.s.batch_max_chars or len(batches[-1]) >= self.s.batch_max_blocks):
                batches.append([])
                size = 0
            batches[-1].append(u)
            size += len(u.text)
        return [b for b in batches if b]

    async def _process_page(self, doc, p: int, units: list[TextUnit], target: Language, n_pages: int) -> None:
        if not units:
            self._page_done(p, n_pages, target)
            return
        for u in units:
            self.blocks[u.id] = BlockState(u)
        drafts: dict[str, str] = {}
        for res in await asyncio.gather(*(self._translate(b, target) for b in self._batches(units))):
            drafts.update(res)
        self.emit({"type": "page_step", "page": p + 1, "step": "translated", "blocks": len(drafts)})
        await self._evaluate(doc, drafts, target, "translate", 0)
        failing = sum(not self._passes(u.id) for u in units)
        self.emit({"type": "page_step", "page": p + 1, "step": "reviewed", "failing": failing})

        for it in range(1, self.s.max_refine_iterations + 1):
            failing = [u.id for u in units if not self._passes(u.id)]
            if not failing:
                break
            self.emit({"type": "refine", "page": p + 1, "iteration": it, "blocks": failing})
            revised = await self._revise(doc, failing, target, escalate=False)
            await self._evaluate(doc, revised, target, "refine", it)

        failing = [u.id for u in units if not self._passes(u.id)]
        if failing and self.s.enable_escalation:
            self.emit({"type": "escalate", "page": p + 1, "blocks": failing})
            revised = await self._revise(doc, failing, target, escalate=True, page=p)
            await self._evaluate(doc, revised, target, "escalate", self.s.max_refine_iterations + 1)
        self._page_done(p, n_pages, target)

    def _page_done(self, p: int, n_pages: int, target: Language) -> None:
        self._done_pages += 1
        states = [b for b in self.blocks.values() if b.unit.page == p and b.best]
        pscore = sum(b.best.score for b in states) / len(states) if states else 100.0
        self.emit({"type": "page", "page": p + 1, "score": round(pscore, 1), "blocks": len(states),
                   "percent": round(12 + 80 * self._done_pages / n_pages, 1)})

    def _passes(self, bid: str) -> bool:
        best = self.blocks[bid].best
        return bool(best) and best.score >= self.s.quality_threshold and best.fit.status != "overflow" and \
            not any(i.severity == "critical" for i in best.issues)

    async def _translate(self, batch: list[TextUnit], target: Language) -> dict[str, str]:
        ids = [u.id for u in batch]
        payload = {"blocks": [{"id": u.id, "text": u.text, "role": u.role, "max_chars": u.max_chars_hint}
                              for u in batch]}
        try:
            res = await self._agent(self.factory.translator(), payload, self._ctx(target, ids))
            return {t.id: t.text for t in res.translations if t.id in ids}
        except AgentRunError as exc:
            if len(batch) > 1:  # split and retry: one bad block should not sink the page
                mid = len(batch) // 2
                left, right = await asyncio.gather(self._translate(batch[:mid], target),
                                                   self._translate(batch[mid:], target))
                return {**left, **right}
            self.emit({"type": "warning", "message": f"Block {batch[0].id} could not be translated: {exc}"})
            self.failed.add(batch[0].id)
            return {batch[0].id: batch[0].text}

    def _fit_tool(self, doc, target: Language) -> Callable[[str, str], dict[str, Any]]:
        def check(block_id: str, text: str) -> dict[str, Any]:
            if block_id not in self.blocks:
                return {"error": f"unknown block id {block_id}"}
            unit = self.blocks[block_id].unit
            fit = doc.measure(unit, text, target)
            ok = fit.status != "overflow"
            res: dict[str, Any] = {"fits": ok, "status": fit.status, "font_scale": fit.scale, "chars": len(text)}
            if not ok:
                res["advice"] = f"Too long for the box: aim for at most ~{int(unit.max_chars_hint * 0.9)} characters."
            return res
        return check

    async def _revise(self, doc, ids: list[str], target: Language, *, escalate: bool, page: int | None = None) -> dict[str, str]:
        out: dict[str, str] = {}
        agent = self.factory.escalation() if escalate else self.factory.refiner()
        images = None
        if escalate and page is not None and not self.factory.pseudo:
            png = await asyncio.to_thread(doc.page_png, page, 110)
            images = ["data:image/png;base64," + base64.b64encode(png).decode()]
        for start in range(0, len(ids), 15):
            chunk = ids[start:start + 15]
            blocks = []
            for bid in chunk:
                st = self.blocks[bid]
                blocks.append({
                    "id": bid, "role": st.unit.role, "source": st.unit.text, "current": st.best.text,
                    "issues": [f"[{i.severity}] {i.message}" + (f" Suggestion: {i.suggestion}" if i.suggestion else "")
                               for i in st.best.issues],
                    "fits": st.best.fit.status != "overflow", "max_chars": st.unit.max_chars_hint,
                })
            ctx = self._ctx(target, chunk)
            ctx.fit_checker = self._fit_tool(doc, target)
            try:
                res = await self._agent(agent, {"blocks": blocks}, ctx, images=images, max_turns=8)
                out.update({t.id: t.text for t in res.translations if t.id in chunk})
                self.failed.difference_update(out)
                self.emit({"type": "tool", "agent": agent.name, "tool": "check_fit", "calls": ctx.fit_checks})
            except AgentRunError as exc:
                self.emit({"type": "warning", "message": f"{agent.name} failed on {len(chunk)} blocks: {exc}"})
        return out

    async def _evaluate(self, doc, candidates: dict[str, str], target: Language, stage: str, iteration: int) -> None:
        if not candidates:
            return
        protected = self.protected
        prepared: dict[str, tuple[Fit, list[Issue]]] = {}
        for bid, text in candidates.items():
            unit = self.blocks[bid].unit
            fit = await asyncio.to_thread(doc.measure, unit, text, target)
            issues = check_block(unit.text, text, target, protected, fit, self.s.min_font_scale)
            if bid in self.failed and text == unit.text:
                issues.insert(0, Issue(origin="check", severity="critical", category="untranslated",
                                       message="The translation service failed for this block; the original text was kept."))
            prepared[bid] = (fit, issues)
        judged = await self._judge(candidates, target) if self.s.enable_judge else {}
        for bid, text in candidates.items():
            fit, issues = prepared[bid]
            issues = issues + judged.get(bid, [])
            self.blocks[bid].consider(Attempt(iteration=iteration, stage=stage, text=text, score=score(issues),
                                              fit=fit, issues=issues))

    async def _judge(self, candidates: dict[str, str], target: Language) -> dict[str, list[Issue]]:
        ids = list(candidates)
        out: dict[str, list[Issue]] = {}
        for start in range(0, len(ids), 30):
            chunk = ids[start:start + 30]
            payload = {"blocks": [{"id": bid, "source": self.blocks[bid].unit.text, "translation": candidates[bid]}
                                  for bid in chunk]}
            try:
                rep = await self._agent(self.factory.judge(), payload, self._ctx(target, chunk))
            except AgentRunError as exc:
                self.emit({"type": "warning", "message": f"Quality judge unavailable for {len(chunk)} blocks: {exc}"})
                continue
            for b in rep.blocks:
                if b.id in candidates:
                    self.judged.add(b.id)
                    out[b.id] = [Issue(origin="judge", severity=e.severity, category=e.category,
                                       message=f"{e.explanation} ({e.span})" if e.span else e.explanation,
                                       suggestion=e.suggestion or None) for e in b.errors]
        return out

    # ------------------------------------------------------------------ report

    def _report(self, pages: list[list[TextUnit]], render_info: dict[str, Any]) -> tuple[QualityReport, list[BlockResult]]:
        blocks: list[BlockResult] = []
        for st in self.blocks.values():
            b = st.best
            if not b:
                continue
            blocks.append(BlockResult(
                id=st.unit.id, page=st.unit.page + 1, source=st.unit.text, translation=b.text, score=b.score,
                fit=b.fit, issues=b.issues, attempts=st.attempts, converged=self._passes(st.unit.id),
                font_preserved=bool(render_info["font_preserved"].get(st.unit.id))))
        blocks.sort(key=lambda b: (b.page, int(b.id.split("-b")[1])))
        weight = lambda b: max(1, len(plain_text(b.source)) + len(CJK_CHAR.findall(b.source)))  # noqa: E731
        total_w = sum(weight(b) for b in blocks) or 1
        doc_score = sum(b.score * weight(b) for b in blocks) / total_w
        sev = {"minor": 0, "major": 0, "critical": 0}
        for b in blocks:
            for i in b.issues:
                sev[i.severity] += 1
        page_reports = []
        for p, units in enumerate(pages):
            pb = [b for b in blocks if b.page == p + 1]
            page_reports.append(PageReport(
                page=p + 1, ocr=any(u.ocr for u in units), blocks=len(pb),
                score=round(sum(b.score for b in pb) / len(pb), 1) if pb else 100.0,
                overflow=sum(b.fit.status == "overflow" for b in pb),
                images_moved=int(p + 1 in render_info["pages_with_moved_images"])))
        s = self.s
        models = {"analyst": s.analyst_model, "translator": s.translator_model, "judge": s.judge_model,
                  "escalation": s.escalation_model} if not self.factory.pseudo else {"all": "pseudo (local, no tokens)"}
        report = QualityReport(
            score=round(doc_score, 1), threshold=s.quality_threshold, passed=doc_score >= s.quality_threshold,
            blocks_total=len(blocks), blocks_below_threshold=sum(not b.converged for b in blocks),
            blocks_refined=sum(any(a.stage == "refine" for a in b.attempts) for b in blocks),
            blocks_escalated=sum(any(a.stage == "escalate" for a in b.attempts) for b in blocks),
            overflow_blocks=sum(b.fit.status == "overflow" for b in blocks),
            shrunk_blocks=sum(b.fit.status == "shrunk" for b in blocks),
            font_preserved_ratio=round(sum(b.font_preserved for b in blocks) / max(1, len(blocks)), 3),
            images_preserved=render_info["images_preserved"], issues_by_severity=sev,
            judge_coverage=round(len(self.judged & {b.id for b in blocks}) / max(1, len(blocks)), 3), pages=page_reports,
            usage_by_agent=self.usage, timings={k: round(v, 2) for k, v in self.timings.items()},
            backend=s.backend, models=models)
        return report, blocks


__all__ = ["TranslationPipeline", "PipelineError", "InvalidDocument", "LANGUAGES"]
