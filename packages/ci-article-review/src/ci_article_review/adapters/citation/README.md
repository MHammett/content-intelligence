# Citation Resolver

Traces factual claims from the fact-check pass to primary sources, records a SHA-256
checksum of whatever content it retrieved, and reports each result at one of three
confidence tiers rather than as a flat resolved/unresolved. Results are saved in
pipeline_history/ as Section 9 of the report.

- `resolver.py` — resolution, checksums, the relevance check that gates the top tier
- `pagination.py` — reads past page one of a paginated source (WordPress REST totals, `rel=next`, numbered pages)
- `wayback.py` — archive availability check and Save Page Now submission
- `topic_match.py` — keyword gating shared by the pointer-only adapters
- `sources/` — the individual source adapters (see sources/README.md)

The tiers, what each one does and doesn't prove, and the archiving behavior are
documented in [docs/CITATIONS.md](../../../../../../docs/CITATIONS.md).
