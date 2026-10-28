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
    CallRecord,
    QualityReport,
    Term,
    TextUnit,
    Usage,
)
from app.quality.checks import check_block, score
from app.quality.layout import LAYOUT_CATEGORIES
from app.review import build_items, resolve_candidate, transliterate

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
    def __init__(self, settings: Settings, emit: Emit, job_id: str = "local", purpose: str = "fast",
                 version: int = 1):
        self.purpose = purpose
        self.version = version
        settings = settings.for_purpose(purpose)
        self.s = settings
        self.emit = emit
        self.job_id = job_id
        self.factory = AgentFactory(settings, cache_key=job_id)
        self.usage: dict[str, Usage] = {}
        self.timings: dict[str, float] = {}
        self.llm = asyncio.Semaphore(max(2, settings.max_concurrent_pages * 2))
        self.blocks: dict[str, BlockState] = {}
        self.analysis: Analysis | None = None
        self._done_pages = 0
        self.failed: set[str] = set()
        self.judged: set[str] = set()
        self.calls: list[CallRecord] = []
        self.page_sizes: list[tuple[float, float]] = []
        self.counters: dict[str, float] = {}

    # ------------------------------------------------------------------ helpers

    def _stage(self, stage: str, message: str, percent: float) -> None:
        self.emit({"type": "stage", "stage": stage, "message": message, "percent": round(percent, 1)})

    def _ctx(self, target: Language, ids: list[str] | None = None) -> TranslationContext:
        """`ids`: the blocks of this call; only the protected terms and glossary entries found in their source
        text go into the prompt."""
        text = "\n".join(self.blocks[i].unit.text for i in ids if i in self.blocks) if ids else None
        return TranslationContext(target=target, analysis=self.analysis, expected_ids=ids or [], batch_text=text)

    @property
    def protected(self) -> list[str]:
        return sorted({t.term for t in self.analysis.protected_terms}) if self.analysis else []

    async def _agent(self, agent, payload, ctx, *, stage: str, page: int | None = None, rnd: int = 0,
                     blocks: int = 0, **kw):
        model = agent.model if isinstance(agent.model, str) else "pseudo"

        def record(u: Usage, seconds: float, _ok: bool) -> None:
            pin, pout, pcached = self.s.price(model)
            cached = min(u.cached_tokens, u.input_tokens)
            self.calls.append(CallRecord(
                agent=agent.name, model=model, stage=stage, page=page, round=rnd, blocks=blocks,
                requests=u.requests, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                cached_tokens=cached,
                cost_usd=round(((u.input_tokens - cached) * pin + cached * pcached + u.output_tokens * pout) / 1e6, 6),
                seconds=round(seconds, 2), version=self.version))

        async with self.llm:
            return await run_agent(agent, payload, ctx, self.usage, timeout=self.s.llm_timeout_seconds * 2,
                                   on_usage=record, **kw)

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
        self._configure_fonts(doc)
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
            result = await self._agent(self.factory.analyst(), {"blocks": sample}, self._ctx(target),
                                       stage="analysis", blocks=len(sample))
            self.analysis = Analysis.model_validate(result.model_dump())
            self.analysis.jargon = self.analysis.jargon[:30]  # the prompt asks for at most 30, most error-prone first
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

        return await self._finish(doc, pages, out, target, t0)

    def _configure_fonts(self, doc) -> None:
        if hasattr(doc, "font_downloads"):
            doc.font_cache, doc.font_downloads = self.s.data_dir / "fonts", self.s.font_downloads

    async def _finish(self, doc, pages: list[list[TextUnit]], out: Path, target: Language, t0: float,
                      decisions: dict[str, Any] | None = None) -> dict[str, Any]:
        self.page_sizes = await asyncio.to_thread(doc.page_sizes)
        self._stage("render", "Rebuilding the PDF with the original layout", 92)
        t = time.perf_counter()
        final = {bid: b.best.text for bid, b in self.blocks.items() if b.best}
        render_info = await asyncio.to_thread(doc.render, final, target, out)
        self.timings["render"] = time.perf_counter() - t
        self._apply_final_layout(render_info)
        report, blocks = self._report(pages, render_info)
        self._stage("review", "Preparing the review of what did not work", 97)
        styles = {u.style.font: ((doc.fonts.generic(u.style.font) if doc.fonts else None) or u.style.family,
                                 u.style.bold) for units in pages for u in units}
        t = time.perf_counter()
        items = await asyncio.to_thread(build_items, blocks, self.analysis, doc.fonts, self.s.quality_threshold,
                                        self.purpose, self.s.data_dir / "fonts", styles)
        self.counters["review_s"] = round(time.perf_counter() - t, 2)
        self.timings["total"] = time.perf_counter() - t0
        report.timings = {k: round(v, 2) for k, v in self.timings.items()}
        self._stage("done", "Translation ready", 100)
        return {"report": report.model_dump(), "blocks": [b.model_dump() for b in blocks],
                "analysis": self.analysis.model_dump(), "target": target.code,
                "calls": [c.model_dump() for c in self.calls], "counters": self.counters,
                "glyphs": render_info.get("glyphs", {}), "purpose": self.purpose, "version": self.version,
                "review": items, "decisions": decisions or {}}

    # ------------------------------------------------------------------ redo with review decisions

    async def redo(self, src: Path, out: Path, target_code: str, filename: str, previous: dict[str, Any],
                   decisions: dict[str, Any]) -> dict[str, Any]:
        target = get_language(target_code)
        t0 = time.perf_counter()
        with trace(f"Redo {filename} -> {target.code} v{self.version}", group_id=self.job_id,
                   metadata={"job_id": self.job_id, "version": str(self.version)}):
            self._stage("validate", "Reopening the document", 2)
            fmt = detect(filename, src.read_bytes()[:2048])
            doc = await asyncio.to_thread(fmt.load, src, max_pages=self.s.max_pages,
                                          ocr_language=self.s.ocr_language, min_font_scale=self.s.min_font_scale)
            self._configure_fonts(doc)
            try:
                return await self._redo(doc, out, target, previous, decisions, t0)
            finally:
                doc.close()

    async def _redo(self, doc, out: Path, target: Language, previous: dict[str, Any], decisions: dict[str, Any],
                    t0: float) -> dict[str, Any]:
        n = doc.page_count
        pages = await asyncio.to_thread(lambda: [doc.units(p) for p in range(n)])
        self.emit({"type": "extracted", "pages": n, "blocks": sum(map(len, pages)), "scanned_pages": []})
        self.analysis = Analysis.model_validate(previous["analysis"])
        terms = decisions.get("terms") or {}
        keep = [t for t in terms.get("keep_original", []) if t]
        translate = [t for t in terms.get("translate", []) if t]
        self.analysis.jargon = [j for j in self.analysis.jargon if j.term not in keep]
        self.analysis.protected_terms = [t for t in self.analysis.protected_terms if t.term not in translate]
        self.analysis.protected_terms += [Term(term=t, kind="other", reason="kept by the user") for t in keep]
        await asyncio.to_thread(doc.prepare, target)

        prev = {b["id"]: BlockResult.model_validate(b) for b in previous["blocks"]}
        for units in pages:
            for u in units:
                st = self.blocks[u.id] = BlockState(u)
                if u.id in prev:
                    pb = prev[u.id]
                    st.consider(Attempt(iteration=0, stage="previous", text=pb.translation, score=pb.score, fit=pb.fit,
                                        issues=[i for i in pb.issues if i.category not in LAYOUT_CATEGORIES]))
        self._stage("translate", "Applying your decisions", 20)
        direct: dict[str, str] = {}
        instructions: dict[str, str] = {}
        strong: list[str] = []
        accepted: set[str] = set()

        for key in ("collision", "layout"):
            d = decisions.get(key) or {}
            ids = [i for i in d.get("blocks", []) if i in self.blocks]
            if d.get("action") == "shorten":
                for i in ids:
                    instructions[i] = "Make it noticeably shorter so it fits its box without overlapping anything."
            elif d.get("action") == "shrink":
                for i in ids:
                    doc.min_scale_overrides[i] = 0.5
            elif d.get("action") == "accept":
                accepted |= set(ids)
        for bid, d in (decisions.get("quality") or {}).items():
            if bid not in self.blocks:
                continue
            if d.get("action") == "retry_strong":
                strong.append(bid)
            elif d.get("action") == "edit" and d.get("text", "").strip():
                direct[bid] = d["text"]
            elif d.get("action") == "accept":
                accepted.add(bid)
        for font, d in (decisions.get("fonts") or {}).items():
            action = d.get("action", "")
            ids = [bid for bid, st in self.blocks.items() if st.unit.style.font == font and st.best]
            if action.startswith("similar:"):
                path = resolve_candidate(action, self.s.data_dir / "fonts")
                if path and doc.fonts and doc.fonts.override(font, path):
                    self.emit({"type": "log", "message": f'Font "{font}" replaced by {path.stem} in every block'})
            elif action == "transliterate" and doc.fonts:
                for bid in ids:
                    miss = doc.fonts.missing(font, self.blocks[bid].best.text)
                    if miss:
                        direct[bid] = transliterate(self.blocks[bid].best.text, miss)
            elif action == "rewrite" and doc.fonts and font in doc.fonts.fonts:
                allowed = "".join(sorted(chr(c) for c in doc.fonts.fonts[font].cmap if chr(c).isprintable()))
                for bid in ids:
                    if doc.fonts.missing(font, self.blocks[bid].best.text):
                        instructions[bid] = (f"Use ONLY these characters (the font has no others): {allowed}. "
                                             "Choose other words rather than dropping letters.")
        for t in keep + translate:
            what = f'Keep "{t}" exactly as in the source (do not translate it).' if t in keep else \
                f'"{t}" is not a brand name here: translate it.'
            for bid, st in self.blocks.items():
                if t.lower() in plain_text(st.unit.text).lower():
                    instructions[bid] = (instructions.get(bid, "") + " " + what).strip()

        self.emit({"type": "redo", "direct": len(direct), "agent": len(instructions), "strong": len(strong),
                   "shrink": len(doc.min_scale_overrides), "fonts": list((decisions.get("fonts") or {}).keys())})
        for bid, text in direct.items():
            st = self.blocks[bid]
            fit = await asyncio.to_thread(doc.measure, st.unit, text, target)
            issues = check_block(st.unit.text, text, target, self.protected, fit, self.s.min_font_scale)
            st.attempts.append(Attempt(iteration=1, stage="edit", text=text, score=score(issues), fit=fit, issues=issues))
            st.best = st.attempts[-1]  # the user's choice wins
        revised: dict[str, str] = {}
        if instructions:
            revised.update(await self._revise(doc, list(instructions), target, escalate=False, stage="redo",
                                              extra=instructions, round=1))
        if strong:
            revised.update(await self._revise(doc, strong, target, escalate=True, stage="redo", round=1))
        if revised:
            await self._evaluate(doc, revised, target, "redo", 1)
        self._stage("translate", "Laying out every page again", 70)
        for p in range(n):
            await self._layout_round(doc, p, target)
        for bid in accepted:
            best = self.blocks[bid].best
            if best:
                for i in best.issues:
                    if i.category in ("collision", "layout") and "(accepted)" not in i.message:
                        i.severity, i.message = "minor", i.message + " (accepted)"
                best.score = score(best.issues)
        return await self._finish(doc, pages, out, target, t0, decisions)

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
        await self._layout_round(doc, p, target)
        failing = sum(not self._passes(u.id) for u in units)
        self.emit({"type": "page_step", "page": p + 1, "step": "reviewed", "failing": failing})

        for it in range(1, self.s.max_refine_iterations + 1):
            failing = [u.id for u in units if not self._passes(u.id)]
            if not failing:
                break
            self.emit({"type": "refine", "page": p + 1, "iteration": it, "blocks": failing})
            revised = await self._revise(doc, failing, target, escalate=False, round=it)
            await self._evaluate(doc, revised, target, "refine", it)
            await self._layout_round(doc, p, target)

        failing = [u.id for u in units if not self._passes(u.id)]
        if failing and self.s.enable_escalation:
            self.emit({"type": "escalate", "page": p + 1, "blocks": failing})
            revised = await self._revise(doc, failing, target, escalate=True, page=p,
                                         round=self.s.max_refine_iterations + 1)
            await self._evaluate(doc, revised, target, "escalate", self.s.max_refine_iterations + 1)
            await self._layout_round(doc, p, target)
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
            res = await self._agent(self.factory.translator(), payload, self._ctx(target, ids), stage="translate",
                                    page=batch[0].page + 1, blocks=len(batch))
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

    async def _revise(self, doc, ids: list[str], target: Language, *, escalate: bool, page: int | None = None,
                      round: int = 0, stage: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
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
                if extra and bid in extra:
                    blocks[-1]["instruction"] = extra[bid]
            ctx = self._ctx(target, chunk)
            ctx.fit_checker = self._fit_tool(doc, target)
            try:
                res = await self._agent(agent, {"blocks": blocks}, ctx, images=images, max_turns=8,
                                        stage=stage or ("escalate" if escalate else "refine"),
                                        page=self.blocks[chunk[0]].unit.page + 1, rnd=round, blocks=len(chunk))
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
                rep = await self._agent(self.factory.judge(), payload, self._ctx(target, chunk), stage="judge",
                                        page=self.blocks[chunk[0]].unit.page + 1, blocks=len(chunk))
            except AgentRunError as exc:
                self.emit({"type": "warning", "message": f"Quality judge unavailable for {len(chunk)} blocks: {exc}"})
                continue
            self.judged.update(chunk)  # the judge lists only blocks with errors: the rest were judged correct
            for b in rep.blocks:
                if b.id in candidates and b.errors:
                    out[b.id] = [Issue(origin="judge", severity=e.severity, category=e.category,
                                       message=f"{e.explanation} ({e.span})" if e.span else e.explanation,
                                       suggestion=e.suggestion or None) for e in b.errors]
        return out

    # ------------------------------------------------------------------ layout

    def _rescore(self, st: BlockState, layout_issues: list[Issue], fit: Fit | None = None) -> None:
        best = st.best
        if best is None:
            return
        if any("(accepted)" in i.message for i in best.issues):
            for i in layout_issues:
                if i.category in ("collision", "layout"):
                    i.severity, i.message = "minor", i.message + " (accepted)"
        best.issues = [i for i in best.issues if i.category not in LAYOUT_CATEGORIES] + layout_issues
        if fit is not None:
            best.fit = fit
        best.score = score(best.issues)

    async def _layout_round(self, doc, p: int, target: Language) -> None:
        """Lay the page out with the current best texts; collisions and font problems become issues."""
        texts = {bid: st.best.text for bid, st in self.blocks.items() if st.unit.page == p and st.best}
        if not texts:
            return
        t = time.perf_counter()
        layout = await asyncio.to_thread(doc.layout_page, p, texts, target)
        self.counters["layout_qa_s"] = self.counters.get("layout_qa_s", 0) + time.perf_counter() - t
        for bid, (fit, _probe, issues) in layout.items():
            self._rescore(self.blocks[bid], issues, fit)
        collisions = sum(any(i.category == "collision" for i in iss) for _f, _p, iss in layout.values())
        if collisions:
            self.emit({"type": "layout", "page": p + 1, "collisions": collisions})

    def _apply_final_layout(self, render_info: dict[str, Any]) -> None:
        for bid, issues in render_info["layout_issues"].items():
            self._rescore(self.blocks[bid], issues, render_info["fits"].get(bid))

    # ------------------------------------------------------------------ report

    def _report(self, pages: list[list[TextUnit]], render_info: dict[str, Any]) -> tuple[QualityReport, list[BlockResult]]:
        blocks: list[BlockResult] = []
        for st in self.blocks.values():
            b = st.best
            if not b:
                continue
            text_issues = [i for i in b.issues if i.category not in LAYOUT_CATEGORIES]
            lay_issues = [i for i in b.issues if i.category in LAYOUT_CATEGORIES]
            blocks.append(BlockResult(
                id=st.unit.id, page=st.unit.page + 1, source=st.unit.text, translation=b.text, score=b.score,
                translation_score=score(text_issues), layout_score=score(lay_issues), font=st.unit.style.font,
                fit=b.fit, issues=b.issues, attempts=st.attempts, converged=self._passes(st.unit.id),
                font_preserved=bool(render_info["font_preserved"].get(st.unit.id))))
        blocks.sort(key=lambda b: (b.page, int(b.id.split("-b")[1])))
        weight = lambda b: max(1, len(plain_text(b.source)) + len(CJK_CHAR.findall(b.source)))  # noqa: E731
        total_w = sum(weight(b) for b in blocks) or 1
        wmean = lambda key: sum(getattr(b, key) * weight(b) for b in blocks) / total_w  # noqa: E731
        collisions = sum(any(i.category == "collision" for i in b.issues) for b in blocks)
        overlaps = sum(render_info.get("output_overlaps", {}).values())
        translation_score = wmean("translation_score")
        # every collision costs 5 points at document level: one overlap is enough to make a page unusable
        layout_score = max(0.0, wmean("layout_score") - 5 * collisions - 5 * overlaps)
        if not render_info["images_preserved"]:
            layout_score = max(0.0, layout_score - 10)
        headline = min(translation_score, layout_score)
        reasons = []
        if collisions or overlaps:
            reasons.append(f"{collisions + overlaps} overlapping text block(s)")
        if translation_score < self.s.quality_threshold:
            reasons.append(f"translation quality {translation_score:.0f} < {self.s.quality_threshold:.0f}")
        if layout_score < self.s.quality_threshold:
            reasons.append(f"layout fidelity {layout_score:.0f} < {self.s.quality_threshold:.0f}")
        untranslated = [b for b in blocks if any("service failed" in i.message for i in b.issues)]
        if untranslated:
            reasons.append(f"{len(untranslated)} block(s) left untranslated")
        sev = {"minor": 0, "major": 0, "critical": 0}
        for b in blocks:
            for i in b.issues:
                sev[i.severity] += 1
        page_reports = []
        for p, units in enumerate(pages):
            pb = [b for b in blocks if b.page == p + 1]
            avg = lambda key: round(sum(getattr(b, key) for b in pb) / len(pb), 1) if pb else 100.0  # noqa: E731
            w, h = self.page_sizes[p] if p < len(self.page_sizes) else (0, 0)
            page_reports.append(PageReport(
                page=p + 1, width=w, height=h, ocr=any(u.ocr for u in units), blocks=len(pb), score=avg("score"),
                translation_score=avg("translation_score"), layout_score=avg("layout_score"),
                overflow=sum(b.fit.status == "overflow" for b in pb),
                collisions=sum(any(i.category == "collision" for i in b.issues) for b in pb)
                + render_info.get("output_overlaps", {}).get(p + 1, 0),
                images_moved=int(p + 1 in render_info["pages_with_moved_images"])))
        s = self.s
        models = {"analyst": s.analyst_model, "translator": s.translator_model, "judge": s.judge_model,
                  "escalation": s.escalation_model} if not self.factory.pseudo else {"all": "pseudo (local, no tokens)"}
        has = lambda cat: sum(any(i.category == cat for i in b.issues) for b in blocks)  # noqa: E731
        report = QualityReport(
            score=round(headline, 1), translation_score=round(translation_score, 1),
            layout_score=round(layout_score, 1), threshold=s.quality_threshold, passed=not reasons,
            fail_reasons=reasons, collisions=collisions, output_overlaps=overlaps,
            missing_glyph_blocks=has("missing_glyphs"), font_substituted_blocks=has("font_substituted"),
            purpose=self.purpose,
            blocks_total=len(blocks), blocks_below_threshold=sum(not b.converged for b in blocks),
            blocks_refined=sum(any(a.stage == "refine" for a in b.attempts) for b in blocks),
            blocks_escalated=sum(any(a.stage == "escalate" for a in b.attempts) for b in blocks),
            overflow_blocks=sum(b.fit.status == "overflow" for b in blocks),
            shrunk_blocks=sum(b.fit.status == "shrunk" for b in blocks),
            font_preserved_ratio=round(sum(b.font_preserved for b in blocks) / max(1, len(blocks)), 3),
            images_preserved=render_info["images_preserved"], issues_by_severity=sev,
            judge_coverage=round(len(self.judged & {b.id for b in blocks}) / max(1, len(blocks)), 3),
            pages=page_reports, usage_by_agent=self.usage,
            timings={k: round(v, 2) for k, v in self.timings.items()}, backend=s.backend, models=models)
        return report, blocks


__all__ = ["TranslationPipeline", "PipelineError", "InvalidDocument", "LANGUAGES"]
