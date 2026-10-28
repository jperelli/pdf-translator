# PROMPTS.md

Two kinds of prompts live here:

1. **Development prompts**: what I asked the coding agent (Devin, running Claude) while building this,
   in order, with what came out of each and what went wrong. Original wording kept (Spanish/English mix).
2. **Runtime prompts**: the prompts the product sends to OpenAI models, with the iterations they went through.
   The source of truth is `app/ai/prompts.py`.

---

## 1. Development prompts

### 1.1 Research and plan

> hagamos research. tengo este problema para resolver que esta como adjunto en el archivo .md. quiero ordenar
> las siguientes ideas en un plan y/o por prioridad para desarrollar un prototipo. haz un reporte en html
>
> usar pdfs para testear: happy path = simple, dificiles chiquitos edge cases idiomas a traducir tipo ingles
> aleman arabe chino, por que elegimos estos? porque son diferentes entre si [...] aleman caracteres latinos
> umlaut etc, arabe ltr, chino kanji en vez de silabas, evalua si hay algunos mejores para testear, la idea es
> que puede ser que haya problemas de overflow de texto que se salga de las cajas al traducir, queremos que el
> pdf quede igual en estructura en lo posible, con la misma tipografia e imagenes posicionadas igual. El ultimo
> ejemplo tiene que ser un manual de una moto stark
>
> duda: como evaluar/eval? como saber que calidad tiene la traduccion? [...] sacar screenshots y comparar?
> opcional, modelo adversario para controlar la calidad del texto producido en el segundo idioma
> control de calidad, offline u online? [...] implementar loop con evaluador? iterar varias veces hasta llegar
> a la quality de threshold, con un break de iteraciones
> opcionales: seleccionar tipo de traduccion: tecnica, formal, etc [...] jerga-detection [...]
> fase 1 detectar estilo y jerga, fase 2 traducir, fase 3 presentar ui con las decisiones tomadas y tunning
> moto stark = moto fuerte? esto seria una mala traduccion [...]
> adversarial si la traduccion es mala y esta en el loop de refinamiento, si no converge, el paso ultimo seria
> pasar imagen y convertir via ocr

**Result:** an HTML report with priorities P0-P3. Key findings it brought back: PDFMathTranslate as the
state-of-the-art reference for layout-preserving PDF translation (AGPL, so learn from it, do not vendor it);
GEMBA-MQM (LLM-as-judge with an error rubric) as the practical evaluation method; W3C/IBM expansion numbers
(short strings +200-300%); overflow should be detected *geometrically*, not with screenshots; brands and
jargon are different problems ("Stark" is a hard rule, jargon is a user choice). It also corrected me: I wrote
"arabe ltr", Arabic is RTL.

### 1.2 Report readability

> fix the colors of the report to be light instead of dark, there is too much contrast its killing my eyes

**Result:** light, low-contrast palette. Same preference carried into the app UI (light theme with the Stark
gold accent instead of the black Stark website look).

### 1.3 Build P0 + P1

> ok, make a first implementation P0 and P1. to eval, samples, use short pdfs max 5 pages, for the stark
> manual, get the first 20 pages only, that way we develop with not so big data. push the samples to the repo.
> I have an openai api key we need to use for the llm/agent part of the runtime, so make it an env variable.
> configure the repository to deploy it in coolify.

> in /workspace/apikey we have the openaiapikey that we can use for the llm/agent part of the application
> when deployed, and also for developing now

**What happened, honestly, including the dead ends:**

- **Rendering spike first.** Before any agent code, the agent tested MuPDF's HTML engine
  (`insert_htmlbox`) with German, Arabic and Chinese: HarfBuzz shaping, RTL and CJK fallback fonts all work
  out of the box. That removed the biggest risk of the plan (Arabic RTL) on day one.
- **Arabic sample, failed attempt.** Generating the Arabic source sample with MuPDF produced a broken text
  layer (presentation forms, no spaces): useless as a *source*. Switched the generator to WeasyPrint, which
  produces the kind of text layer real Arabic PDFs have, including the classic scrambled lam-alef ligatures,
  which then went into the translator prompt as "translate meaning, not artifacts".
- **OCR, failed attempt.** MuPDF's built-in OCR text page lost all spaces and read noise out of the
  illustration. Replaced with Tesseract TSV output (word confidences, paragraphs), dropping low-confidence
  words and splitting lines at large gaps so text inside photos does not merge with captions.
