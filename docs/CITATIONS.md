# Citations (Section 9)

After the fact-check pass, the pipeline takes the claims that came out of it and tries to trace each one to a primary source.

**Which claims.** All five fact-check buckets are resolved: `confirmed`, `outdated`, `contradicted`, `unverifiable` and `primary_source_needed`. `confirmed` matters most and is easy to overlook — those are the claims that ship *as written*, so they are the ones whose sources most need checksumming and archiving. Each entry records which bucket it came from in `fact_check_bucket`, because the same URL verified the same way means something different behind a claim you are about to publish than behind one you are about to rewrite.

**Which URL.** The draft's own citation for the claim comes first. Section 9 audits the article's citations, so the question worth answering is "does the source *you cited* say this" — not "is there some page on the internet that agrees." The pipeline reads the draft's numbered citation block, traces each claim back to the marker attached to it, and checks the claim against that source. See [How a claim is traced to a citation](#how-a-claim-is-traced-to-a-citation) below.

Failing that — a claim the draft attaches no citation to — the URL named in the claim's own source field (`source`, or `best_candidate_source` for `primary_source_needed`) is used instead. A claim with neither gets no URL at all and falls through to the source adapters. That is deliberate: "the draft cites nothing here" is a true and useful thing to report, and it is the honest answer where a guess used to go.

> **What used to be here.** Until this changed, a claim with no source of its own inherited a *response-level* grounded URL — the first entry of the provider's search-results list for the whole response, which was never about any particular claim. In one run that stamped a single LBNL energy report onto 44 unrelated claims (Yorkville water rates, ICNIRP exposure limits, IARC classifications) and the relevance checker correctly reported that an energy report does not discuss any of them. Two real findings sat inside 47 false positives. A section that flags everything flags nothing.
>
> **Do not restore it by adding more grounding sources.** The obvious-looking repair — wire a second provider's grounding in so there are more URLs to draw on — was built (Gemini grounding, PR #81) and is now moot: the defect was never a shortage of grounded URLs, it was that a *response-level* URL is not about any particular claim, so a better or more numerous supply of them changes nothing. PR #81 was closed unmerged and the grounded-URL path no longer exists to plug into. The fix for low coverage is more claims traceable to a citation, not more URLs to guess with.

The results land in **Section 9** of the report — both in `run_N_<timestamp>_report.json` and in the readable `run_N_<timestamp>_review.md`.

Section 9 is not a pass/fail list. A claim can be resolved at very different levels of confidence, and the difference matters: one tier means a model read the source and confirmed it backs the claim, another means "here is a portal that is probably about the right topic." Reading those as equivalent is exactly the mistake this section is structured to prevent.

**How the readable report is organised.** It opens with the fraction that matters — *N of M claims were checked against a document the pipeline fetched and read* — then a table putting every claim in exactly one disposition, then one block per disposition in the same order. The fraction leads because it governs how much of the rest to trust, and it is usually small: in the run this structure was built from, 18 of 144 claims had a document fetched and read. "Checked" deliberately counts both read outcomes — 9 where the document supported the claim and 9 where it did not — because a mismatch *was* checked; the check returned "no". Counting only the confirmations would reproduce, in the summary line, the same conflation between assertion and retrieval that the tiers exist to separate. The tier names below were already honest about that, but a four-way count in the opening line made the reader derive it.

**A `confirmed` finding with no external source is demoted.** The fact-check pass sometimes reports a claim as `confirmed` while citing nothing outside the draft. Measured 2026-09-05 on an 18K-char draft: 4 of 19 confirmations named no document — one cited "Draft Article", the pipeline confirming the draft against itself, and three cited "Manual Calculation", where the model did the arithmetic and reported the result as an established fact. All four carried `source_url: "N/A"`. Those move to `unverifiable`, keeping the original source text so the reader can see what was offered instead. The sums may well be right; `confirmed` is the tier that means a document backs the claim, and a model reasoning its way to a conclusion is what the other buckets already represent. A source naming a real document without a URL — an unlinked "Honda ServiceNews B18010I" — still counts, since those unlinked leads are among the most useful things the pass produces.

**The `confirmed` bucket is reconciled against retrieval.** The fact-check pass's verdict and this section's retrieval are independent — the first is model judgment about the claim, the second is whether a document was opened. In that same run 85 claims came back `confirmed`; 9 had a document read that supported the claim, 8 had one that did not, and for 68 no document was read at all, so the report now states that relationship directly rather than leaving `fact_check_bucket: confirmed` sitting beside a claim with no URL, where it reads as corroboration it is not.

---

## How a claim is traced to a citation

The fact-check models return claims, not locations, so something has to connect a claim back to the place in the draft it came from. That work lives in `adapters/citation/draft_citations.py`.

