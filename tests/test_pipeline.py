"""End to end with the pseudo model: real Agents SDK runner, guardrails, tool calls, loop and rendering."""

import asyncio

import pymupdf
import pytest

from app.config import Settings
from app.formats import InvalidDocument
from app.pipeline import TranslationPipeline


def run(src, out, target, **overrides):
    events = []
    settings = Settings(llm_backend="pseudo", **overrides)
    res = asyncio.run(TranslationPipeline(settings, events.append).run(src, out, target, src.name))
    return res, events


def test_jargon_sample_protects_brand(samples, tmp_path):
    res, _ = run(samples / "08-jargon-en.pdf", tmp_path / "o.pdf", "es")
    terms = {t["term"] for t in res["analysis"]["protected_terms"]}
    assert {"Stark VARG", "Stark Future"} <= terms
    assert all("Stark" in b["translation"] for b in res["blocks"] if "Stark" in b["source"])
    assert res["report"]["passed"]
    assert pymupdf.open(tmp_path / "o.pdf").page_count == 1


def test_loop_catches_and_fixes_injected_mistakes(samples, tmp_path):
    # the pseudo translator injects a mistake (dropped number / translated brand) in ~1 of 9 blocks
    res, events = run(samples / "02-tight-boxes-en.pdf", tmp_path / "o.pdf", "es")
    refined = [b for b in res["blocks"] if any(a["stage"] == "refine" for a in b["attempts"])]
    assert refined and any(e["type"] == "refine" for e in events)
    for b in refined:
        first, best = b["attempts"][0], b
        assert first["score"] < 100 <= best["score"] or best["fit"]["status"] != "overflow"
    tools = [e for e in events if e["type"] == "tool"]
    assert tools and all(e["tool"] == "check_fit" for e in tools)


@pytest.mark.parametrize("target", ["de", "ar", "zh"])
def test_tight_boxes_never_overflow_silently(samples, tmp_path, target):
    res, _ = run(samples / "02-tight-boxes-en.pdf", tmp_path / "o.pdf", target)
    r = res["report"]
    assert r["images_preserved"]
    for b in res["blocks"]:
        if b["fit"]["status"] == "overflow":
            assert any(i["category"] == "layout" for i in b["issues"]), "overflow must be reported"
    out = pymupdf.open(tmp_path / "o.pdf")
    assert out.page_count == 2


def test_bounded_loop_without_judge(samples, tmp_path):
    res, events = run(samples / "01-simple-en.pdf", tmp_path / "o.pdf", "de", enable_judge=False,
                      max_refine_iterations=1, enable_escalation=False)
    assert sum(e["type"] == "refine" for e in events) <= 1
    assert res["report"]["judge_coverage"] == 0


def test_corrupted_input_is_rejected(samples, tmp_path):
    with pytest.raises(InvalidDocument):
        run(samples / "invalid" / "not-a-pdf.pdf", tmp_path / "o.pdf", "es")
    with pytest.raises(InvalidDocument, match="password"):
        run(samples / "invalid" / "encrypted.pdf", tmp_path / "o.pdf", "es")


def test_repaired_file_warns(samples, tmp_path):
    _, events = run(samples / "invalid" / "truncated.pdf", tmp_path / "o.pdf", "es")
    assert any(e["type"] == "warning" and "repaired" in e["message"] for e in events)


@pytest.mark.parametrize("name,target", [("10-cv-like-en.pdf", "de"), ("10-cv-like-en.pdf", "es"),
                                         ("09-stark-varg-manual-20p.pdf", "es"), ("02-tight-boxes-en.pdf", "de")])
def test_no_overlapping_text(samples, tmp_path, name, target):
    res, _ = run(samples / name, tmp_path / "o.pdf", target)
    r = res["report"]
    assert r["collisions"] == 0 and r["output_overlaps"] == 0, r["fail_reasons"]
    assert r["score"] == min(r["translation_score"], r["layout_score"])


def test_cost_records_and_glyph_stats(samples, tmp_path):
    res, _ = run(samples / "11-subset-heading-en.pdf", tmp_path / "o.pdf", "de")
    stages = {c["stage"] for c in res["calls"]}
    assert {"analysis", "translate", "judge"} <= stages
    assert all(c["input_tokens"] >= 0 and c["stage"] for c in res["calls"])
    assert res["glyphs"]["tokens"] == 0 and "fonts" in res["glyphs"]