- **Round-trip test as the fidelity baseline.** Idea: write the *original* text back into its own boxes;
  anything that does not fit is a bug in the layout engine, not in the translation. First run: almost
  everything "expanded" or "overflowed". Causes found one by one:
  - MuPDF's default `<body>` margin (12 px top and bottom) inside every box;
  - glyph bboxes are shorter than CSS line boxes, fixed with a half-leading "layout box";
  - fonts falling back to wider generic faces.
  After the fixes: 100% "fit" on every sample, now a pytest.
- **Subset fonts lie.** Reusing the manual's embedded fonts rendered "quali ed" and "speci ed": MuPDF always
  applies the `fi` ligature and the subset's ligature glyph is empty; `has_glyph` also says yes for accented
  letters whose outlines were stripped. Zero-width joiners and friends do not stop MuPDF's ligatures (tried 5).
  Final approach: test ink by rendering, test ligatures explicitly, decide per font before translating.
- **Overflow false negatives.** The pseudo German "Wéíghtung" was reported to fit a 25 pt cell: MuPDF's
  `Story.place` does not report a single word wider than the box. Fixed by checking the filled width.
  Rounded buttons were not detected as frames (they are curves, not rectangles): any filled shape is a frame now.
- **A photo detected as a scan.** The manual's full-page bike photo was sent to OCR (6 s for nothing). Scans
  are mostly paper-white; photos are not, so a brightness test was added.
- **Pseudo model instead of mocks.** To test the agents without spending tokens, the agent implemented the
  Agents SDK `Model` interface locally (pseudo-localization + deterministic injected mistakes + real tool
  calls). This caught real bugs: the target-language regex failing on "Chinese (Simplified) (zh)", and the
  script check marking "STARK FUTURE" (all protected terms) as untranslated.
- **First live run, two failures surfaced by the system itself:**
  - the SDK read the key from the process env, not from our settings: every call failed, and the report
    still said "passed 90.8" because blocks that kept their source text only got a soft penalty. Fixed both
    (`set_default_openai_key`, failed blocks are now critical);
  - `reasoning.effort = "minimal"` is rejected (400) by the gpt-5.4/5.5 family, which accepts `none`. In that
    run the escalation step (gpt-5.5) rescued the whole document, which was a nice proof that the loop works.
- **Dependency pinning.** Versions published less than 7 days earlier (openai, fastapi, pytest) were pinned
  to the previous releases.

### 1.4 Deploy

> carga la variable OPENAI_API_KEY y deploya el repo en coolify, dame la url final

**Result:** Coolify application from the public repo (Dockerfile build pack, health check `/api/health`),
key as a runtime-only variable, deployed at https://pdf-translator.coffee.jperelli.com and checked with a
real translation.

### 1.5 "Overlapping text should be unacceptable": honest evaluation, missing glyphs, review loop

> I need to understand how we are doing the evaluation, because for example I see this translation with a lot
> of overlap in text, and that should be unacceptable. I send you also an example where it seems one
> typography does not have special characters [...] Should we create the special characters that dont exist
> in that typography somehow? [...] should we flag that and propose different alternatives at the end? [...]
> Either way, overlapping text is not acceptable. [...] we need that final step of warnings/things that didnt
> work and be able to choose some alternatives and redo with those decitions

> ok, write that in a plan to implement things [...] how expensive the "Build accented letters from pieces of
> the same font." is in terms of adding that by default [...] the end screen could be a stats screen [...]
> a table of the previous runs [...] the log of the run [...] in a tab in the final screen [...] download
> button on the top and the quality score [...] tabs: preview, details (jargon, refinement, stats, log),
> review

> Headline score show the lower; similar-font have both, if the bundled set works, use it, if not go to google
> fonts to get one at runtime; add sqlite for persistence; use the real cv as a sample just change the name and
> address, pii, leave the photo; hardcode current list prices yes. with that, start implementing the plan

**Diagnosis (with evidence, before changing anything):**
- The evaluation only *predicted* each block's fit in isolation and never looked at the written PDF. Two
  blocks could grow into the same space, separators and underlines were not obstacles, and paragraphs split
  by a symbol-font arrow were placed as separate fragments. A document with overlapping text scored ~100.
- The "ANHÄNGE" bug was reproduced on the manual's StarkSans subset: the glyph test rendered each character
  with `insert_text` and looked for ink, but PyMuPDF falls back to another font for missing glyphs, so the
  test said yes even for a Chinese character. MuPDF then mixed a serif Ä into the word.

