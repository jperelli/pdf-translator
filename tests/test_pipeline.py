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
