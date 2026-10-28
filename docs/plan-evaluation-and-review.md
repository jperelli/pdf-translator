# Plan: honest layout evaluation, missing glyphs, run history and the review/redo loop

Status: implemented (Oct 28), except the anonymised real CV sample, which needs the original PDF. A synthetic `samples/10-cv-like-en.pdf` reproduces its structure meanwhile.

Triggers:
- A CV translation scored high while text overlapped other text, separators and underlines.
- The Stark manual heading "ANHÄNGE" was rendered with a serif "Ä" inside the StarkSans font.

Principle: **overlapping text is never acceptable**, whatever the purpose of the translation. Missing glyphs
and other fidelity problems are warnings the user can resolve in a second pass.

---

## Phase 1. Evaluation that inspects the rendered PDF (highest priority)

### 1.1 Root causes to fix (what the current evaluation misses)

| # | Problem | Today | Fix |
|---|---|---|---|
| a | Blocks checked one at a time; two blocks grow into the same free space | each block "fits" alone | reserve space: a grown box becomes an obstacle for later blocks (sequential allocation, reading order) |
| b | Vector lines (separators, underlines, frames) are not obstacles | text crosses them | add drawings (lines, thin rects) to the obstacles; underlines are attached to their text unit (moved or removed with it) |
| c | Paragraphs split into several units by an inline symbol font or a gap (the CV's "15 → 5") | fragments placed at their old positions collide with the reflowed paragraph | do not break a line on font change alone; only on real gaps / tabs; merge symbol-font spans into the paragraph |
| d | Hard line breaks and list structure lost (CV summary) | lines merged into one paragraph | keep a line break when a line ends well before the right edge (ragged), emit `<br>`; keep list items as separate units |
| e | Missing spaces when words are separate spans ("JulianPerelli") | spans concatenated | insert a space when the gap between spans is > ~0.15 em and neither side has one |
| f | Glyph test is a false positive (renderer fallback leaves ink, even for Chinese in a Latin font) | font marked usable, MuPDF mixes fonts per glyph | detect missing glyphs by reading back the fonts actually used (1.2), and by checking the glyph outline is non-empty in the font file |
| g | Judge sees text only, score mixes everything | layout problems drown in the average | two scores + hard gate (1.3) |

### 1.2 Post-render layout QA (deterministic, zero tokens)

Runs on the output PDF after every render (including re-renders in the review loop):

1. **Collisions.**
   - For each unit, record the real ink rect: the box used by `insert_htmlbox`, minus the spare height it
     reports.
   - Check each rect against other units, untouched original text (numbers, codes), images and vector lines.
   - Any intersection above a tolerance (for example 1 pt) is a **critical** `collision` issue on both units.
2. **Font fallback.**
   - Read back the output spans of each unit. If the unit ends up with more than one font family, or with a
     family other than the planned one, raise a **warning** `missing_glyphs`.
   - Record the font and the missing characters (for example `StarkSans-Medium: Ä`).
3. **Structure.** Lost line breaks or list items, and a change in the number of lines of headings, raise a
   **warning** `structure`.
4. **Shrink.** A font scaled below the threshold raises a **warning** (already exists, moves into this report).
5. **Optional, later: visual diff.** Render the original and the output with text masked and compare the
   non-text pixels, to catch graphics covered by painted rectangles (scanned pages).

Collisions feed back into the existing loop like overflow does. In order:
1. shrink further within limits;
2. ask the Refiner for a shorter text (`check_fit` will also report collisions);
3. reflow inside the section's free area.

If a collision still remains, the block is left as critical and goes to the Review tab.

### 1.3 Scores and gate

- **Translation quality** (judge + text checks, as today, 0-100).
- **Layout fidelity** (0-100): collisions (critical), fallback fonts, shrink, lost structure, images moved.
- **Passed** requires: no collisions, translation quality ≥ threshold, no untranslated blocks. The UI shows
  both scores; the top shows the lower one as the headline, with the reason.

### 1.4 Purpose of the translation (chosen at upload)

| | Understand (draft) | Publish (professional) |
|---|---|---|
| Collisions | critical, blocked | critical, blocked |
| Mixed fonts / fallback glyphs | warning, fallback allowed | warning, auto-fix attempted (Phase 2), listed for review |
| Font shrink | down to 60% | down to 80%, then rewrite shorter |
| Judge | cheaper (judge model = translator model) | stronger judge |
| Escalation | off | on |

### 1.5 Tests

- New regression samples:
  - **CV-like:** two-column header, word-per-span text, symbol-font arrows, separators and underlines. Built
    synthetically; the real CV stays out of the repo, or goes in only as an anonymised copy.
  - **Heading in a subset font** missing umlauts.
- Assertions: zero collisions after render for every sample × target in pseudo mode, and the fallback
  warning raised for "ANHÄNGE".

---

## Phase 2. Missing characters

### 2.1 Cost of "build accented letters from pieces of the same font"

- **It uses no tokens and no LLM.** It is plain font editing with fontTools (already installed as a WeasyPrint
  dependency; it would become a runtime dependency).
- An accented letter is a base glyph plus a mark: Ä = A + dieresis, é = e + acute, ñ = n + tilde.
- Measured on the manual's fonts: parsing each font takes 0-18 ms. Building a glyph is copying and
  transforming two outlines, so well under a millisecond per glyph. A whole document needs a handful of
  glyphs per font.
- **It can run by default.** The stats screen will still show: glyphs built, per font, the time spent, and
  "0 tokens".

What it can and cannot do, checked on the real fonts:
- `StarkSans-Medium` (the heading font) is a bare CFF subset with 40 glyphs: capitals and digits, `period`,
  no `dieresis`, no `acute`, no lowercase.
  - **Ä Ö Ü can be built** from A/O/U plus two scaled periods.
  - **É Ñ cannot** (there is no acute or tilde shape to borrow).
- The `UniversalSansDisplay-*` subsets list 499 glyph names, including `dieresis` and `acute`, but most
  outlines were stripped. The builder must check that each piece has real contours before using it.

Algorithm:
1. NFD-decompose each missing character into base + combining mark.
2. Find the base glyph and a mark source, in this order:
   1. the spacing mark glyph (`dieresis`, `acute`, `grave`, `circumflex`, `tilde`, `ring`, `cedilla`);
   2. a derived shape: dieresis from two `period`s, acute or grave from a rotated `period` or `quotesingle`;
   3. none: give up.
3. Place the mark centred on the base's bounding box, above the cap height (capitals) or x-height
   (lowercase), with the font's own spacing.
4. Write the glyph:
   - TrueType: as a composite glyph;
   - CFF: by drawing the transformed outlines with a pen.
5. Add it to the cmap, then swap the font buffer in the FontBook archive.
6. Re-run the coverage check. Anything not built goes to the next option.

### 2.2 Other options (offered in Review, not automatic)

| Option | Tokens | Notes |
|---|---|---|
| Similar complete font for the whole font family in the document | 0 | Bundled open fonts (Archivo, Montserrat, Inter, Michroma, Noto Sans/Serif/Mono…). Rank them by rendering the same available letters and comparing width, cap height, x-height and stroke weight. Show the top 3 with previews. Never mix fonts inside a word. |
| Rewrite using only available characters | small (Refiner calls for the affected blocks only) | The allowed character set is passed to the Refiner; the judge verifies the meaning ("APPENDIXES" → "ANLAGEN"). Best for short headings and labels. |
| Transliterate (Ä → AE) | 0 | Only on explicit choice; acceptable for all-caps headings in some contexts. |
| Keep the fallback | 0 | Draft mode default. |
| LLM-drawn glyphs | high, unreliable | Not planned. Mismatched shapes next to professionally designed glyphs, and no way to verify them. |

---

## Phase 3. Cost and stats accounting

Data model additions (per job, saved in `result.json`):

- Every agent call records:
  - **agent**, **page**, **block ids**;
  - **stage**: `analysis`, `translate`, `judge`, `refine` (with round number), `escalate`, `review_redo`;
  - input/output tokens, duration.
- Tokens are converted to an **estimated cost** using a price table per model in config (USD per 1M input /
  output tokens), editable by env var.
- Non-LLM work also records counts and time: OCR, layout QA, glyph building (glyphs built per font, ms),
  render.

Aggregates:
- per document;
- per page;
- per stage:
  - evaluation = judge;
  - back and forth = refine + escalate rounds;
  - first draft = translate;
  - analysis;
- per agent / model;
- per review redo (version 2, 3…).

---

## Phase 4. UI restructure

### 4.1 Final screen layout

Top bar:
- file name, target language, version (v1, v2…);
- **quality score**: headline plus the two sub-scores (translation, layout);
- **Download** button (primary);
- "New translation".

The stat tiles at the top are removed. Below that, top-level tabs:

1. **Preview**: side-by-side pages, as today.
   - Adds: a toggle to outline units with issues (red = collision, amber = warning) and click-to-jump to the
     block in Details.
2. **Details**, with sub-tabs:
   - **Jargon**: today's "Decisions": language, tone, domain, protected terms, glossary.
   - **Refinement**: today's "Blocks & issues": per block source, translation, score, issues and attempt
     history (`translate 75 → refine 100`).
   - **Stats**: today's "Cost & trace" plus charts, all plain SVG (no build step, no CDN needed):
     - tokens and estimated cost by stage (stacked bar: analysis / translate / judge / refine / escalate /
       redo);
     - tokens per page (bar chart, each bar split by stage);
     - tokens and requests per agent and model (table);
     - non-LLM work: OCR time, glyphs built (count, fonts, ms, "0 tokens"), render time, layout QA;
     - timeline of the run (stages over time);
     - totals for the document and for each version.
   - **Log**: the same activity log shown during progress, replayed from the stored events, with filters
     (stages, agents, warnings, trace spans).
3. **Review** (badge with the number of open items): the decisions to redo with.

### 4.2 Review tab

Groups, each with a count, affected pages and blocks, and the choices.

1. **Collisions (critical)**. Per block or for all:
   - shrink to fit;
   - shorten (Refiner);
   - allow reflow of the section;
   - (draft only) accept.
2. **Missing characters (warning)**. Per font, for example `StarkSans-Medium: Ä Ö Ü, 6 blocks`:
   - built automatically (shows the result and lets you undo);
   - use a similar font (top 3 previews);
   - rewrite with available characters;
   - transliterate;
   - keep the fallback.
3. **Low quality blocks** (below threshold, or judge errors still open):
   - retry with the stronger model;
   - edit manually (inline text field);
   - accept.
4. **Terms**: the P2 glossary approval. Per jargon term, translate or keep the original; per protected term,
   keep or allow translation.
5. **Structure / shrink warnings**: accept, or fix.

Then an **"Apply and redo"** button:
- It creates a new version of the job and reprocesses **only the affected blocks**:
  - font, transliteration and shrink choices: re-render only, no tokens;
  - term or rewrite choices: Refiner/Translator for those blocks only, then judge and layout QA on them.
- It shows the estimated token cost before running (from the block sizes and the price table).
- When finished, the final screen shows the new version, with a v1/v2 switch in Preview and the delta in
  Stats.

### 4.3 Run history screen

- New route `/#history` and a "History" link in the header.
- Table of previous runs: date, file, target, purpose, version count, quality / layout score, pages, tokens,
  estimated cost, status, actions:
  - open (the final screen);
  - download the latest or any version;
  - delete.
- Backed by `data/jobs/*/result.json` plus a small index file (or SQLite) so it survives restarts.
- Retention: keep `JOB_TTL_HOURS`, with "pin" to keep a run.
- Needs **persistent storage** in Coolify: a volume on `/data`, which the current deployment does not have.

---

## Phase 5. API changes

- `GET /api/jobs?limit=&offset=`: history list.
- `GET /api/jobs/{id}`: adds `versions`, `stats`, `review_items`.
- `GET /api/jobs/{id}/events`: unchanged (the Log tab replays it).
- `POST /api/jobs/{id}/redo`: body is the decisions JSON. Returns the new version id and streams progress
  over the same SSE endpoint.
- `GET /api/jobs/{id}/versions/{v}/download`.
- `DELETE /api/jobs/{id}`.
- `POST /api/jobs` adds `purpose=understand|publish`.

---

## Implementation order and rough size

| Step | Content | Size |
|---|---|---|
| 1 | Extraction fixes (spaces, hard breaks, no split on font change, underline binding) + regression samples | M |
| 2 | Post-render layout QA (collisions, font readback, structure) + two scores + gate | M |
| 3 | Space reservation and drawings as obstacles; collisions fed into the loop | M |
| 4 | Correct glyph detection (outline check) | S |
| 5 | Per-call accounting (stage, page, tokens, cost) + non-LLM counters | S |
| 6 | Final screen restructure: top bar, Preview / Details (Jargon, Refinement, Stats with SVG charts, Log) | M |
| 7 | Glyph builder (fontTools), on by default, reported in Stats | M |
| 8 | Run history screen + persistence + Coolify volume on `/data` | S-M |
| 9 | Review tab + decisions model + `redo` endpoint, re-render-only path first | L |
| 10 | Review options that call agents: rewrite with allowed characters, shorten, terms, similar-font ranking | M |
| 11 | Purpose selector (understand / publish) wiring the thresholds | S |

Steps 1 to 4 fix the honesty of the evaluation and can ship on their own. Steps 5 and 6 make the cost
visible. Steps 7 to 11 are the review loop.

## Open questions

1. Headline score: show the lower of translation and layout, or always both side by side?
2. Similar-font candidates: a bundled set (offline, predictable) or a download from Google Fonts at runtime?
3. History persistence: a JSON index is enough for one instance. Do you want SQLite from the start?
4. Should the real CV be added as an anonymised sample, or should I only build a synthetic CV-like sample?
5. Price table: OK to hardcode the current list prices per model in config, overridable by env?
