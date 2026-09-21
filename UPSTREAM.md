# Upstream contribution queue

Things this project needs that a maintained library nearly does. The rule
(see also the reuse-first note in CONTRIBUTING): when a library is 90% right,
**file the issue or PR upstream** rather than carrying a local fork of the
behaviour. A local workaround is a maintenance liability; an upstream fix is
one everybody gets.

Each entry carries the evidence needed to open it — a reproduction, a
measurement, and the proposed change. Do not add an entry without one.

**Whether we use the package is not the test.** If we find a real bug in
somebody's library, we file it, adopted or not — the finding is worth the same
to their users either way, and sitting on it because it no longer affects us is
just hoarding. Two corollaries, both learned the hard way on entries 2 and 3:

- *"We don't depend on it" is not a verification.* Deciding not to file is a
  claim about **their** code, so it has to be grounded in reading their code.
  Entry 3 was dropped on non-adoption and, when the source was actually read,
  turned out to contain a genuine bug in one library — and a claim that was
  simply wrong about the other.
- *A bug and a missing feature are filed differently.* A defect gets an issue.
  A capability the library never claimed gets our measurement added to whatever
  thread already exists, not a new issue implying they broke something.

Status values: `ready` (evidence complete, needs filing) · `verify`
(candidate, claim not yet checked **against their source**, not merely against
whether we still use it) · `filed` (link the issue/PR) · `contributed` (added to
an existing upstream thread — link the comment) · `landed` · `split` (parts
resolved differently — say which) · `dropped` (with the reason).

---

## 1. litellm — credit exhaustion is classified as a rate limit

