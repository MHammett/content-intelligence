# Prompts

One file per review domain, mapped in `pipeline._DOMAIN_PROMPTS`:

| File | Domain | Report section |
|---|---|---|
| `fact_check.txt` | `fact_check` | Section 2: Factual Verification |
| `ai_speak.txt` | `voice_style` | Section 3: Voice and AI-Speak |
| `argument_integrity.txt` | `argument_integrity` | Section 4: Argument Integrity |
| `completeness.txt` | `completeness` | Section 5: Completeness and Framing |
| `red_team.txt` | `red_team` | Section 6: Red Team Findings |

**Every byte of a `.txt` file here is sent to the model.** `pipeline._load_prompt()`
does `path.read_text()` and passes the result straight into the request — there is
no comment syntax and nothing is stripped. Do not add editorial notes, rationale,
or TODOs inside a `.txt` prompt; they become tokens on every call in that domain,
on every run. Notes go in this file or in `docs/`.

Nothing globs this directory — only the five filenames named in `_DOMAIN_PROMPTS`
are ever read — so sibling files like this one are safe.

## Notes on `ai_speak.txt`

- The drafting model is excluded from this domain, and only this one
  (`pipeline._DRAFTER_EXCLUDED_DOMAINS`). A model under-reports its own habits.
  The exclusion list should only grow on evidence that a domain is actually
  compromised — widening it costs real review coverage.
- **Commercial AI-detectors were evaluated as a complement or replacement for
  this pass and rejected** (2026-09-05). Short version: the handoff already
  declares `Drafted with:`, so a document-level detector score carries no
  information; passage-level scores are the weakest thing detectors do, are not
  calibrated probabilities, and penalize the surface signature of technical
  rigor; and one Pangram 4 call on a 10,000-word draft costs more than the entire
  five-domain, six-model review. Full evidence, the numbers, and the specific
  triggers that would reverse the decision: [`docs/AI-DETECTORS.md`](../../../../../docs/AI-DETECTORS.md).
- Cross-article recurrence of the flags this prompt produces is handled by
  `voice_pattern_report.py`, not by this prompt.