**The draft's citation block** is found by its heading (`## Citations`, `## Sources`, `## References`, `## Works Cited`, …) and parsed into `marker → URLs`. Sub-numbering is preserved: `[24a]` and `[24d]` are different sources, not two references to note 24.

**Markers cite backwards.** In `…first reactor online by 2030. [24a] Microsoft signed a 20-year PPA…`, the marker belongs to the sentence *before* it. This is the single detail everything else rests on — attaching markers forward shifts every citation in a paragraph one sentence late, which produces a mapping that looks entirely plausible and is wrong throughout. The body is cut into spans at each marker run, and a span keeps the text preceding its marker.

**A list item is its own unit.** An uncited bullet does not inherit the next bullet's markers. In the run that motivated this, a summary list whose middle bullet (xAI's turbines, QTS's water, Meta's EPA review) carried no citation took the markers off the bullet below it, pointing three Virginia noise sources at a Tennessee air-permit claim.

**Matching** is a bag-of-words-plus-figures similarity between the claim and each span. Figures carry most of the weight — two passages about water use are told apart by `42,000` versus `350,000`, not by vocabulary. A claim that clears no span by a clear margin gets no URL rather than a guessed one; in the reference run, 4 of 133 claims landed there.

**Ordering.** When several markers are plausible, they are ranked using the citation entries' own descriptions. Span position alone is not enough: a summary bullet that restates several findings and cites the span once will match a claim almost verbatim while its markers point elsewhere. The block breaks the tie — entry `[6a]` names xAI and Memphis in its description, entry `[4]` does not.

If a claim explicitly names a marker (`"cited in [29] but not public"`), that wins outright. The model is naming the citation directly, which beats any similarity score.

### More than one cited source

A draft often cites several sources for one passage, and the marker enclosing a claim is not always the one the author meant for the passage's opening sentence. So up to three candidate URLs are checked, best first, **stopping at the first one that supports the claim**. A claim whose first cited source backs it costs exactly one fetch; only claims that would otherwise be reported unsupported pay for the rest.

When none of them supports the claim, the reported entry is the most *informative* outcome, not the first one tried — a `contradicts` outranks a `not_addressed`. This is load-bearing. In the reference run the draft's "17 billion gallons" figure was contradicted by the LBNL report it cited (which says 66 billion liters, ≈17.4 billion gallons) while two sibling sources simply did not discuss it. Reporting whichever came back first would have buried the only thing worth acting on.

The other URLs checked are recorded on the entry as `alternates_checked`, so "only the third cited source actually carries this claim" is visible rather than silently smoothed over.

---

## Confidence tiers

Every entry carries a `verification` field. Four values reach the report — `checksum`, `pointer`, `unverifiable`, `content_mismatch` — plus the entries that never reached a tier at all, which the readable report splits by whether a URL was tried. Six blocks in all; the table at the top of Section 9 lists them in this order and every claim appears under exactly one.

### Read, and supports the claim — `verification: "checksum"`

The strongest tier. The source URL was fetched, its content SHA-256 checksummed and recorded, **and** a model read that content and confirmed it supports the specific claim.

This tier is only reachable two ways:

1. **A cited-source citation** — the draft cites a source for this claim (or, failing that, the fact-check model supplied one). The pipeline fetches it, checksums it, then makes a separate cheap model call (`mistral-small-latest`) asking whether the page content actually supports the claim. The verdict must come back `supports`. A verdict of `contradicts`, `not_addressed`, or `inconclusive` sends the pipeline to the next source cited for the claim, and demotes the entry out of this tier entirely if none of them supports it (see *Content mismatch* below).

   This check exists because a URL reaching this path is not necessarily the right page: a model-supplied one is often recalled from training data, and a draft's own citation can be attached to the wrong sentence. **A URL that loads is not evidence that the page says what the claim says.** The fetch proves the page exists; only the relevance check speaks to whether it's the right page.

2. **A data-fetching source adapter** — `census`, `crossref`, `eia`, or `fred`. These retrieve actual data from an API, so the checksummed content *is* the evidence.

**How much to trust it:** high. The source was retrieved and its relevance affirmatively checked. Still worth a glance — the relevance check is one cheap model call, not a human — but this tier is doing real verification work.

> **If the relevance check can't run,** the entry does not stay here — it moves to *Fetched, but could not be read* below. Reaching this tier always means a model read the extracted content and affirmed it. With no Mistral key configured, expect no verified `known_url` entries at all.

**What the model actually reads.** The fetched body is reduced to readable text before verification: main-article extraction for HTML (nav, header, footer, script and style blocks stripped) and `pypdf` text extraction for PDFs. The excerpt sent to the model is then centred on the passage containing the claim's distinctive terms and figures, rather than the first N characters of the document — in a long PDF the supporting sentence is rarely near the top.

### Pointer only — `verification: "pointer"`

A topic-relevant source was identified. **Nothing was verified.**

