# Research: faster and cheaper translations without losing quality

Date: 2026-10-28.

Status:

- **Implemented:** levers 2 (glossary per batch, capped analysis, terse judge), 4 (cache-friendly prompts,
  cache key per job and agent, cached tokens recorded) and 7 (one CJK font for the whole document).
- **Not implemented yet:** levers 1, 3, 5 and 6.

Measured results are in the "Results" section at the end.

## Where the time and money go today (measured)

The reference run is the CV in production: 4 pages, 85 blocks, Quality mode (formerly "publish"), real models.
Data from the job's per-call records; costs at list prices.

| Stage | Model | Input tok | Output tok | Cost | Share | Model time |
|---|---|---:|---:|---:|---:|---:|
| Judge | gpt-5.4 | 21,154 | 8,783 | $0.185 | **57%** | 132 s (10 requests) |
| Escalation | gpt-5.5 | 8,701 | 1,003 | $0.074 | **23%** | 14 s (2) |
| Refine | gpt-5.4-mini | 24,320 | 2,122 | $0.028 | 9% | 18 s (11) |
| Translate | gpt-5.4-mini | 9,837 | 3,024 | $0.021 | 6% | 16 s (4) |
| Analysis | gpt-5.4-mini | 3,502 | 3,148 | $0.017 | 5% | 17 s (1) |
| **Total** | | 67,514 | 18,080 | **$0.324** | | **112 s wall clock** |

What the numbers show:

- **The judge is most of the cost.** It is the most expensive model per block, and it is called again for every
  revised block (113 judgements for 85 blocks). Its output (~78 tokens per judged block, including reasoning
  at `low` effort) costs more than its input.
- **Cached input tokens: 0.** Prompt caching saved nothing.
- **The glossary is oversized.** The analysis produced **117 "jargon" terms for a CV** (most are job titles kept
  as is). That costs 3,148 output tokens and 17 s before any translation starts. The glossary is then pasted into
  the instructions of *every* call: about 870 tokens × 27 calls, roughly a third of all input tokens.
- **Wall time is a chain of round trips.** Pages run in parallel, but each page is sequential: translate →
  judge → refine → judge → (escalate → judge). A judge request takes about 13 s.
- **Not much repetition to exploit within a document:**
  - Stark manual: 6% of characters are repeated blocks (running headers like "Stark VARG Owners Manual" ×16).
  - CV: 1%.
  - Re-running the same document (a re-upload, a redo, a new target language with the same glossary) repeats
    100% of the source.
- **Non-LLM time is small for Latin scripts** (render 1.5 s here). For Chinese the render dominates: 48 s of
  53 s on the 20-page manual, because MuPDF reloads the CJK fallback font for every block.

## The levers

Ranked by expected impact.

**"Risk" means risk to translation quality.** Every lever marked medium or high must pass the evaluation
described at the end before it ships.

### 1. Use newer, cheaper models where we pay most

The API key has access to models newer than the ones we use (checked with `GET /v1/models`). List prices
per 1M tokens, input / output ([pricing][pricing]):

| Role | Today | Candidate | Change |
|---|---|---|---|
| Judge (Quality) | gpt-5.4 $2.50 / $15 | **gpt-6.1-terra** $1 / $5 | −60% input, −67% output |
| Escalation | gpt-5.5 $5 / $30 | **gpt-6.1-sol** $2 / $10 | −60%, −67% |
| Translate / refine | gpt-5.4-mini $0.75 / $4.50 | gpt-6-luna $0.10 / $0.50 | −87%, −89% (quality unknown) |
| | | gpt-6.1-terra $1 / $5 | +33% input, +11% output |

- **What the vendor says:** OpenAI describes GPT-6.1 Terra as "a fast, efficient model for … clear, natural
  writing", with structured outputs, prompt caching, `reasoning.effort` from `none` upward, and Flex/Batch
  support ([model page][terra]).
- **Impact on the CV run:** judge on Terra + escalation on 6.1 Sol alone: **$0.324 → $0.158 (−51%)** with the
  same token counts.
- **Possible speed gain:** smaller, newer models usually generate faster ([latency guide][latency]), so judge
  and escalation latency may drop as well. To be measured.
- **Risk: medium.** A weaker judge misses errors silently, which is the worst failure for this product. Swap
  only if Terra agrees with the current judge on our corpus (see evaluation).

### 2. Send less: glossary per batch, a capped analysis, a terse judge

These change no model and should not change quality. They remove tokens that carry no information.

- **Glossary per batch:**
  - Today: every call gets the whole glossary and protected-term list.
  - Change: pass only the terms that occur in the blocks of that batch. Most batches use a handful of the
    117 terms.
  - Impact: about −30% of all input tokens on the CV.