def test_redo_applies_font_decision_without_tokens(samples, tmp_path):
    res, _ = run(samples / "09-stark-varg-manual-20p.pdf", tmp_path / "v1.pdf", "es")
    font_items = [i for i in res["review"] if i["kind"] == "font"]
    stark = next(i for i in font_items if i["font"] == "StarkSans-Medium")
    similar = next(o["id"] for o in stark["options"] if o["id"].startswith("similar:"))
    decisions = {"fonts": {"StarkSans-Medium": {"action": similar}}}
    events = []
    settings = Settings(llm_backend="pseudo")
    res2 = asyncio.run(TranslationPipeline(settings, events.append, version=2).redo(
        samples / "09-stark-varg-manual-20p.pdf", tmp_path / "v2.pdf", "es", "m.pdf", res, decisions))
    assert res2["version"] == 2 and res2["calls"] == []  # a font swap is a re-render: zero tokens
    assert res2["glyphs"]["fonts"]["StarkSans-Medium"]["substitute"]
    assert not any(i["kind"] == "font" and i["font"] == "StarkSans-Medium" for i in res2["review"])


def test_redo_edit_and_transliterate(samples, tmp_path):
    from app.review import transliterate

    assert transliterate("ANHÄNGE", {"Ä"}) == "ANHAENGE"
    assert transliterate("Äpfel", {"Ä"}) == "Aepfel"
    res, _ = run(samples / "01-simple-en.pdf", tmp_path / "v1.pdf", "de")
    bid = res["blocks"][2]["id"]
    decisions = {"quality": {bid: {"action": "edit", "text": "Willkommen auf Ihrem neuen Motorrad"}}}
    res2 = asyncio.run(TranslationPipeline(Settings(llm_backend="pseudo"), lambda e: None, version=2).redo(
        samples / "01-simple-en.pdf", tmp_path / "v2.pdf", "de", "s.pdf", res, decisions))
    edited = next(b for b in res2["blocks"] if b["id"] == bid)
    assert edited["translation"] == "Willkommen auf Ihrem neuen Motorrad"
    assert "Willkommen auf Ihrem neuen Motorrad" in pymupdf.open(tmp_path / "v2.pdf")[0].get_text()


def test_prompt_sends_only_terms_of_the_batch():
    """The glossary and protected terms are filtered per batch, whole words only, and come last."""
    from app.ai import prompts
    from app.ai.agents import TranslationContext, _fmt
    from app.languages import get_language
    from app.models import Analysis, Term

    analysis = Analysis(source_language="en", source_language_name="English", tone="formal", domain="CV",
                        summary="A CV.", protected_terms=[Term(term="Stark", kind="brand", reason=""),
                                                          Term(term="CTO", kind="other", reason="")],
                        jargon=[{"term": "holeshot", "translation": "salida", "note": ""},
                                {"term": "berm", "translation": "peralte", "note": ""}])
    ctx = TranslationContext(target=get_language("es"), analysis=analysis,
                             batch_text="Stark riders love the holeshot. The director agreed.")
    text = _fmt(prompts.TRANSLATOR, ctx)
    assert '"Stark"' in text and "holeshot -> salida" in text
    assert "CTO" not in text and "berm" not in text  # "cto" inside "director" is not the term
    assert text.index("DOCUMENT") < text.index("TERMS IN THESE BLOCKS")  # per-call part goes last
    other = _fmt(prompts.TRANSLATOR, TranslationContext(target=get_language("es"), analysis=analysis,
                                                         batch_text="Berms everywhere."))
    prefix = text[:text.index("TERMS IN THESE BLOCKS")]
    assert other.startswith(prefix)  # identical prefix across calls of one job: cacheable


def test_cache_key_per_job_and_agent():
    from app.ai.agents import AgentFactory

    f = AgentFactory(Settings(llm_backend="pseudo"), cache_key="job1")
    assert f.judge().model_settings.extra_args is None  # the local model has no cache
    f.pseudo = False
    assert f.judge().model_settings.extra_args == {"prompt_cache_key": "pdf-translator:job1:judge"}
    assert f.translator().model_settings.extra_args["prompt_cache_key"].endswith(":translator")


def test_judge_lists_only_errors_and_covers_everything(samples, tmp_path):
    res, _ = run(samples / "01-simple-en.pdf", tmp_path / "o.pdf", "de")
    assert res["report"]["judge_coverage"] == 1.0  # blocks the judge left out count as judged correct


def test_chinese_font_is_embedded_once(samples, tmp_path):
    res, _ = run(samples / "02-tight-boxes-en.pdf", tmp_path / "o.pdf", "zh")
    out = pymupdf.open(tmp_path / "o.pdf")
    cjk = {x for p in out for x, *_rest in p.get_fonts(full=True) if "Fallback" in _rest[2] or "CJK" in _rest[2]}
    assert len(cjk) == 1, cjk  # insert_htmlbox embedded one copy per block
    assert (tmp_path / "o.pdf").stat().st_size < 5_000_000