Six adapters are pointer-only: `epa`, `ferc`, `fhwa`, `icc`, `ilga`, `pjm`. They match a claim against a keyword list for a regulatory or statistical topic and, on a hit, return the URL of the relevant portal or publication — a place a human could go look this up. They do not retrieve the figure, do not confirm it, and do not know whether the portal actually contains anything supporting the claim.

The report labels this tier "topic-relevant source identified, NOT independently verified — confirm manually before citing," and that label is literal.

Keyword matching is gated by `topic_match.py`, which discards a keyword hit when it appears in the same sentence as a credential phrase ("credentials in", "degree in", "expertise in", …). Without that gate, a sentence like *"He does not hold credentials in environmental engineering or air quality analysis"* genuinely contains "air quality" and would resolve to the EPA Air Quality System portal — a claim about a person's background pointed at an emissions database. The gate deliberately errs toward not resolving rather than resolving to the wrong topic, so expect some claims to land in *No source identified* that a human would have matched.

**How much to trust it:** treat it as a research lead, not a citation. Open the URL and confirm before the claim ships.

### No source identified / fetch refused — `resolved: false`, no `verification`

No configured adapter matched, or the source URL couldn't be fetched. The entry carries a `note` explaining which. Nothing was established.

The readable report splits this in two, because the two halves call for different follow-up. An entry with **a URL** was a real fetch that failed (403, 404, DNS) and renders under *Source URL identified, but the fetch was refused* — the document is named, a 403 describes automated access rather than the document, and the archive copy is listed beside it, so these are often clearable by hand. An entry with **no URL** renders under *No source identified*: nothing was ever found to try.

**How much to trust it:** nothing to trust — this is a to-do.

### Content mismatch — `verification: "content_mismatch"`

A distinct failure mode, and the highest-information outcome in the section: the source URL fetched and checksummed fine, but the relevance check came back saying the page does **not** support the claim. These are the only entries where a document was genuinely retrieved, read, and found not to back the claim it was cited for, so the readable report gives them their own block (*Read, and does NOT support the claim*) directly under the confirmed ones. They previously rendered inside *Unresolved*, indistinguishable from claims nothing had ever been fetched for. The entry records the verdict (`contradicts`, `not_addressed`, or `inconclusive`) and the model's one-sentence reason, and the report separates them: `contradicts` means the source says otherwise and the draft may be factually wrong, while `not_addressed`/`inconclusive` far more often means the wrong URL was checked or the relevant passage did not extract — a citation problem, not a factual one. All nine mismatches in the motivating run were `not_addressed`.

This is worth more attention than an ordinary unresolved entry. An ordinary one means "we couldn't find a source." This one means "a source was proposed and it doesn't check out" — and a `contradicts` verdict in particular is a signal about the claim, not just about the citation.

**Every source cited for the claim failed**, not just one. When the draft cites several, all of them are checked before the entry lands here, and `alternates_checked` lists the ones that are not shown. Reporting a mismatch after checking one of three cited sources would say something false about a draft that cited the right source second.

This tier asserts something about the source, so it is only ever reached when the document was genuinely read. If the content could not be extracted, or the check could not run, the entry becomes *Fetched, but could not be read* instead — never this.

### Fetched, but could not be read — `verification: "unverifiable"`

The source URL fetched and checksummed fine, but no judgement about it was possible. Either the content could not be read, or the relevance check could not run:

- the document is a PDF with no extractable text layer (a pure scan, or password-protected);
- the page is JavaScript-rendered, paywalled, or otherwise yielded no article text;
- the response was a bot-check, CAPTCHA, or paywall interstitial served as HTTP 200 rather than the document itself (`content_kind: "access_wall"`);
- no Mistral API key is configured, or the relevance call failed or returned an unparseable verdict.

`resolved` stays `true` — a real document was fetched, and it is still archived and shown to you for manual checking. But nothing was confirmed and, importantly, **nothing was refuted**.

**How much to trust it:** treat it exactly like *Pointer only* — a lead to check by hand. The one thing it never means is that the source failed to support the claim. That distinction is the point of the tier: an honest "we couldn't read this" is useful, while a wrong "this source doesn't back you up" is actively misleading.

---

## Content drift

The checksum recorded on a verified citation isn't only a fingerprint of what was fetched — it's comparable across runs. Before resolution starts, the pipeline builds a URL → prior-checksum index from every report under `pipeline_history/`: every run, every article, since the same primary sources (EPA eGRID, EIA state profiles) get cited across many articles for one publication. When a citation resolves and its URL is already in that index with a different checksum, the entry gains a `content_changed_since` field recording the prior checksum, and which run of which article last saw it.

It surfaces as its own block above the tiers in the readable report, and in the console summary:

```
############################################################
WARNING: 2 verified source(s) have changed content since a prior run checksummed them:
  https://www.eia.gov/electricity/state/illinois/
    Last matched in run 3 of 'grid-reliability-piece' on 2026-01-04T09:12:00
Claims previously verified against these sources may need re-checking.
############################################################
```

