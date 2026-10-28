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

---

## 2. Runtime prompts

Status: version 1 of every prompt, written with the rationale below and validated on the first live eval
runs (scores 95-100 on 02/03/05/06 into DE/AR/ZH/EN, see README). No prompt iteration has been driven by a
failed live run yet; when one is, it is logged here with the before/after.

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

### Refiner and Escalation

- Gets source, current translation, the concrete issues (from checks and judge, with suggestions) and
  whether it fits. "Fix every listed issue without introducing new ones; keep what was already right."
- Must call `check_fit` before answering, at most twice per candidate (bounded tool loop, `max_turns=8`).
- Escalation adds the page image and asks to use it to understand what each block *is* (button, header,
  warning). This is my "if it does not converge, use the image" idea, implemented as vision context for a
  stronger model rather than OCR, because the text layer was fine and the missing information was context.