**What was built:** extraction fixes (spaces between word objects, `<br>` for intentional line breaks,
separators end paragraphs, underlines become `<u>` and move with their text, duplicate text layers dropped),
space claims in reading order, MuPDF probes per block (real line boxes and fonts), collision detection on
probes *and* on the written PDF, two scores with a hard gate, a fontTools font rebuilder that builds accents
from the font's own pieces (no LLM), similar-font ranking (bundled OFL fonts, Google Fonts fallback), a
Review tab with decisions and a redo that only reprocesses affected blocks, SQLite history and versions,
per-call token/cost accounting with list prices, a Stats tab with SVG charts, and the understand/publish
purpose.

**Mistakes and dead ends on the way:**
- First run of the new layout QA: 32 "collisions" on the manual. Most were false: line boxes include the
  full ascender/descender and neighbouring lines touch by a point or two. Fixed by requiring a real share of
  the line height. Two more classes of false positives followed: zero-height hairlines are "empty"
  rectangles for PyMuPDF (so the underline exemption failed), and in Arabic the tall Naskh line boxes of one
  paragraph overlap each other. Each one was found by looking at the flagged area, not by loosening limits.
- Real ones remained: TOC page numbers share a "line" with their entry (split by a tab) and were not
  obstacles, so a long Spanish entry ran over "20". Fixed by using line segments as obstacles. The read-back
  of the written PDF is what caught it: the prediction had missed it.
- A `round` parameter shadowed Python's `round()` inside the cost recorder; every agent call raised inside
  the usage callback, was retried and split, and the run "succeeded" with 37 translator requests for 19
  blocks. Visible in the trace counts, fixed by renaming.
- The pseudo model accented every Latin target with the same letters (ñ, ç in German), which created fake
  "missing glyph" warnings; it now only uses letters the target language has.
- Similar-font ranking first took 11 s (decoding every glyph of Noto with fontTools) and penalised weight
  so much that a wide display face (Michroma) lost to Montserrat for the wide StarkSans caps. Now only the
  sample glyphs are decoded (0.3 s) and width/shape dominate.
- The real CV PDF was not available in the sandbox (only a screenshot), so a synthetic `10-cv-like-en`
  reproduces its structure. The anonymised real CV is pending.

### 1.6 Observability and job queue research, then a Huey queue

> ok, now i want observability into all of this. Lets research alternatives: Can we use grafana and store all
> tempo spans for this? also what can we use to store errors? [...] and for ui usage/screen recording?

> what alternatives are there as queues for this, so that each pdf conversion goes into a job in a queue? it
> should be one that we can get the progress from and we could keep it simple with sqlite as backend. Also for
> event communication between app and queue can we use the same sqlite? [...]

> i think huey would be the best. I'd directly use the events table [...]

> si, implementalo

**Result:** two research notes with primary sources (`docs/research-observability.md`,
`docs/research-job-queue.md`), then the queue: `SqliteHuey` tasks run by `huey_consumer` process workers, all
progress written to an `events` table, and the SSE endpoint tailing it, checking `PRAGMA data_version` before
each query.

**What went wrong on the way:**
- The first lost-worker detection used a heartbeat thread. The test that kills a worker mid-render showed the
  retry being declared dead too: a single MuPDF call held the GIL for 47 s (measured), so a live worker
  looked dead. Detection now checks whether the worker process (host, pid, start time) still exists; the
  heartbeat is only a fallback.
- Huey's process workers did not shut down on SIGTERM with the default signal settings (exceptions in the
  workers, consumer left hanging), which would have made every container stop wait for Docker's kill.
  Fixed with `--graceful-signal TERM --shutdown-timeout 5`.
- `UPDATE ... RETURNING` in autocommit mode keeps the statement, and the write lock, open until the cursor is
  fully read: the claim uses `fetchall()`.

### 1.7 "My CV is in Ubuntu Sans and the translation comes out in a serif font"

> my cv has a typography ubuntu sans, when translating i don't understand why it's getting changed to a serif
> typography and the looks is totally different.

**Diagnosis** (on the production run's output PDF, which still embeds the CV's original font for the
untranslated text):
1. The CV's `Ubuntu-Regular` is a simple TrueType subset whose own cmap maps only codes 1..81 to glyphs; the
   letters live in the PDF's `ToUnicode`. The font rebuilder read only the font's cmap, saw 0 characters, and
   dropped the font: no block could reuse it.
2. The generic fallback was chosen from MuPDF's "serif" span flag. The descriptor only says `/Flags 4`
   (symbolic) and the subset has no OS/2 table, MuPDF reports that as serif, and "Ubuntu" has no "sans" in
   its name: every block went to Charis SIL.
