# If I had to start over, what would I do differently?

*Draft written after the first P0 + P1 implementation; to be completed at the end of the project.*

- **Start with the round-trip test, not with the translator.** Re-rendering the original text into its own
  boxes is a free, exact fidelity test. It found every real layout bug (default body margin, line-height vs
  glyph bbox, sideways overflow of long words). I found it on day 1, but only after writing the renderer;
  it should have been the first line of code.
- **Build the fake model on day 0.** Implementing the Agents SDK `Model` interface locally (pseudo-localization)
  made the whole agent stack testable for free and turned layout testing in Arabic and Chinese into a
  zero-token loop. Real tokens were only spent once the plumbing was right.
- **Treat "the service failed" as a first-class outcome in the score.** The first live run with a broken
  key produced a "passed" document that was not translated. Every failure path needs an explicit severity.
- **Do not trust font metadata.** I lost time believing `has_glyph`. Subset fonts must be tested by rendering.
- **Decide on fonts per document before translating**, because fit measurements depend on the font; I first
  decided after translating, which made the measurements and the final render disagree.
- **Check model parameters against the live API early** (`reasoning.effort` values differ between model
  families). A 3-line probe script would have saved a failed run.
- **What I would keep:** code orchestration with agents only where judgement is needed, error lists (MQM)
  instead of scores, a tool that lets the reviser ask the layout engine, and a small adversarial corpus
  where each sample fails in a different way.