- **Capped analysis:**
  - Today: the Analyst lists every proper noun as jargon.
  - Change: ask only for terms a generic translator would get wrong (the prompt's original intent), cap the
    list (e.g. 30), and put keep-as-is names in `protected_terms` without notes.
  - Impact: analysis output from 3,148 to about 1,000 tokens, so roughly **17 s → 6 s** of waiting before the
    first translation (output tokens dominate latency, [latency guide][latency]).
- **Terse judge:**
  - Today: every block gets an entry, with free-text explanations, at reasoning effort `low`.
  - Change: list only blocks with errors, keep the explanation under ~15 words, and evaluate reasoning effort
    `none` on the candidate model.
  - Impact: judge output roughly halved. Halving output tokens roughly halves latency ([latency
    guide][latency]).
- **Combined with lever 1:** **≈ $0.107 (−67%)**, an estimate.
- **Risk: low.** The terse judge needs the same evaluation as lever 1, because reasoning effort affects
  judgement.

### 3. Fewer sequential round trips

Wall time is set by the longest page chain, not by total tokens.

- **Smaller judge requests in parallel:** chunks of ~10 blocks instead of 30. Same tokens, more parallelism,
  each request finishes sooner. Rate limits are not a concern (5,000 RPM on the lowest tier for Terra,
  [model page][terra]).
- **Escalate earlier:** when the first judgement has a *critical* error, go straight to the strong model
  instead of a refine round first. One round trip less on the worst blocks.
- **One refine round in Quality instead of two, if the data supports it.** The eval harness already records
  `translate 75 → refine 100` histories, so we can measure how often round 2 changes the outcome before
  dropping it.
- **Don't re-judge purely mechanical fixes:**
  - When the Refiner only shortened a text to fit, and deterministic checks still pass, its own `check_fit`
    tool already verified the fit.
  - Medium risk: shortening can drop meaning, so this is limited to small length reductions.
- **Risk:** low for the first two, medium for the last two.

### 4. Make prompt caching actually hit

Facts from the docs ([prompt caching][caching]):

- Caching is automatic for a matching **prefix**.
- On our current models (before GPT-5.6) the minimum cacheable length "varies by request settings", and a
  stable `prompt_cache_key` helps routing.
- On GPT-5.6 and later the minimum is 1,024 visible tokens, cached reads cost 0.1× input (0.05× on 6.1 Terra
  and Sol), and writes cost 1.25× for a 30-minute lifetime.

What to change:

- Keep the static instructions first and byte-identical across calls.
- Put per-document context (analysis, glossary) next, and per-batch content (blocks) last.
- Set `prompt_cache_key = <job id>:<agent>`.
- Avoid firing all first requests of a stage at the same instant: the first call writes the cache, the next
  ones can read it.

Impact: modest. Input is about a third of our cost and the per-document prefix is small, so expect around
5–10% after lever 2 shrinks the prefix. It is free and does not touch quality. On 6.x models a prefix used
only once costs 1.25×, so it only pays when several calls share it, as ours do.

**Risk:** none.

### 5. Translation memory

Store `(source text, source lang, target lang, glossary hash) → accepted translation + judgement` in SQLite.

- **Within a document:** running headers and footers are translated and judged once (6% on the manual).
- **Across runs:** a re-upload of the same CV, a redo, or a newer version of a manual reuses everything that
  did not change. Your CV re-run after the font fix would have cost nearly nothing in LLM calls.
- **Risk: low**, if the key includes the glossary and protected-term decisions, and the context-sensitive
  short labels (≤ 3 words) are excluded.

### 6. Pay less per token when speed is not needed (Flex, Batch)

- **Flex processing** (`service_tier="flex"`): Batch prices (−50%) on synchronous calls, in exchange for slower
  responses and occasional `429 Resource Unavailable` (not charged), with the advice to retry or fall back to
  standard ([flex][flex]). Supported by our models and the 6.x ones ([pricing][pricing]).
- **Batch API:** the same −50%, but results come within a time window. Only good for a "deliver later" option.
- **Where they fit:**
  - They make things cheaper, not faster.
  - An **"Economy" option for big documents**, or for running the offline evals.
  - Not the default interactive path.
- **Risk:** none to quality; slower.

### 7. Non-LLM speed: the Chinese render

- **Problem:** 48 s of 53 s for the 20-page manual into Chinese. MuPDF reloads the CJK fallback font for every
  `insert_htmlbox`.
- **Fix:** embed one CJK font through our font archive, so it is loaded once, instead of relying on the
  fallback.
- **Impact and risk:** affects only CJK targets; no quality risk.

## Summary

| # | Lever | Cost | Speed | Quality risk |
|---|---|---|---|---|
| 1 | Judge → gpt-6.1-terra, escalation → gpt-6.1-sol | −51% (measured tokens, list prices) | likely faster | medium: needs eval |
| 2 | Glossary per batch, capped analysis, terse judge | to ≈ −67% with #1 (estimate) | −10 s before translating; shorter judge calls | low |
| 3 | Parallel small judge chunks, escalate earlier, fewer rounds | small | biggest wall-time gain | low to medium |
| 4 | Prompt caching (stable prefix, cache key) | −5–10% | small | none |
| 5 | Translation memory | 6% within a doc; up to ~100% on re-runs | same | low |
| 6 | Flex / Batch as an Economy option | −50% | slower | none |
| 7 | One CJK font in the archive | 0 | −40 s on 20 pages of Chinese | none |
| (C) | Also translator/refiner → gpt-6-luna | to ≈ −77% (estimate) | likely faster | **high**: only if eval proves it |

## How to make sure quality does not drop

Before shipping lever 1, the terse judge, or any "fewer rounds" change:

1. **Corpus:** samples 02, 03, 08, 10, 12, the 20-page manual and the CV structure, into German, Spanish, Arabic
   and Chinese.
2. **Reference:** translate with today's configuration and judge with a fixed, stronger judge that is not a
   candidate (gpt-5.5, or 6.1-sol at higher effort). Store per-block MQM errors.
3. **Candidates:** for each configuration, run the same corpus and judge it with the *same* reference judge:
   - MQM score per sample and per language;
   - number of critical/major errors;
   - layout score;
   - tokens, cost, wall time.
4. **For judge swaps, measure the judge itself:** precision and recall of the candidate judge's flagged blocks
   against the reference judge's flags. A cheaper judge that misses majors is not acceptable even if scores
   look fine.
5. **Accept** when MQM is within noise (two runs of the baseline give the noise level), there are no new
   critical errors, and the judge keeps ≥ 90% recall on major/critical errors.

`evals/run.py` already runs the corpus and writes summaries; it needs a `--judge-model` override (it has one for
the dev judge) and an agreement report. Estimated eval cost: a few dollars per configuration at today's
prices.

## Suggested order

1. **Levers 2, 4 and 7:** no quality risk, quick.
2. **The evaluation harness changes, then lever 1** (judge + escalation swap) if it passes.
3. **Lever 3** (parallel judge chunks, escalate earlier), measured on wall time.
4. **Lever 5** (translation memory).
5. **Lever 6** (Economy option), and luna for translation only if the eval says so.

## Results (levers 2, 4 and 7)

Same samples, Quality mode, real models. The current code and the previous commit (a git worktree) ran
side by side:

| Sample | Cost before → after | Wall time before → after | Score before → after |
|---|---|---|---|
| 10 CV-like → de | $0.0407 → $0.0238 | 42 s → 22 s | 95.4 → 97.1 |
| 08 jargon → es | $0.0216 → $0.0121 | 21 s → 13 s | 100 → 91 (noise, see below) |
| 03 two columns → de | $0.0246 → $0.0248 | 14 s → 17 s | 97.3 → 95.8 |
| **Total** | **$0.087 → $0.061 (−30%)** | **78 s → 52 s (−33%)** | |

- **Judge output on the CV:** 1,693 → 583 tokens.
- **Prompt cache:** the only hit was 1,152 of 1,355 analysis input tokens on the CV. It came from running
  the same document with the same job id minutes earlier (the benchmark was repeated), not from calls
  within one run. Within a run, the prefixes of these small documents stay below the cacheable minimum.
  Caching pays on re-runs and long documents; the saving above comes from levers 2 and 7.
- **Noise on sample 08:** three more runs each gave 91, 91, 98.5 before and 92.5, 100, 92.5 after. The
  variation comes from one block (`p1-b4`) that the judge flags differently each time, so the 91 is
  run-to-run noise.
- **Not caused by this change:** in both versions, a block with one *major* judge error is sometimes accepted
  without a refine round. That deserves its own look.
- **Chinese, 20-page manual (demo model):**

  | | Before | After |
  |---|---|---|
  | Render | 45 s | 2.4 s |
  | Peak memory | 1.1 GB | 390 MB |
  | Score | 99.3 | 99.3 |

[pricing]: https://platform.openai.com/docs/pricing
[terra]: https://platform.openai.com/docs/models/gpt-6.1-terra
[caching]: https://platform.openai.com/docs/guides/prompt-caching
[flex]: https://platform.openai.com/docs/guides/flex-processing
[latency]: https://platform.openai.com/docs/guides/latency-optimization