3. A third bug surfaced while testing the fix: a substitute label with spaces ("Open Sans 700 (bundled)")
   ended up in a CSS `url()`, which broke the whole stylesheet and also produced Charis.

**Fix:** rebuild the cmap from `ToUnicode`; complete a subset with the full font of its own family (bundled,
else Google Fonts); classify serif/sans/monospace from the outlines; sanitise archive file names. A synthetic
sample (`12-subset-unicode-en.pdf`) reproduces the CV's font structure without its personal data. On the
real font: loaded (78 characters), measured as sans, completed from Google Fonts' Ubuntu in 0.8 s.

---

## 2. Runtime prompts

Status: version 1 of every prompt validated on the first live eval runs (scores 95-100 on 02/03/05/06 into
DE/AR/ZH/EN); version 2 adds line breaks, underlines and user instructions (see below), validated live on
10-cv-like and 11-subset-heading into German (97.2 and 99.2, no overlaps).

Version 3 (October 2026) reorders every prompt for prompt caching and cost:

1. the agent's fixed rules;
2. a `DOCUMENT` section (domain, tone, style, summary), identical within a job;
3. a `TERMS IN THESE BLOCKS` section with only the protected terms and glossary entries found in that call's
   blocks.

The Analyst lists at most 30 error-prone terms, most important first. The Judge reports only the blocks
with errors, with explanations of at most 15 words. Measured live: −30% cost and −33% time on three
samples, with scores within the run-to-run noise.

All are in `app/ai/prompts.py`; placeholders like `{target_name}` are filled per run from the typed run
context (target language + document analysis). Inputs are JSON; outputs are strict JSON schemas
(`app/ai/schemas.py`), so the prompts never describe the output format in prose.

### Analyst (once per document)

Goal: everything a translator needs that is not in a single page. Design decisions:

- It receives a **round-robin sample over all pages** (first block of every page, then the second...), not
  the first N characters, so a 20-page manual is understood as a whole for ~4 k tokens.
- **Brand vs dictionary word** is called out explicitly with the Stark example, because it is the failure
  mode the user cares about most ("moto Stark ≠ moto fuerte").
- "List multi-word names and their parts if they also appear alone": the deterministic check looks for exact
  surface forms, so "Stark" alone must be listed even when "Stark VARG" is.
- "Only list terms that actually occur": the glossary is injected into every call, so padding costs tokens
  on every page.

### Translator (per page, batched)

- Rules are numbered and short; the protected list and glossary are injected from the analysis.
- `max_chars` is a **soft budget** ("never drop information to save space"): a hard limit risks the model
  dropping qualifiers in safety text. Overflow is handled by the layout ladder and the Refiner, not by
  mutilating the first draft.
- `role` (heading/label/body/caption) keeps headings short and preserves ALL CAPS styling.
- Extraction artifacts rule (hyphenation, scrambled Arabic ligatures, numbers out of place): added after
  looking at the Arabic sample's text layer.
- "If a block is already in the target language, return it unchanged": mixed-language documents.

### Judge (MQM)

- GEMBA-MQM style: list errors with severity and category instead of a 1-10 score. Error lists are
  more stable than scores and give the Refiner something concrete to fix.
- Critical severity is defined to include *safety information wrong* and *protected term translated*:
  domain-specific stakes for an owner's manual.
- "Do not invent errors: a different wording from what you would write is not an error": LLM judges tend
  to flag preferential rewrites as errors; this keeps scores calibrated.
- Layout is excluded ("checked elsewhere") so the judge does not double-penalize long text.
- A different and stronger model than the translator, to reduce self-preference bias.

### Translator and Refiner, v2 (layout round)

- Added: keep `<u>` tags and every `<br>` line break (addresses, lists, a summary written line by line).
  Before, extraction merged those lines and the translator had no way to know they were separate.
- Added to the Refiner: blocks may carry an `instruction` from the user's review decisions ("use ONLY
  these characters", "keep this term", "make it noticeably shorter"). It overrides the style rules but not
  the meaning. The allowed character set for "rewrite with available characters" is the rebuilt font's real
  coverage, so the model cannot pick a letter the font does not have.

### Refiner and Escalation

- Gets source, current translation, the concrete issues (from checks and judge, with suggestions) and
  whether it fits. "Fix every listed issue without introducing new ones; keep what was already right."
- Must call `check_fit` before answering, at most twice per candidate (bounded tool loop, `max_turns=8`).
- Escalation adds the page image and asks to use it to understand what each block *is* (button, header,
  warning). This is my "if it does not converge, use the image" idea, implemented as vision context for a
  stronger model rather than OCR, because the text layer was fine and the missing information was context.
