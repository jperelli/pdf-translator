"""Offline evaluation over the sample corpus (development time).

    python -m evals.run                                  # pseudo backend, zero tokens: layout regressions
    python -m evals.run --backend openai --samples 01 08 --targets es de
    python -m evals.run --backend openai --judge-model gpt-5.5   # spend more on the judge, offline only

Writes evals/results/<timestamp>/{summary.md,summary.json,<sample>.<lang>.pdf,<sample>.<lang>.png}.
The PNG puts page 1 of the original next to the translation for a quick visual check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import pymupdf

from app.ai.setup import configure_sdk
from app.config import Settings
from app.pipeline import TranslationPipeline

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def side_by_side(src: Path, out: Path, png: Path, page: int = 0) -> None:
    a = pymupdf.open(src)[page].get_pixmap(dpi=60)
    b = pymupdf.open(out)[page].get_pixmap(dpi=60)
    canvas = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, a.width + b.width + 12, max(a.height, b.height)), False)
    canvas.clear_with(235)
    canvas.copy(pymupdf.Pixmap(pymupdf.csRGB, a), a.irect)
    b2 = pymupdf.Pixmap(pymupdf.csRGB, b)
    b2.set_origin(a.width + 12, 0)
    canvas.copy(b2, b2.irect)
    canvas.save(png)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["pseudo", "openai"], default="pseudo")
    ap.add_argument("--samples", nargs="*", help="sample name prefixes (default: all)")
    ap.add_argument("--targets", nargs="*", help="override the targets listed in samples/manifest.json")
    ap.add_argument("--judge-model")
    ap.add_argument("--translator-model")
    args = ap.parse_args()

    overrides = {"llm_backend": args.backend}
    if args.judge_model:
        overrides["judge_model"] = args.judge_model
    if args.translator_model:
        overrides["translator_model"] = args.translator_model
    settings = Settings(**overrides)
    configure_sdk(settings)
    manifest = json.loads((SAMPLES / "manifest.json").read_text())
    outdir = ROOT / "evals" / "results" / time.strftime("%Y%m%d-%H%M%S")
    outdir.mkdir(parents=True)

    rows = []
    for name, meta in manifest.items():
        if args.samples and not any(name.startswith(p) for p in args.samples):
            continue
        for target in args.targets or meta["targets"]:
            src = SAMPLES / name
            out = outdir / f"{src.stem}.{target}.pdf"
            warnings: list[str] = []
            sink = lambda e: e["type"] == "warning" and warnings.append(e["message"])  # noqa: E731
            t = time.perf_counter()
            try:
                res = await TranslationPipeline(settings, sink, job_id=f"eval-{src.stem}-{target}").run(src, out, target, name)
            except Exception as exc:  # an eval run reports failures, it does not stop
                rows.append({"sample": name, "target": target, "error": f"{type(exc).__name__}: {exc}"})
                print(f"{name:32} {target}  ERROR {exc}")
                continue
            r = res["report"]
            side_by_side(src, out, outdir / f"{src.stem}.{target}.png")
            tokens = sum(u["input_tokens"] + u["output_tokens"] for u in r["usage_by_agent"].values())
            row = {"sample": name, "target": target, "score": r["score"], "passed": r["passed"],
                   "blocks": r["blocks_total"], "refined": r["blocks_refined"], "escalated": r["blocks_escalated"],
                   "below": r["blocks_below_threshold"], "overflow": r["overflow_blocks"], "shrunk": r["shrunk_blocks"],
                   "fonts_kept": r["font_preserved_ratio"], "images_ok": r["images_preserved"],
                   "issues": r["issues_by_severity"], "tokens": tokens, "seconds": round(time.perf_counter() - t, 1),
                   "warnings": warnings}
            rows.append(row)
            print(f"{name:32} {target}  score {row['score']:5.1f}  blocks {row['blocks']:3}  refined {row['refined']:2}  "
                  f"overflow {row['overflow']}  tokens {tokens:6}  {row['seconds']}s")

    (outdir / "summary.json").write_text(json.dumps({"settings": overrides, "rows": rows}, indent=2, ensure_ascii=False))
    lines = [f"# Eval run {outdir.name} ({args.backend})", "",
             "| sample | target | score | blocks | refined | escalated | overflow | shrunk | images | tokens | s |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['sample']} | {r['target']} | ERROR: {r['error']} |||||||||")
        else:
            lines.append(f"| {r['sample']} | {r['target']} | {r['score']} | {r['blocks']} | {r['refined']} | "
                         f"{r['escalated']} | {r['overflow']} | {r['shrunk']} | {'ok' if r['images_ok'] else 'moved'} | "
                         f"{r['tokens']} | {r['seconds']} |")
    (outdir / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"\nresults: {outdir}")


if __name__ == "__main__":
    asyncio.run(main())