**Status:** contributed — twice, and **deliberately not turned into a PR.** No
maintainer has responded to any of it as of 2026-09-21; see "Why no PR" below,
which is the part worth reading before anyone picks this up again.
[BerriAI/litellm#32785](https://github.com/BerriAI/litellm/issues/32785) already
reported this on 2026-07-10 (and traced it further than we had, to the OpenAI
branch of `exception_type()` testing `is_error_str_rate_limit()` before ever
consulting the body's `insufficient_quota` code). Our evidence went there as
[a comment](https://github.com/BerriAI/litellm/issues/32785#issuecomment-5299586013)
on 2026-08-14: the cross-provider table below (which #32785 offered to enumerate
and did not), confirmation that it still reproduces on v1.96.2 (they tested
1.91.1 and HEAD at 2026-07-10), and the in-band SSE case. The classifier was
offered there as the PR, pending a maintainer steer that never came.
Second contribution 2026-08-16: review input on
[PR #32798](https://github.com/BerriAI/litellm/pull/32798#issuecomment-5310640191),
the pick of three competing PRs already open for this.
**Repo:** BerriAI/litellm (tested against 1.96.2)

An account with no credits raises `litellm.exceptions.RateLimitError` with
`status_code=429`. The provider message survives intact, but the *type* says
"transient, back off and retry" when the true condition is "terminal, a human
must add credits". `RateLimitError` is the class retry logic keys on, so a
credit-dead account gets retried on every call in a batch.

**Reproduction** (a real credit-exhausted OpenAI key, 2026-08-12):

```python
litellm.completion(model="gpt-5.5", messages=[...], api_key=DEAD_KEY)
# litellm.exceptions.RateLimitError, status_code=429
# "OpenAIException - You have no credits remaining. Add credits to continue
#  using the API at https://platform.openai.com/settings/organization/billing/"
```

The same key streaming (`stream=True`) also raises `RateLimitError` — good, in
that it raises at all, but the same misclassification.

**Proposed change:** a distinct exception (or a flag on the existing one) for
terminal billing states, so retry/fallback logic can skip them. The providers
signal it unambiguously and differently from each other, which is exactly the
kind of normalisation litellm exists to do:

| provider | signal |
|---|---|
| OpenAI | `insufficient_quota` / `credit_balance_exhausted`, HTTP 429 |
| Anthropic | `credit_balance_too_low`, HTTP 400 |
| xAI | "insufficient credits", HTTP 403 |
| several | HTTP 402 Payment Required |

**Where each signal actually lands**, read off litellm `main` on 2026-08-16 —
`insufficient_quota` and `credit_balance` appear nowhere in
`exception_mapping_utils.py` (2,568 lines), so nothing has moved since #32785
was filed:

| provider | HTTP | class litellm raises today |
|---|---|---|
| OpenAI | 429 | `RateLimitError` — the bug |
| Anthropic | 400 | `BadRequestError`, via the `400 or 413` branch of `_map_anthropic_exception` |
| xAI | 403 | generic `APIError` — `_map_openai_exception` has no 403 branch, so it falls to the trailing `else` |
| nlp_cloud | 402 | `RateLimitError` — the only place litellm maps 402 at all |

Four ways to be out of money, three different exception classes, and 402 lands
back in `RateLimitError`. **The sharp edge for #32798:** `xai` is in
`litellm.openai_compatible_providers`, so it dispatches into
`_map_openai_exception` — the very function that PR patches — but its
`custom_llm_provider == "openai"` gate excludes it. Anyone upgrading and
branching on the new subtype gets correct behaviour on OpenAI and silently wrong
behaviour on xAI out of one code path. That argues for a per-provider table
rather than a single equality check, which is what we offered to write on top of
whatever shape merges.

**Why no PR.** The condition for opening one was that maintainers be receptive.
Measured 2026-08-16, they are not — and the reason is backlog, not disagreement:

- **Three PRs already implement this**, all opened 2026-07-10 and all still open:
  [#32798](https://github.com/BerriAI/litellm/pull/32798) (human author, CLA
  signed), [#32789](https://github.com/BerriAI/litellm/pull/32789) and
  [#32850](https://github.com/BerriAI/litellm/pull/32850) (both the Devin bot,
  CLAs unsigned). All three add `InsufficientQuotaError(RateLimitError)`. All
  three are `CONFLICTING`/`REVIEW_REQUIRED` with **zero maintainer comments** in
  five weeks.
- **3,280 open PRs, 1,877 of them older than 30 days.** Of the last 100 merged,
  **94 are Berri staff or their own Devin bot**; 6 are outside contributors.
- The thread is not short of analysis — the community already did the three-way
  comparison between the PRs. It is short of maintainer attention.

A fourth PR joins a pile of three unreviewed ones, so the contribution went where
it was additive instead: review input on the one that could plausibly land.
Revisit if a maintainer engages; the follow-up offer is on the record there.

**Correction to the 2026-08-14 comment.** It flagged the in-band SSE path as
something a fix to `exception_type()` alone would miss.
[#32835](https://github.com/BerriAI/litellm/pull/32835) landed 2026-07-11 and
does raise on in-stream error events, so that path is no longer silent — but it
raises `APIError`, so the terminal-vs-transient distinction is still lost there.
Corrected upstream in the #32798 comment rather than left standing.

**What we carry locally.** Not `ci_core/llm/quota.py` — that went with the
adapters. What survives is `_is_terminal_quota_error` in
`ci_core/llm/client.py`: a deliberately narrow phrase list, applied to every
retryable status (a dead account arrives as a 429 directly and as a synthesised
503 mid-stream), existing only to stop our own single retry. The full
cross-provider classifier stays unbuilt here on purpose — it belongs upstream,
where each provider's wording is better known than we know it.

**Checked independently, 2026-08-21.** Four days after our review input on
#32798, another contributor re-ran every row of our table against `main` rather
than taking it on trust
([their comment](https://github.com/BerriAI/litellm/pull/32798#issuecomment-5366100115)).
All four rows held, including the 402 one: `nlp_cloud`'s `429 or 402` really
does send an out-of-credit response to `RateLimitError`. They added a scope point
that changes what our follow-up offer means. `_map_openai_exception` has no 403
branch at all, so the per-provider table we offered to write has nothing to
attach to yet: whoever writes it has to add that branch first and choose a
default class for a 403 across the ~30 openai-compatible providers that share
the function. That is a more contentious change than adding a row, and bigger
than we framed it.

**State on 2026-09-21.** Still no maintainer comment on #32785 or #32798;
#32785's only new comment is a +1 linking the commenter's own blog. #32798 is
now `CONFLICTING` and still targets `litellm_internal_staging`, which litellm
has since abandoned for `main` (see entry 6). Like our #37126, it was closed and
reopened mechanically on 2026-09-13 when that branch was recreated. Retargeting
and rebasing it is its author's call. Outside PRs do land there, just rarely: 42
of the 1,500 merges from 2026-08-27 to 2026-09-21 came from forks, by 38
distinct authors.

---

## 2. Link checkers — no TLS-impersonation tier for Cloudflare-blocked links

**Status:** contributed — measurement added to
[lycheeverse/lychee#1439](https://github.com/lycheeverse/lychee/issues/1439#issuecomment-5302215817)
on 2026-08-15. Not filed as a defect: lychee treats a 403 correctly, so there is
no bug here, only a capability it lacks — and #1439 has had that request open
for two years. Our numbers argue for a lighter answer than the FlareSolverr
sidecar being discussed. The local tier stays ours (see the note at the end).
**Candidate repo:** lycheeverse/lychee (or whichever checker we adopt)

Citation checking hits sites that 403 every honest request. Measured against six
real blocked citations on 2026-08-12:

- browser **headers** alone: **0/6** — the hard blocks are Cloudflare, which
  returns 403 to a full browser header set *on the domain root*. It fingerprints
  the TLS handshake; no header string changes that.
- browser **TLS fingerprint** (`curl_cffi`, `impersonate="chrome"`): **2/5**
  recovered with real content — congress.gov (488 KB) and a CDC PDF (661 KB).

The remaining three (ASME, Wiley/AGU, Royal Society) return a ~6 KB challenge
page to everything and are very likely subscription gates, not bot gates.

**Proposed change:** an opt-in final tier that retries a blocked URL with a
browser TLS fingerprint before reporting it dead. Opt-in matters — it should be
a deliberate choice, not a default.

**Verified 2026-08-14, contributed 2026-08-15.** We did not adopt lychee (a Rust
binary whose output our tier semantics would need rebuilding around) and built
the tier ourselves: `ci_core/http.py` `impersonating_get` (`curl_cffi`,
`impersonate="chrome"`, behind the optional `unblock` extra), wired into
`analysis/links.py` as the final tier after an honest request is refused —
opt-in, as the entry wanted.

Not adopting it is *not* a reason to withhold the finding, so the measurement
went upstream anyway. What it is not, though, is a bug: lychee reporting a 403
as a 403 is correct behaviour. So it went as data on lychee's existing open
request for Cloudflare bypass (#1439, open two years, currently discussing a
FlareSolverr sidecar) rather than as a new issue — the useful contribution being
that a TLS fingerprint gets a real slice of the same benefit with no sidecar.

---

## 3. Wayback clients — rate-limit backoff and authenticated SPN2

**Status:** split. Our own throttling failure was our bug and is fixed (see the
verification note at the end). Separately, reading the candidate libraries'
source turned up a real defect in waybackpy, filed as
[akamhy/waybackpy#200](https://github.com/akamhy/waybackpy/issues/200) on
2026-08-15. savepagenow: no defect, and half of this entry's original claim was
wrong about it.
**Candidate repos:** akamhy/waybackpy, palewire/savepagenow (not pastpages —
the project moved)

`https://archive.org/wayback/available` throttles hard at IP level with a long
window. Measured 2026-08-12: 12 consecutive requests all 429, the *first* one
included, and still 429 after a 45s cooldown at 1 request / 6s.

Impact on this project, from its own run reports:

| run | resolved citations | archived | 429 |
|---|---|---|---|
| 2026-08-12 | 32 | 7 | 25 |
| 2026-08-11 | 49 | 0 | ~49 |

**Verified 2026-08-14/15. The run-report damage was ours and is fixed — but
reading the libraries anyway found a real bug in one of them.**

*Our half, fixed — on the second attempt:* the measured impact (49 resolved, 0
archived, ~49 429s) came from our own pipeline having no pacing and no backoff
at all. The guard added in response paced calls, honoured `Retry-After`, and
tripped a breaker once archive.org had refused repeatedly — but it was written
as if lookups were sequential, and `check()` runs on `_MAX_PARALLEL` resolver
threads. Under that, the backoff slept in the failing thread while the others
kept the pace up, and a success zeroed a counter shared with every other thread,
so a run throttled four-in-five never tripped the breaker at all. Fixed properly
in #99 (merged 2026-08-16), where a 429 moves a clock every thread waits on and
the count is a per-run budget. Recording the sequence because the first fix
looked right and tested green: concurrency was the part nobody checked, and the
test that proves it has to drive `check()` from a pool — a single-threaded test
passes against the broken version.
`submit()` already supported authenticated SPN2 via an archive.org S3-style key
pair (`Authorization: LOW <access>:<secret>`).

*savepagenow — no defect, and this entry was wrong about it.* `capture()` takes
`authenticate=True` and reads `SAVEPAGENOW_ACCESS_KEY` / `SAVEPAGENOW_SECRET_KEY`
to send the same `LOW` header, so "no authenticated SPN2" was simply incorrect.
It has no 429 backoff, but it raises a typed `TooManyRequests` carrying the
response headers, so `Retry-After` reaches the caller intact — raising and
letting the caller decide is a design choice, not a defect.

*waybackpy — real bug, filed as
[#200](https://github.com/akamhy/waybackpy/issues/200) and fixed in
[PR #201](https://github.com/akamhy/waybackpy/pull/201).*
`availability_api.py`'s `setup_json()` calls `.json()` with no status check.
archive.org answers a throttled availability call with an HTML error page, so
`.json()` raises `JSONDecodeError`, which surfaces as
`InvalidJSONInAvailabilityAPIResponse` — a rate limit reported as malformed
data, with the server's `Retry-After` discarded. Their **Save** API already
handles this correctly (`Retry` adapter plus `TooManyRequestsError`, added for
their own #131 in 2022); the fix never crossed to the Availability API, and
`TooManyRequestsError` is already in their tree, so it is a two-line status
check. Reproduced against 3.0.6 with a stubbed 429 — the live endpoint was not
throttling on 2026-08-15, which is itself worth noting: archive.org's throttling
is episodic, so the 2026-08-12 measurement is a throttled window, not a constant.

Note the same shape as entry 1: a throttle misreported as a data-format error,
sending the reader hunting for a parser bug that isn't there.

Caveat on expectations: waybackpy looks abandoned — last release 3.0.6 on
2022-03-15, last commit 2022-11-17, last repo push 2024-02-26, with open issues
sitting unanswered for years. Filed anyway; a searchable report of the error
string has value to the next person even if no maintainer answers.

---

## 4. litellm — completion() silently drops reasoning-summary streaming

**Status:** filed — [BerriAI/litellm#36992](https://github.com/BerriAI/litellm/issues/36992),
2026-08-14.
**Repo:** BerriAI/litellm (tested against 1.96.2)

`litellm.completion()` routes OpenAI reasoning models through Chat Completions,
which sends **nothing** while the model thinks. `litellm.responses()` uses the
Responses API and streams `response.reasoning_summary_text.delta` throughout.
Same model, same effort, same prompt, measured 2026-08-12:

| surface | time to first byte | total | max inter-chunk gap |
|---|---|---|---|
| `completion(stream=True)` | **79.1s** | 79.1s | **79.1s** |
| `responses(stream=True)` | **0.8s** | 76.5s | 17.2s |

The first row is 100% silence before any byte arrives. Anyone setting a socket
read timeout from observed inter-token gaps will size it correctly for every
provider except OpenAI reasoning models, then see them time out — with a hang
and a long think indistinguishable on the wire.

**Proposed change:** either route reasoning models to the Responses API from
`completion()` when a summary is available, or document the limitation
prominently. The silent part is the problem: nothing in the response indicates
that summary streaming was dropped.

**Prior art checked before filing** (both cited in the issue, so it cannot be
waved off as already-solved): #13780 asked for reasoning summaries through
`/chat/completions` and was closed *completed* on 2025-11-19, but the resolution
was `extra_body` param pass-through — that carries params *to* the Responses
API, it does not make the default `completion(stream=True)` path stream
summaries. PR #14117, which would have routed reasoning models to the Responses
API and is the actual fix, was **closed unmerged on 2026-02-24**. The routing
option has therefore been attempted and dropped once already, which is why the
issue leads with the cheaper ask: signal the drop, don't hide it.

---

## Checked and found to be non-issues

Recording these so nobody re-investigates them.

- **litellm passes Perplexity search parameters through.** `search_mode`,
  `search_recency_filter`, and `search_domain_filter` all reach the API.
  Verified 2026-08-12: `search_mode="academic"` took scholarly citations from
  4/18 to 9/22, and a domain filter constrained every citation to the requested
  domain.
- **litellm preserves the provider-specific fields we depend on.** Perplexity
  `citations` / `search_results` as top-level extras; Gemini grounding as
  `vertex_ai_grounding_metadata`; truncation as `finish_reason="length"`; cached
  tokens as `prompt_tokens_details.cached_tokens` and Gemini
  `cache_read_input_tokens`.
- **litellm raises on an in-band streaming error.** The HTTP-200-then-error case
  that this pipeline used to report as "Malformed JSON response" surfaces as a
  proper exception.

---

## 5. litellm — `web_search_options` + a system prompt breaks XAI

**Status:** landed 2026-09-16 — through BerriAI's own reimplementation, not our PR.
Filed as [BerriAI/litellm#37127](https://github.com/BerriAI/litellm/issues/37127)
with [PR #37128](https://github.com/BerriAI/litellm/pull/37128), both 2026-08-16. Their
Devin bot opened [#38254](https://github.com/BerriAI/litellm/pull/38254) from the issue
on 2026-08-25; a maintainer merged it to `main` (`d108cdc43`) on 2026-09-16 and the
issue closed as completed. #37128 was never reviewed or mentioned, and we closed it as
superseded on 2026-09-21. The fix is only in pre-releases so far (`v1.103.0-dev.2`,
`v1.103.0-rc.1`), so the xAI Live Search line in `configs/presets.yaml` stays commented
until a stable 1.103.0 ships and we move off 1.96.2.
See the root-cause note at the end — the fix is a deletion, not a remapping.

*What the comparison showed.* #38254's source change is byte-identical to #37128's,
which is not evidence of copying: there is exactly one minimal fix. Everything else was
theirs — a regression test that injects an `httpx.MockTransport`, so it runs the real
HTTP path where ours mocked `client.post`, and proof through a live proxy on both
`/v1/chat/completions` and `/v1/responses`, which their issue template asks for. What
they took from us was the diagnosis and the reproduction, credited via `Fixes #37127`.
**For litellm, the issue is the delivery vehicle and the PR adds nothing:** 0 of their
last 100 merges came from a fork (measured 2026-09-21).

With `web_search_options` set, litellm turns the request's system message into an
`instructions` field and then rejects that field as unsupported for XAI. The
system message is fine without search, and search is fine without a system
message; only the combination fails.

```python
import litellm, os

msgs = [
    {"role": "system", "content": "Answer briefly."},
    {"role": "user", "content": "Newest litellm version on PyPI?"},
]
litellm.completion(
    model="xai/grok-4.3",
    messages=msgs,
    web_search_options={"search_context_size": "medium"},
    api_key=os.environ["GROK_API_KEY"],
)
# litellm.UnsupportedParamsError: LlmProviders.XAI does not support
# parameters: {'instructions': 'Answer briefly.'}
```

Measured 2026-08-16 against litellm 1.96.2, grok-4.3:

| messages | `web_search_options` | result |
|---|---|---|
| user only | no | OK |
| user only | yes | OK |
| system + user | no | OK |
| system + user | **yes** | **UnsupportedParamsError** |

`drop_params` is not a workaround: it would drop `instructions`, which is the
system prompt, so the call would succeed having silently discarded the
instructions the whole review depends on.

**Why it matters here:** xAI Live Search genuinely works — asked for the newest
litellm release on PyPI, grok answered "I do not know" without it and returned a
correct, cited version with it. Every prompt this pipeline sends is a system
prompt, so the capability is unreachable through litellm until this is fixed.
`configs/presets.yaml` carries the enabling line commented out with a pointer
here.

Anthropic's equivalent works and is enabled, which is what makes this look like
a litellm transformation bug rather than an xAI limitation.

**Root cause, 2026-08-16 — this entry guessed the mechanism right and the cause
wrong.** The routing guess was correct: `main.py`'s `responses_api_bridge_check()`
forces xAI + `web_search_options` onto the Responses API, and the bridge hoists a
string system message into `instructions`. But the fix is not to stop that hoist.
`XAIResponsesAPIConfig` declares `instructions` unsupported, and **that claim is
simply false** — xAI's API reference lists it, and three direct calls to
`api.x.ai/v1/responses` confirmed it: `instructions` alone returns HTTP 200 and is
obeyed (asked to answer only "BANANA", grok answered `BANANA`) and echoed back,
and it works alongside a server-side `web_search` tool. So the fix is deleting the
exclusion, not special-casing the shared bridge. Reproduced on 1.96.2 *and* on
master `973329e98`, so it is not stale.

Worth recording as the same shape as entries 2 and 3: the entry's *diagnosis*
("reusing a Responses-API-shaped mapping") described real code and still pointed
at the wrong repair. Reading the provider's source and then asking the provider's
API is what separated the two.

Two details the PR turns on. `map_openai_params` *also* popped `instructions`,
with a debug log — dead code, because `_check_valid_arg` raises first; whoever
wrote the config expected a silent drop and got a hard failure instead. And the
harm from `drop_params` is now measured rather than predicted: with it set, the
call succeeds and grok returns a cited paragraph plus `pip install` instructions
in place of the bare version number the system prompt demanded. Silent, and
exactly what this entry warned about.

The exclusion (`6fb0a8fc`, Nov 2025) predates the web-search routing
(`dbc80061`, Jan 2026) that made it reachable from `completion()` — neither
change was wrong on its own, which is why nobody caught it.

---

## 6. litellm — `responses()` accepts `response_format` and silently ignores it

**Status:** filed as
[BerriAI/litellm#37125](https://github.com/BerriAI/litellm/issues/37125) with
[PR #37126](https://github.com/BerriAI/litellm/pull/37126), both 2026-08-16.
Still unreviewed on 2026-09-21, when the PR was retargeted to `main`; see
*Five weeks on* below.
**Repo:** BerriAI/litellm (tested against 1.96.2, re-checked on `main` 2026-09-21)

`response_format` is the spelling every other litellm surface uses. On the
Responses API it is accepted and does nothing. The call succeeds, the caller
believes a schema is enforced, and it is not. Measured 2026-08-16 against
`gpt-5.4-mini`, same prompt, asking for a strict schema with a top-level `flags`
array:

| request | outcome |
|---|---|
| no format specified | model invents its own shape: `{"ai_speak": ..., "suggestion": ...}` |
| `text={"format": {"type": "json_schema", "name": ..., "strict": True, "schema": ...}}` | **exact requested schema** |
| `response_format={"type": "json_schema", "json_schema": {...}}` | call succeeds, schema **ignored** — own shape again |

**Root cause**, confirmed by reading their source rather than inferred from the
symptom: `responses()` has no `response_format` parameter, so it lands in
`**kwargs`, and `get_requested_response_api_optional_param` then narrows to the
keys declared on `ResponsesAPIOptionalRequestParams`
(`base_pre_process_non_default_params` keeps a key only `if k in
default_param_values`). `response_format` is not declared there, so it is
removed *before* `_check_valid_arg` runs — which is what makes it silent rather
than loud. Their guard is fine; hand it the parameter directly and it raises
exactly the `UnsupportedParamsError` a caller should have seen. Two consequences
worth recording: `drop_params=False` does not mean what it says on this path,
and the same silent drop applies to **any** undeclared key, not just this one.

**Proposed change (and the PR):** map `response_format` onto `text.format`.
This is not new machinery — `convert_text_format_to_text_param` already performs
that exact conversion for litellm's own `text_format=` parameter, and
`type_to_response_format_param` returns a `response_format`-shaped dict
unchanged when handed one. The parameter simply never got routed into it.
Precedence is `text` > `text_format` > `response_format`, so both existing
spellings behave as before. The PR also covers two `KeyError`s reachable in that
helper today via `text_format`: the schema-less forms (`{"type":
"json_object"}`, `{"type": "text"}`) and a `json_schema` block with no `strict`.

**Scoping correction, found while building the fix.** The first draft of this
entry assumed the drop was provider-independent, because the filter runs before
any provider mapping. It is not. Only providers with a native responses config
take that path:

| provider | responses config | `response_format` |
|---|---|---|
| openai / azure / xai | `OpenAIResponsesAPIConfig` etc. | **silently dropped** |
| anthropic / gemini / mistral | `None` | works |

Where the config is `None`, `responses()` falls through to the chat-completions
bridge and `response_format` reaches `completion()`, which supports it — so it
works there by accident. This makes the symptom worse than first reported: the
same call honours the schema on `anthropic/claude-sonnet-5` and ignores it on
`openai/gpt-5.4-mini`, which reads as a model capability difference. Caught only
because the live before/after was run on Anthropic first and *both* sides
returned the schema; re-running on `xai/grok-4.3` (native path) reproduced it
cleanly and proved the fix. The correction is
[a comment on the issue](https://github.com/BerriAI/litellm/issues/37125#issuecomment-5310724905).

**Why it matters here.** The old adapter carried a comment saying "the Responses
API has no `response_format`", and that was repeated into a merged PR as fact.
The parameter exists, is accepted, and does nothing — so probing the capability
the obvious way *confirms* the wrong conclusion. Structured output was available
the whole time under a different name. This is the same failure mode as entry 1
and the waybackpy half of entry 3: a condition reported as the wrong kind of
thing, sending the reader somewhere there is nothing to find.

The test note is the reusable part. A test asserting the call succeeds passes
against the broken version, which is presumably how this survived; the PR's
tests assert on the params bound for the provider, and 8 of its 11 fail
without the fix.

**Five weeks on (2026-09-21).** No maintainer has reviewed it. The one staff
touch was mechanical: on 2026-09-13 `litellm_internal_staging` was deleted and
recreated, which auto-closed the PR, and it was reopened the same morning.
Meanwhile litellm had moved its default branch back to `main`, now 3,081 commits
ahead of staging and the base for 36 of the 39 fork PRs among the 60 most
recently opened, so this one was sitting on an abandoned branch. A PR can go
stale because its base is abandoned, not only because it conflicts; check the
default branch on every revisit.

The bug is unchanged on `main` (`1cac8bd9ab`), re-checked live against
`xai/grok-4.3`: `response_format` is still ignored and the model answers in
prose. Retargeting to `main` hit three CI obstacles, none in the fix itself:

- **Their type-discipline gate.** LIT002 (mutable-collection construction) on
  `main` is already over its own ceiling, 26724 against 26715, so the gate
  rejects any net-new dict literal, and the converter added five. Rebuilt with
  the forms their checker exempts, TypedDict-annotated literals and
  `MappingProxyType`, rather than suppressed.
- **The branch predated their CI.** `test-linting.yml` checks out the PR head
  and runs `.github/actions/detect-changes`, which landed on `main` on
  2026-08-19, after this branch was cut, so `lint` failed before it reached our
  code. Merging `main` in fixed that without a force-push, so every commit hash
  cited in the PR is still valid.
- **Their test-quality gate.** It counts `sys.path.insert` under TQ003, and
  `main` is already over that ceiling too, 63 against 62. The line had been
  copied from the sibling `text_format` test, which `main` had since cleaned up
  the same way.

Two of the three were ratchets that `main` itself already exceeds, so an outside
PR can land only by adding none of whatever those gates count. Running their
gate scripts locally before pushing is cheaper than finding each one in CI.

One correction of our own: the PR text said "12 passed" and "5 of the 12 fail
without the fix". The new test file has 11 tests and 8 of them fail without the
fix; 12 was the total including a pre-existing test. Corrected in the PR body
and in both comments that repeated it, each with a visible note.

With all three cleared, CI on the tip (`b2ef9d5b64`) is green against `main`:
89 checks passed, 1 skipped, none failed. Mechanically the PR is as ready as an
outside contribution gets, with the CLA signed, Greptile at 5/5 and CI green, and
it is still waiting on a human. Whether one comes is a separate question: outside
PRs are about 3% of litellm's merges (see entry 1), and entry 5 landed the other
way, with their bot reimplementing the fix from the issue. So #37125 is as likely
a route to a fix as the PR, and both carry the same diagnosis.

---

## 7. pytest-socket — fixture teardown runs after the guard is lifted

**Status:** filed as
[miketheman/pytest-socket#537](https://github.com/miketheman/pytest-socket/issues/537),
2026-09-18, offering the PR. Worked around here by the `pytest_runtest_teardown`
in `pytest_plugins/socket_guard.py`, which is written to be deleted when this
ships.
**Repo:** miketheman/pytest-socket (tested against 0.8.1, the latest release;
`main` has not touched the hook since, checked 2026-09-18)

A test run under `--disable-socket` or `--allow-hosts` is guarded from its
fixtures' setup through its call, and then not at all while its fixtures are
torn down. pytest-socket lifts its restrictions in its own teardown hook:

```python
def pytest_runtest_teardown() -> None:
    _remove_restrictions()
```

pytest runs the fixture finalizers from `_pytest.runner.pytest_runtest_teardown`
(`item.session._setupstate.teardown_exact(nextitem)`). Both are plain hook
implementations, pluggy calls those newest-registered first, and pytest-socket —
loaded from its entry point or with `-p` — always registers after pytest's
built-in runner. So the lift always runs first, and every finalizer runs with
the network open. Their tracker has nothing on it (issues and PRs searched
2026-09-18 for teardown, finalizer, "fixture teardown", "yield fixture",
trylast, runtest_teardown). The nearest is PR #90, which moved the
`socket_enabled`/`socket_disabled` restores out of those fixtures and into this
hook.

**Reproduction** (`pytest --disable-socket --allow-hosts=127.0.0.1`):

```python
import socket

import pytest


def dial():
    try:
        with socket.socket() as sock:
            sock.connect(("0.0.0.0", 0))  # refused by the OS at once: nothing leaves the machine
    except (RuntimeError, OSError) as exc:
        return type(exc).__name__


@pytest.fixture
def fixture():
    print("setup:", dial())  # SocketConnectBlockedError
    yield
    print("teardown:", dial())  # OSError: connect() really ran


def test_it(fixture):
    print("call:", dial())  # SocketConnectBlockedError
```

Measured 2026-09-18 on pytest 8.4.2 and 9.1.1, pluggy 1.6.0: function-, class-,
module- and session-scoped yield fixtures, `request.addfinalizer` callbacks and
xunit `teardown_module` all reach a real `connect()` at teardown, while setup
and call are blocked. Loading it with `-p pytest_socket` and autoloading off
changes nothing.

**Proposed change:** `@pytest.hookimpl(trylast=True)` on
`pytest_runtest_teardown`, so the restrictions come off after the runner has
run the finalizers. Verified on a copy of 0.8.1 with only that line added: every
teardown above is then blocked, and pluggy calls the runner before
pytest-socket. Since nothing lifts the restrictions in between, teardown runs
under exactly the ones `pytest_runtest_setup` chose for that test — markers and
the `socket_enabled`/`socket_disabled` fixtures included. The test that goes
with it has to dial from a fixture's teardown: every test of the call phase
passes today.

**A second, smaller gap the one-liner leaves.** After a *teardown* error under
`-x` or `--maxfail`, pytest decides to stop only once that teardown has run with
`nextitem` set, so the fixtures the next test would have shared are torn down
later, by `_pytest.runner.pytest_sessionfinish` (`teardown_exact(None)`) —
outside any test, and so outside the guard. A setup or call failure does not
leave it: pytest then tears everything down in that test's own teardown
(pytest-dev/pytest#11706). A `pytest_sessionfinish` wrapper that applies the
global restrictions around it closes this; `socket_guard.py` has one. The issue
raises it as a separate case rather than bundling it with the fix above.

**Why it matters here:** a green run under `--disable-socket` was being read as
"this suite does not touch the network", and teardown is exactly where cleanup
code — closing a client, flushing an upload — makes its calls. The local
workaround is held to `packages/ci-article-review/tests/test_socket_guard.py`,
whose teardown tests all fail without it.

---

## 8. pytest-socket — name lookups go unguarded under an allow-list

**Status:** `ready`. This is a capability pytest-socket never claimed for this
mode, so by the rule above it belongs on the existing thread rather than in a
new issue: [miketheman/pytest-socket#43](https://github.com/miketheman/pytest-socket/issues/43)
asked for lookups to be blocked, and asked how that should interact with
`socket_allow_hosts()`. PR #482 closed it for `--disable-socket` alone. #43 is
closed, so this goes either as a comment there or as a feature request that
links it. Worked around here by the lookup guard in
`pytest_plugins/socket_guard.py`, which is written to be deleted when this
ships.
**Repo:** miketheman/pytest-socket (tested against 0.8.1, the latest release;
`main` unchanged here as of 2026-09-18)

Since 0.8.0, `--disable-socket` guards `socket.getaddrinfo` and
`socket.gethostbyname` as well as `socket.socket`. Under `--allow-hosts` both
stay open: `socket_allow_hosts()` patches `socket.socket.connect` and nothing
else, so every name a test looks up goes to the real resolver. An allow-list is
how a suite keeps loopback usable (this one's database and logging tests bind
local sockets), so a suite like that runs in exactly the mode where lookups go
unguarded. Their tracker has nothing else on it: issues and PRs were searched
2026-09-18 for getaddrinfo, DNS, gethostbyname, resolve and allow_hosts. #412,
about allow-listed hostnames resolving to new addresses at runtime, is the
opposite problem, and was closed as stale.

**Reproduction** (`pytest --disable-socket --allow-hosts=127.0.0.1`):

```python
import socket


def test_it():
    socket.getaddrinfo("example.com", 443)  # not blocked: the real resolver answers
```

With `--disable-socket` alone, the same test fails with `SocketBlockedError: A
test tried to use socket.getaddrinfo.`

**Measured 2026-09-18** on this repo's suite (3,145 tests, run with
`--allow-hosts=127.0.0.1,::1`) with a plugin that records every lookup and the
test that made it: 23 tests asked the real resolver about a name. With every
such lookup made to fail, three of them failed. One had already failed a real
run, when a network blip left example.com unresolved. None of the 23 looks a
name up itself: each reached the resolver through an SSRF check that resolves
a URL's host before fetching it.

**Proposed change:** in `socket_allow_hosts()`, alongside the `connect` patch,
guard `getaddrinfo` and `gethostbyname` so they pass through only lookups that
need no nameserver (an address literal, `None` or `""`, `localhost`) and the
hostnames on the allow-list, and raise `SocketBlockedError` naming the host
for any other. `_remove_restrictions()` then restores them with the identity
check #482 added. `socket_guard.py` does exactly this from outside, by wrapping
`socket_allow_hosts` and `_remove_restrictions`. Its tests in
`packages/ci-article-review/tests/test_socket_guard.py` cover the exemptions,
and the guard's lifecycle across markers, fixtures, collection and teardown.