**What a mismatch proves:** that the bytes at that URL are not the bytes that were there the last time this pipeline fetched it. That's all. It does not say what changed, how much, or whether it matters.

The change may be a substantive revision — a figure restated, a methodology note added, a table replaced with updated annual data — which is exactly what you want to know about a source a previously-published claim rests on. Or it may be a rotating ad slot, a "last updated" timestamp, a session token in the markup, or a randomized nav element. A SHA-256 over full page content cannot tell those apart, and this feature does not try to. Treat a flag as *go look at the page*, not as *the source changed its story*.

**What it never does:** block. A mismatch does not fail resolution, does not demote the entry out of its tier, and does not change `resolved`. A citation flagged for drift is still a verified citation; the flag is a note for the human reviewer sitting alongside it.

**Scope: the verified tier only.** Drift is computed for `verification: "checksum"` entries and compared only against prior entries that were themselves at that tier. Pointer-only citations are excluded in both directions. Their checksum is taken over whatever the adapter returned as content, which for a portal pointer is usually nothing at all or a static blurb — so a mismatch there would be measuring the adapter, not the source. Excluding them from the index also matters: a URL first cited pointer-only and later fetched for real would otherwise flag as "changed" on the strength of the tiers differing. Entries from reports predating the `verification` field are skipped for the same reason — their tier can't be established, so no comparison from them is trustworthy.

Content-mismatch entries (`verification: "content_mismatch"`) are not drift-flagged either. They've already left the verified tier carrying a stronger signal, and stacking a second one on top adds noise, not information.

There's no drift check on a first run against an empty or missing `pipeline_history/`, and none for a URL never cited before — no prior checksum means nothing to compare.

**Scope: matching checksum bases only.** Two checksums are compared only when they were taken over the same thing, recorded as `checksum_basis`. A `known_url` checksum used to cover the raw HTTP response body and now covers the extracted article text (`checksum_basis: "extracted_text"`), so comparing across that change would report every previously-cited source as having changed when none of them moved. Reports written before the switch carry no `checksum_basis` and are therefore not compared against current ones. This suppression is self-healing: once a URL has been re-checksummed on the new basis, the run after that compares normally. Adapter-sourced citations carry no basis label at all — what their checksum covers never changed, so they stay comparable against every prior run.

---

## Wayback Machine behavior

Every resolved citation URL is checked against the Wayback Machine, and the pipeline actively works to get sources archived rather than only reporting on them.

**Availability check.** Each resolved URL is looked up via archive.org's availability API. The result records whether a snapshot exists, its timestamp, its age in days, and whether it's stale. "Stale" defaults to older than 180 days and is set by `pipeline.wayback_snapshot_stale_days` in `user.yaml`. If the cited URL is itself a `web.archive.org` link, its date is read straight from the URL and no archive-of-an-archive lookup happens.

**Rate limiting, pacing, and the circuit breaker.** archive.org throttles the availability API at IP level over a long window rather than per-second, and it does not warn first. Measured 2026-08-12: 12 consecutive lookups all returned 429 — including the very first — and it was still 429 after a 45-second cooldown at one request every 6 seconds. With no pacing and no backoff, the archive thread had been effectively dead for two runs: 49 resolved citations, 0 archived, ~49 rate-limited.

Three mechanisms now sit in front of it, because no one of them is sufficient:

| Mechanism | Value | Why |
|---|---|---|
| Minimum interval between calls | `3.0s`, serialised on a process-wide lock | Resolution runs in a thread pool, so without this the *pool's width* sets the request rate, not any deliberate choice. |
| Retry with backoff | up to `3` attempts, `5s × 2^attempt`, capped at the server's `Retry-After` (max 60s) | Prefers archive.org's own answer about when to come back over a number we invented. |
| Circuit breaker | trips after `5` consecutive 429s | Once archive.org is refusing consistently, further calls only spend the run's time collecting more 429s. |

**What a tripped breaker means for the report.** Every lookup after the trip is skipped, so those citations carry `archived: null` — *the lookup did not complete*, which is not the same as *no snapshot exists*, and nothing is submitted for archiving on the strength of a null. Because the breaker makes this the common case rather than a rare one, Section 9 states it once at the top rather than leaving the reader to infer it from a page of per-entry notices:

```
> **Archive status is unknown for 65 of these citations.** archive.org
> rate-limited this run (HTTP 429). `archived: null` means the lookup did not
> complete, **not** that the page is unarchived — and nothing was submitted for
> archiving on that basis. Re-run to find out.
```

An `archived: false` entry is untouched by all of this: that is an answer, and only `null` means the run never found out.

**Save Page Now submission.** After resolution finishes, any resolved citation whose URL is *not* yet archived gets submitted to archive.org's Save Page Now API. This runs as a follow-up pass at a lower concurrency than resolution itself (2 vs. 8), because each submission triggers a real page capture on archive.org's side rather than a cheap read.

**Submitted is not archived, and the report no longer conflates the two.** This used to be fire-and-forget: the pipeline submitted, recorded `submitted: true`, and said "submitted for archiving" — which reads as *archived*. Nothing established that. A capture archive.org accepted and then dropped looked exactly like one it completed, run after run. Each citation now records an `archive_outcome`, and only one of its values claims a snapshot exists:

| `archive_outcome` | Means | Snapshot URL? |
| --- | --- | --- |
| `archived` | archive.org named the snapshot it wrote | yes — reported and citable |
| `pending` | capture queued, still running when the report was written | no |
| `submitted` | accepted, no snapshot named and no job id to ask about | no |
| `capture_failed` | accepted, then the capture failed — reason recorded | no |
| `submit_failed` | archive.org refused the request itself | no |
| `not_attempted` | never asked: non-public address, or the host did not resolve | no |

How each is established depends on which endpoint the run can use:

**Without credentials** the unauthenticated `GET /save/<url>` runs the capture inline and redirects to the resulting snapshot, so the snapshot URL is in hand before the call returns — no waiting, no polling, no next run. (Measured 2026-09-05: one 302 hop to a `/web/<timestamp>/<url>` minted seconds earlier.) The pipeline previously followed that redirect and discarded the URL.

**With credentials** the SPN2 endpoint queues the capture and returns a job id instead. Those get a bounded same-run wait (~45s for the whole pass, sharing the pacing clock below), and anything still running when it expires stays `pending`, keeps its job id, and is reconciled on the *next* run: that run asks `/save/status/<job_id>` what became of it before resubmitting, so a capture that keeps failing is reported with archive.org's own reason instead of silently retrying forever. Note that the job-status endpoint is **credential-only** — it answers `401` to everyone else — which is why an unauthenticated run reads its answer off the redirect instead.

Freshness is measured rather than assumed: a save can redirect to a pre-existing snapshot, and a four-year-old copy is still reported stale.

The console says which happened:

```
  3 resolved URL(s) archived this run — snapshot URL recorded against the citation
  1 resolved URL(s) submitted for archiving, capture NOT confirmed — reported as pending, not as archived
  1 resolved URL(s) accepted by archive.org and then failed to capture — still not archived
```

Every failure here degrades to "not established" and never fails the run.

**Capture options.** The `/save` form's control names *are* the API's parameter names, so everything the web form offers is available: `capture_outlinks`, `capture_all`, `capture_screenshot`, `disable_adblocker`, `wm-save-mywebarchive`, `email_result`, `wacz`. The pipeline sets exactly one, and it is a correctness fix rather than a feature:

`capture_all=0` — **archive.org's own form defaults this ON**, and ON means "archive the page even if it answers 4xx/5xx". For citation archiving that manufactures a false positive: a source that blocks the capture with a 403 would have its block page archived, the next run's availability check would find a snapshot, and the report would call the citation archived when what is archived is an error page. It would bite hardest on sources that refuse automated requests — the citations already flagged as most needing an archive.

Deliberately not set: `email_result` and `wacz` (archive.org emails the account owner once per capture, and a run submits many citations), `wm-save-mywebarchive` (writes to the operator's personal archive), `capture_outlinks` (enormous added load for pages the article does not cite), `capture_screenshot` (nothing renders a screenshot URL yet, so it would be collected and discarded), `force_get` (captures JavaScript-rendered pages worse).

`if_not_archived_within` was tried and rejected. It looks like a fit for `wayback_snapshot_stale_days`, but measured 2026-09-06 it makes archive.org answer `job_id: null` with *"The same snapshot had been made 177 hours ago"* while the identical request without it captures normally — so it removes information, and it is a second gate on a decision this pass has already made.

**Capacity-aware pacing (authenticated only).** `GET /save/status/user` reports what the account can actually do right now:

```json
{"processing":0,"available":3,"daily_captures":49,"daily_captures_limit":30000}
```

Every concurrency number governing archiving was otherwise invented. Before submitting, the run asks: if the daily quota is exhausted, nothing is submitted and each citation says so with the quota figures — rather than the author reading a batch of failures. If fewer capture slots are free than the static ceiling, the batch narrows to match. A reading can only ever make the run *more* cautious; it never widens past the static bound, so a stale or wrong answer cannot make things worse. Without credentials the endpoint answers 401 and the run behaves exactly as before.

**Reference list for publication.** Section 9 opens with every cited address, once each, in the order first cited, paired with its archive copy:

```
### Reference list - live and archived addresses (3 source(s))

1. https://www.iana.org/help/example-domains
   archived: http://web.archive.org/web/20260905123736/https://www.iana.org/help/example-domains
2. https://www.rfc-editor.org/rfc/rfc2606.html
   archived: http://web.archive.org/web/20260830225036/https://www.rfc-editor.org/rfc/rfc2606.html

**No archive copy (1)** - publishable only as a live link:
- https://example.org/x - archive.org tried to capture it and could not
```

The pairing was always present per citation, but only inside the diagnostic entries below - spread over five disposition buckets and interleaved with claim text, verification tiers and relevance notes. That is the right shape for deciding whether to trust a citation and the wrong shape for putting both addresses into the article. A source backing three claims appeared three times; here it appears once.

Sources with no snapshot are listed rather than dropped, each with the specific reason, because the author needs to know which references the article cannot carry an archive link for. A pairing that was not confirmed against the live page, captured an error page, or is stale is marked inline - those are the ones that should not be published as-is.

**Does the archive say what the live page said?** The report tells you to *cite both* the live URL and the archive copy. That is a recommendation you act on, and nothing used to establish that the two agree — a snapshot of a paywall, a cookie wall, a bot block, or a much older version of the page renders exactly like a good one.

Each resolved citation's snapshot is now fetched and compared against the same checksum the live fetch produced. It works the same whether this run created the snapshot or it already existed.

The comparison is exact, not fuzzy, because of one API detail: `https://web.archive.org/web/<timestamp>id_/<url>` serves the **original captured bytes**, while the ordinary form injects archive.org's banner and its `wombat.js` URL rewriter. Measured 2026-09-06 across three URLs (one archived a week earlier), the `id_` body was byte-identical to the live page — SHA-256 equal — while the ordinary form was nearly three times the size for the same document. Comparing the injected form would report every citation as divergent.

| `archive_match` | Report line |
| --- | --- |
| `identical` | "Archive verified: the snapshot's text is identical to the live page this run checked" |
| `differs` | "**Archive does NOT match the live page**" plus what was measured |
| `unchecked` | says why, and that the snapshot may or may not contain the document |

A difference is reported, not judged. It has two innocent explanations (the page changed after capture; extraction differs slightly) and one serious one (the capture is not the document), and this pass cannot tell them apart. Citations resolved *from* the archive are skipped — comparing a snapshot with itself proves nothing.

**Re-asked alternative sources.** When a refuted citation's asserting model proposes a different source, that proposal goes through the whole of `resolve_citations` — so the run has already asked archive.org about it and, where it was missing, spent a real capture. The archive address and match verdict are carried through to the proposal in the report rather than discarded, so a source the author may adopt arrives with its durable copy attached. The proposal's own verification is untouched: a refuted citation stays refuted.

**Snapshots that captured an error page.** The availability API reports each capture's own HTTP status, and it was being discarded. A pre-existing snapshot can be a capture of a 403 or 404 — "archived" is then true and useless, because what is preserved is the refusal, not the document. Those are now flagged outright. The pipeline's own captures cannot produce one (`capture_all=0`), but pre-existing snapshots are outside its control.

**Was that us, or archive.org?** A 520, a read timeout and a refused connection look identical from inside the pipeline, and each is equally consistent with "we asked too often" and "the service is having a bad afternoon" — all three were seen in a single afternoon of real runs. When at least one submission fails, the run asks `GET /save/status/system` once and records the answer against the failed entries, so a failure reads either *"archive.org reported its capture system healthy at the time"* (it was us) or *"...reported its capture system as degraded — this was the service, not the pipeline"*. A healthy run never makes the call, and an unknown verdict adds nothing rather than appending "we could not tell" to a failure the reader is already looking at. Credential-only, like the other status endpoints.

**Queued captures expire after 7 days.** A job archive.org has forgotten would otherwise stay in the pending index permanently, and every future run would spend a paced status call rediscovering that. Captures finish in seconds to minutes; a week is generous.

**Unreadable-origin fallback.** When a direct fetch of a `known_url` fails in a way that means *we couldn't read the origin* rather than *the resource is gone*, the pipeline makes one attempt (never a retry loop) to obtain the document another way, and uses whatever it gets for checksumming and relevance verification. There are two tiers, tried in this order:

1. **The live page behind a browser TLS fingerprint** — 403 only. See "Escalating past a bot block" below.
2. **An archive.org snapshot of the same URL** — every failure in the table below.

It fires on:

| Failure | Recorded `origin_failure` |
|---|---|
| 401 Unauthorized | `auth_required` |
| 403 Forbidden | `blocked` |
| 429 Too Many Requests | `rate_limited` |
| Connect/read timeout | `timeout` |
| DNS or connection failure | `unreachable` |

It deliberately does **not** fire on 404/410 — the resource is genuinely gone, and surfacing that is the point; an archive copy would mask a problem you need to fix by re-sourcing the claim. It also does not fire on 5xx, which is the origin's own failure rather than a refusal aimed at us: a transient 5xx will be fine by the time a reader clicks, and a persistent one means the source needs replacing. Neither is helped by quietly substituting an archived copy.

The entry records `verified_via: "wayback_fallback"`, the `origin_failure` reason above, and an `archive_provenance` note stating that the checksum and relevance verdict describe the archived copy, not the live page. Staleness is not suppressed: a 245-day-old snapshot that satisfied a timeout is still reported stale, and the provenance note says so.

The same rules govern draft link validation (`analysis/links.py`), so a link recovered from the archive after a timeout reads `OK (via archive: origin timed out)` rather than being flattened into a plain `OK` or a bare `BROKEN`.

### Escalating past a bot block

A 403 gets one more attempt before the archive: the same request with a browser TLS fingerprint, via `ci_core.http.impersonating_get`. **For a public document, retrieval method does not affect citation validity** — the reader opening the link gets the same page — so a source that refuses scripts but serves browsers is a verifiable citation, not an unverifiable one. On the 2026-09-05 Honda run this was eight claims whose sources the *link checker* could already read, because it has escalated since 2026-08-12 and citation verification did not.

**Scope.** Only a 403. A 401 says an account is required, which is the access-controlled case this policy puts out of scope; a 429 is a rate limit, and changing fingerprint to slip one is abuse rather than verification; a timeout or DNS failure never reached the origin, and no handshake fixes that. No attempt is made at a CAPTCHA, a JS challenge or a subscription gate, and none would work — the measurement in `ci_core/http.py` records three academic publishers returning a challenge page to impersonation too.

**Live before archive**, deliberately, because the two are not competing options:

- The archive lookup runs on the success path regardless, so escalating first yields the snapshot link **as well as** a current read. Archive-first would trade the current read away to obtain something the run was going to get anyway.
- The checksum and relevance verdict are reported at the strongest tier this pipeline has, so they should describe the document the article actually cites. Both snapshots behind the Honda run's refused claims were flagged stale (358 and 494 days); verifying a claim against a year-old copy and labelling it `checksum` asserts a currency the run never established.

A failed escalation costs nothing: anything that comes back unreadable — a challenge page, a near-empty body — falls through to the archive fallback exactly as an un-escalated 403 would. The tier is additive or it is a regression.

**What the entry records.** `verified_via: "tls_impersonation"`, `origin_failure: "blocked"`, and a `reader_access` note. That note is access information, not a disclosure: what escalation predicts is that the *reader* may hit friction on this host, which makes it exactly the citation that needs an archive copy printed beside it. Section 9 renders it under the live URL, where the author is deciding what to paste.

Where the fetch landed somewhere other than the URL cited — the fact-check model supplies Vertex `grounding-api-redirect` URLs, so it usually does — the destination is recorded as `final_url`, **the archive lookup follows it**, and the paste-ready "Cite both" pairing names it. Asking archive.org about the redirect instead returns `archived: false`, which the report would state as "archive.org has no snapshot of this URL" while the real source is archived: a false absence printed beside the citations that most need the archive to be there.

**This tier needs an optional dependency.** `impersonating_get` returns `None` — leaving the archive fallback to run exactly as before — unless `curl_cffi` is installed. It is an optional extra because the degradation is safe and the wheel carries a compiled libcurl; end users who want the tier install it explicitly:

```bash
uv sync --extra unblock
```

**In this repo it is no longer optional in practice.** `ci-core[unblock]` is in the root `[dependency-groups] dev`, alongside `ci-core[persistence]` and for the same reason: `uv run` re-syncs from the lockfile before every invocation, so while the extra appeared in no dependency group, *no `uv run` command ever had curl_cffi*. `impersonating_get` returned `None` unconditionally, and the escalation added to `analysis/links.py` on 2026-08-12 had never once fired — nor had `_impersonation_fallback_content` in the citation resolver. Nothing errors when this happens; the run simply resolves fewer citations, which is why it survived a month unnoticed. The unit tests do not catch it either: they install a fake `curl_cffi` and pass whether or not the real one is present. A plain `uv sync` now brings it in.

**A run now says when the tier is missing**, in both places it matters. `impersonating_get` returns `None` for a held block and for an absent `curl_cffi` alike — the right default for a fetch, the wrong one for a report, since only one of the two is fixable by installing something. So the run logs a one-time warning (once per process, not per link), flags each affected link with `escalation_unavailable`, and records a `degradations` entry that reaches `run_N_*_review.md`. The entry says to read those links as **unknown rather than blocked**: the attempt was never made. Use `ci_core.http.impersonation_available()` to tell the two apart in code.

**The version floor is load-bearing.** `impersonate="chrome"` resolves to whatever `curl_cffi.requests.impersonate.DEFAULT_CHROME` the installed release ships, so an older release asks for an older Chrome profile — and a stale profile is fingerprinted by exactly the Cloudflare deployments this tier exists to get past. Measured 2026-09-06, sequential fetches only — concurrent requests to a single Cloudflare host cannot tell a version difference apart from luck:

| curl_cffi | `DEFAULT_CHROME` | congress.gov | bianchihonda.com |
| --- | --- | --- | --- |
| 0.16.0 | `chrome146` | 0 of 5 | 0 of 4 |
| 0.16.1 | `chrome150` | 1 of 1 (422,259 bytes) | not sampled |
| 0.16.2 | `chrome150` | 1 of 1 | not sampled |
| 0.16.3 | `chrome150` | 2 of 2 | 4 of 5 |

Every one of 0.16.0's nine failures carried `cf-mitigated: challenge`; none of the successes did. 0.16.3's one miss on bianchihonda.com is the nondeterminism described below, not a version effect — the same host served the same build on the attempts either side of it.

The working boundary is 0.16.1, which bumped curl-impersonate to 2.1.1. The extra pins `>=0.16.3` — one release higher — because 0.16.3 fixes `tls_signed_cert_timestamps not applied` (upstream PR #833), a defect on the fingerprint surface itself: 0.16.1 and 0.16.2 clear these hosts while still sending a subtly wrong handshake. The old `>=0.7` was not merely permissive, it resolved to 0.16.0 in `uv.lock`, so the documented `uv sync --extra unblock` installed the one version measured to be challenged everywhere it was tried. Expect to raise this floor again; a profile that works today ages out.

**Reading a 403 from this tier.** The `cf-mitigated` response header separates the two causes, which need opposite responses:

- **`cf-mitigated: challenge` present** — Cloudflare rejected the *fingerprint*. That is a curl_cffi version problem, not a property of the source: raise the floor and re-measure.
- **header absent** — the origin refuses browsers too. That is a genuine subscription or JS gate, and no fingerprint will move it; the three academic publishers in the `ci_core/http.py` measurement are this case. Not worth chasing.

**Success is not deterministic.** Six concurrent requests to one Cloudflare-fronted host in a single run had four succeed and two challenged. A claim resolved this way in one run may be reported refused in the next — see the rerun-nondeterminism caveat that already applies to this pipeline. It is also why the version comparison above is one sequential fetch per version: concurrent fetches to a single Cloudflare host cannot tell a version difference apart from luck.

### archive.org credentials (optional)

Submissions work without credentials via the unauthenticated capture endpoint, subject to tighter and less predictable rate limits. Supplying an S3-style key pair from <https://archive.org/account/s3.php> uses the authenticated SPN2 endpoint, which gets higher rate limits and returns a job id. The trade-off is not one-directional: the unauthenticated endpoint captures inline and hands back a snapshot URL immediately, while the authenticated one queues the capture, so an authenticated run is the one that has to wait or reconcile. What credentials buy is the ability to *ask what happened* — `/save/status/<job_id>` is credential-only, so without them a queued capture can never be followed up:

```yaml
api_keys:
  archive_org:
    access_key: your_access_key_here
    secret_key: your_secret_key_here
```

See [CONFIGURATION.md](CONFIGURATION.md#api-keys).

---

## Configuring sources

Adapters are selected per publication in `configs/<publication>.yaml`:

```yaml
citation_sources:
  - name: EIA
    adapter: eia
    description: Energy Information Administration data
  - name: FRED
    adapter: fred
    description: Federal Reserve Economic Data (BLS, Census series)
```

Adapters are tried in the order listed, and the first one that resolves wins. A claim carrying a `known_url` skips adapter matching entirely.

**`citation_sources` may be empty, and often should be.** Every shipped adapter targets US government or regulatory data, and most target Illinois energy policy specifically. If none fit your publication, leave the list empty — citation resolution still runs. Claims whose fact-check source names a URL are fetched, checksummed, relevance-verified and archived with no adapter configured at all, and for most publications that is the main path. Only the adapter-matching loop is skipped.

A source naming an adapter that does not exist (including `generic_url`, which was never implemented) is reported as a warning at run time and resolves nothing.

| Adapter | Kind | Reaches |
|---|---|---|
| `census` | data fetch | Verified |
| `crossref` | data fetch | Verified |
| `eia` | data fetch | Verified |
| `fred` | data fetch | Verified |
| `epa` | pointer | Pointer-only |
| `ferc` | pointer | Pointer-only |
| `fhwa` | pointer | Pointer-only |
| `icc` | pointer | Pointer-only |
| `ilga` | pointer | Pointer-only |
| `pjm` | pointer | Pointer-only |

Adding an adapter: see [the source adapter interface](../packages/ci-article-review/src/ci_article_review/adapters/citation/sources/README.md).

---

## Cost

The relevance check runs once per `known_url` citation, on a deliberately cheap model. When the pipeline passes its `api_call_log` through, each check is recorded as a `citation_verification:known_url` entry and flows into the run's cost total like any other model call.
