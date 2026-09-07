"""Tell the author which handoff fields were missing and what each one cost.

``handoff_parser`` already warns when a field is absent, but those warnings go
to the log, and the log is not what an author reads — the report is. Worse,
they say what is *absent* without saying what it *cost*: "The review models
will receive an empty primary_claim" describes a variable, not a consequence,
and a reader who has never seen the prompt templates cannot turn it into a
decision.

This module turns each absence into the sentence the author actually needs:
which field, what it degraded (named domains, named report sections, measured
counts where a count exists), and what to write instead. Where the run can
infer a decent candidate — the audience from the publication config, the
sources from the draft's own citation block, the history key from the title —
the candidate is offered as text to paste, so the fix is an acceptance rather
than a writing task.

**Nothing here is ever fed back into the run.** ``assess`` reads the handoff
and returns new dicts; it does not mutate its arguments, and the pipeline
calls it *after* the review, with the report already built. A proposed
``primary_claim`` that the author has not accepted must never reach a model
prompt, because a review conducted against a claim the pipeline invented for
itself is worse than one conducted against no claim at all: the first is
confidently wrong about what the piece argues, the second is merely
uninformed. Closing the loop means handing the author the missing line, not
filling it in for them.

Fields are only listed here when their absence actually changes something. The
handoff's ``Publication:`` line, for instance, is deliberately absent from the
table below: the draft pipeline resolves its publication config from the
``--publication`` argument and never reads that line, so reporting it as a gap
would be reporting a cost the run does not pay.
"""

import re

from .adapters.citation.draft_citations import DraftCitations
from .consolidation import _DOMAIN_SECTIONS


#: Ranking for the report. ``critical`` fields fill a prompt template variable
#: directly, so their absence changed the question the models were asked;
#: ``degrading`` fields are context the models would have used but can reason
#: without; ``advisory`` fields affect run bookkeeping (history continuity,
#: which model is excluded from voice) rather than review quality.
_SEVERITY_ORDER = {"critical": 0, "degrading": 1, "advisory": 2}

#: Longest candidate we will paste into the report. A proposal the author has
#: to scroll past is one they will skip, and the point of the candidate is that
#: accepting it is cheaper than writing the field.
_MAX_SUGGESTION_CHARS = 600

#: How many citation entries the proposed SOURCES block lists before saying how
#: many it left out. A 73-marker draft should not paste 73 lines into a report,
#: and it must not paste 20 as though that were all of them either.
_MAX_PROPOSED_SOURCES = 20

#: An unfilled template placeholder — "[One or two sentences: ...]" left where
#: the claim should be. This is worse than an empty field, because the bracket
#: text *is* sent to the models as the primary claim, so the run looks fully
#: specified while every domain reasons about an instruction to the author.
#: Matched only when the whole value is bracketed: a real claim that happens to
#: contain a bracketed aside must not trip this.
_PLACEHOLDER_RE = re.compile(r"^\s*\[[^\]]*\]\s*$", re.DOTALL)

#: Lines the "first substantive paragraph" scan skips when looking for a
#: primary-claim candidate: headings, images, blockquotes, list bullets, rules
#: and the citation block's own marker lines.
_SKIP_LINE_RE = re.compile(r"^\s*(#{1,6}\s|!\[|>\s|[-*+]\s|\d+\.\s|-{3,}\s*$|\[\d+\])")

#: A block opening in italics or bold — an author disclosure, editor's note or
#: standfirst. Only the opening is matched, because the emphasis is routinely
#: left unclosed across the paragraph break that ends the block.
_EMPHASISED_RE = re.compile(r"^\s*[*_]{1,2}[^*_\s]")

#: Standing boilerplate that opens an article without being any part of its
#: argument. Both of these came out of real runs, and the emphasis rule above
#: catches neither on its own:
#:
#: * a markdown draft opened with an italicised conflict-of-interest note, and
#: * the same author's *published* page opened with an AI-use disclosure that
#:   reached the extractor as plain text, because ``extract_article`` strips
#:   the HTML emphasis that marked it up as an aside.
#:
#: Deliberately narrow — these are set phrases from publishing convention, not
#: a topic filter — and paired with the word ceiling in
#: ``_first_substantive_paragraph`` so a real argument that happens to discuss
#: disclosure is never dropped for saying the word.
_DISCLOSURE_RE = re.compile(
    r"(\bai\b[^.]{0,40}?\b(was|were)\s+(used|involved)"
    r"|(written|drafted|edited)\s+with\s+(the\s+)?(assistance|help|aid)\s+of"
    r"|\bai[- ](assisted|generated)"
    r"|views expressed"
    r"|no financial (position|interest|stake)"
    r"|^\s*(disclosure|disclaimer|editor'?s note|correction)\s*[:—-])",
    re.IGNORECASE,
)

#: Not case-insensitive, deliberately: "I" is matched only as a capital,
#: because a case-blind "i" matches the i in "i.e." and would count an
#: abbreviation as a personal claim. The others are matched in both cases
#: since they legitimately open a sentence.
#:
#: First-person *singular* only. "we" and "our" are routinely editorial or
#: corporate ("we asked the county", "our coverage area") and attributing those
#: to one named person would be wrong more often than right; "I" is the pronoun
#: that actually needs a name behind it before a claim about the writer can be
#: checked against a page.
_FIRST_PERSON_RE = re.compile(r"\b(?:I|[Mm]e|[Mm]y|[Mm]ine|[Mm]yself)\b")

#: Section 9 is produced by a pass, not by a review domain, so it has no entry
#: in ``_DOMAIN_SECTIONS`` to derive a title from.
_CITATION_SECTION = "SECTION 9: Citations"

#: A disclosure is a short note. Above this, a paragraph is doing real work and
#: is never dropped for matching a phrase above.
_DISCLOSURE_MAX_WORDS = 60


def _is_missing(value):
    """True when a field is absent, blank, or still holding its placeholder."""
    if not value:
        return True
    text = str(value).strip()
    if not text:
        return True
    return bool(_PLACEHOLDER_RE.match(text))


def _is_placeholder(value):
    """True only for the left-in-the-template case, so it can be named as such."""
    return bool(value) and bool(_PLACEHOLDER_RE.match(str(value).strip()))


def _truncate(text, limit=_MAX_SUGGESTION_CHARS):
    """Cut at a sentence boundary if there is one, else at a word boundary."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    for boundary in (". ", "? ", "! "):
        cut = head.rfind(boundary)
        if cut > limit // 2:
            return head[: cut + 1].strip()
    return head[: head.rfind(" ") if " " in head else limit].strip() + " …"


def _first_substantive_paragraph(draft):
    """The draft's opening prose paragraph, or "" if there is none.

    Used only as a *candidate* primary claim. An opening paragraph is not a
    claim — it is usually the setup for one — which is why the report says so
    beside it rather than presenting it as an answer.

    Opening boilerplate is skipped, by two rules that both came from real
    runs rather than from imagination:

    * A 135K-char markdown draft opened with an *italicised* conflict-of-
      interest note, and the first-long-prose-block rule proposed it as the
      article's claim. Hence ``_EMPHASISED_RE``.
    * The same author's published page — the ``--url`` path — opened with an
      AI-use disclosure that arrives as plain text, because the extractor
      strips the HTML emphasis marking it as an aside. Hence
      ``_DISCLOSURE_RE``, which the italic rule cannot reach.

    Neither rule can be exhaustive, which is why the report labels what it
    returns a starting point and tells the author to cut it down.
    """
    for block in re.split(r"\n\s*\n", draft or ""):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        prose = [ln for ln in lines if not _SKIP_LINE_RE.match(ln)]
        if not prose:
            continue
        para = " ".join(ln.strip() for ln in prose)
        words = para.split()
        # A one-line fragment is a subtitle or a byline, not the opening.
        if len(words) < 15:
            continue
        if _EMPHASISED_RE.match(para):
            continue
        if len(words) <= _DISCLOSURE_MAX_WORDS and _DISCLOSURE_RE.search(para):
            continue
        return para
    return ""


def _pub_audience_lines(pub_config):
    """``Primary:``/``Secondary:`` lines built from the publication config.

    The publication's ``audience`` block is what the review prompts already
    fall back to for ``{audience}``, so proposing it here is not an invention —
    it is naming the description the models actually used, in the shape the
    handoff wants it in.
    """
    audience = (pub_config or {}).get("audience")
    if not audience:
        return []
    if isinstance(audience, str):
        text = audience.strip()
        return [f"Primary: {_truncate(text)}"] if text else []
    lines = []
    for key, label in (("primary", "Primary"), ("secondary", "Secondary")):
        value = str(audience.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {_truncate(' '.join(value.split()))}")
    return lines


def _slug(title):
    """The history directory this title actually produces.

    Delegates to ``history._slug`` rather than reimplementing it: the point of
    the ``History key:`` gap is to name the directory the run filed itself
    under, and a second copy of the slug rules would eventually name a
    different one. Imported lazily because ``history`` pulls in
    ``report_markdown``, which has no business being loaded to assess a
    handoff.
    """
    from .history import _slug as _history_slug

    return _history_slug(title or "")


def _join_and(items):
    """ "a", "a and b", "a, b and c" — the report is prose, not a CSV."""
    items = list(items)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _passes(items):
    """ "the fact_check pass" / "the fact_check and red_team passes"."""
    items = list(items)
    if not items:
        return "affected passes"
    return f"{_join_and(items)} pass{'es' if len(items) > 1 else ''}"


def _verb(items, singular, plural):
    """Agree a verb with a list rendered by ``_join_and``."""
    return singular if len(list(items)) == 1 else plural


#: Small counts read better as words, and "both" beats "all 2".
_NUMBER_WORDS = {1: "the one", 2: "both", 3: "all three", 4: "all four", 5: "all five"}


def _ran_phrase(affected, wanted):
    """ "all three ran this run" vs "only completeness ran this run".

    A run narrowed by ``--only-domain`` must not be told a field degraded a
    pass that never executed, so this counts what actually ran rather than
    what the field feeds.
    """
    if not affected:
        return "none of them ran this run"
    if len(affected) == len(wanted):
        return f"{_NUMBER_WORDS.get(len(wanted), f'all {len(wanted)}')} ran this run"
    return f"only {_join_and(affected)} ran this run"


def _domain_list(domains_ran, wanted):
    """The subset of ``wanted`` that actually ran this pass, in report order."""
    ran = set(domains_ran or ())
    return [d for d in wanted if d in ran]


def _sections_for(domains):
    return [_DOMAIN_SECTIONS[d] for d in domains if d in _DOMAIN_SECTIONS]


def _gap(
    field,
    label,
    severity,
    impact,
    *,
    domains=(),
    sections=None,
    suggestion=None,
    basis=None,
    guidance=None,
    placeholder=False,
):
    # ``sections`` is normally derived from ``domains`` — one review domain
    # builds one section. Citation resolution breaks that: it is a pass, not a
    # domain, so a field that only degrades Section 9 has a section to name and
    # no domain to derive it from. Passing it explicitly keeps the per-section
    # note (which keys off ``domains``) correctly silent there.
    return {
        "field": field,
        "label": label,
        "severity": severity,
        "impact": impact,
        "domains": list(domains),
        "sections": _sections_for(domains) if sections is None else list(sections),
        "suggestion": suggestion,
        "suggestion_basis": basis,
        "guidance": guidance,
        "placeholder": placeholder,
    }


#: Which review domains each handoff field actually reaches, read off the
#: prompt templates rather than assumed.
#:
#: Two mechanisms, and the distinction matters for how bad an absence is.
#: ``primary_claim`` and ``pre_draft_analysis`` are *system*-prompt template
#: variables (``{primary_claim}``, ``{pre_draft_analysis}`` in
#: ``prompts/*.txt``) — absent, the domain's standing instructions have a hole
#: in them. Everything else is assembled into the *user* prompt by
#: ``pipeline._build_user_prompt``, which simply omits the line, so the model
#: is uninformed rather than misdirected.
#:
#: The audience entry lists four domains, not the two ``handoff_parser``'s
#: debug note used to name: ``ai_speak`` gates its audience-condescension rule
#: on the AUDIENCE field, ``completeness`` and ``red_team`` both score findings
#: by audience segment, and ``argument_integrity`` is asked where "a skeptical
#: reader from the target audience" would push back.
FIELD_DOMAINS = {
    "primary_claim": ("argument_integrity", "completeness", "red_team"),
    "pre_draft_analysis": ("argument_integrity", "completeness"),
    "target_audience": (
        "voice_style",
        "argument_integrity",
        "completeness",
        "red_team",
    ),
    "sources_cited": ("fact_check", "red_team"),
    "uncertain_sections": ("fact_check", "red_team"),
    "known_gaps": ("completeness",),
    "additional_context": (
        "fact_check",
        "voice_style",
        "argument_integrity",
        "completeness",
        "red_team",
    ),
}


def _title_gap(handoff, draft, domains_ran):
    title = (handoff.get("title") or "").strip()
    if title and title.lower() != "untitled":
        return None
    ran = sorted(domains_ran or ())
    h1 = re.search(r"^#\s+(.+)$", draft or "", re.MULTILINE)
    candidate = h1.group(1).strip() if h1 else ""
    return _gap(
        "title",
        "Article:",
        "critical",
        impact=(
            f"Every review prompt opened with `ARTICLE TITLE: Untitled`. "
            f"All {len(ran)} domain{'s' if len(ran) != 1 else ''} that ran "
            f"({_join_and(ran)}) judged the piece without knowing what it is "
            f"called, so any finding about "
            f"whether the draft delivers on its headline could not be made. "
            f"The title is also the history key when no `History key:` is set, "
            f"so this run filed itself under a generic directory and the next "
            f"run will not find it as a delta baseline."
        ),
        domains=_domain_list(ran, _DOMAIN_SECTIONS),
        suggestion=f"Article: {candidate}" if candidate else None,
        basis="the draft's first `#` heading" if candidate else None,
        guidance=(
            None
            if candidate
            else "Add an `Article:` line with the working headline. The draft "
            "has no `#` heading to take one from."
        ),
        placeholder=_is_placeholder(handoff.get("title")),
    )


def _primary_claim_gap(handoff, draft, domains_ran, has_prior_run):
    value = handoff.get("primary_claim")
    if not _is_missing(value):
        return None
    affected = _domain_list(domains_ran, FIELD_DOMAINS["primary_claim"])
    is_placeholder = _is_placeholder(value)
    if is_placeholder:
        lead = (
            "`PRIMARY CLAIM` was left as its template placeholder, so the "
            "bracketed instruction text was sent to the models *as the claim* — "
            "which is worse than leaving it blank, because the run looks fully "
            "specified while every domain reasons about a note addressed to you."
        )
        consequence = (
            "Each that ran was asked whether the draft establishes a claim that "
            "does not exist, so each fell back to inferring one from the text "
            "and graded the draft against its own inference."
        )
        delta_detail = (
            "the placeholder is what gets compared, so it will read as "
            "unchanged for as long as it is left there"
        )
    else:
        lead = "`PRIMARY CLAIM` was empty."
        consequence = (
            "Each that ran was asked whether the draft establishes its claim "
            "while being shown no claim, so each inferred one from the text and "
            "graded the draft against its own inference."
        )
        delta_detail = (
            "an empty field is treated as unchanged rather than as a change it "
            "could not measure"
        )
    delta_note = (
        f" The delta against the prior run also cannot report whether the claim "
        f"moved: `claim_changed` compares this field against the last run's, and "
        f"{delta_detail}."
        if has_prior_run
        else ""
    )
    candidate = _truncate(_first_substantive_paragraph(draft))
    return _gap(
        "primary_claim",
        "PRIMARY CLAIM",
        "critical",
        impact=(
            f"{lead} It is a system-prompt variable for "
            f"{_join_and(FIELD_DOMAINS['primary_claim'])}, and "
            f"{_ran_phrase(affected, FIELD_DOMAINS['primary_claim'])}. "
            f"{consequence} Findings in "
            f"{_join_and(_sections_for(affected)) or 'the affected sections'} "
            f"are about a claim you did not state.{delta_note}"
        ),
        domains=affected,
        suggestion=f"PRIMARY CLAIM\n{candidate}" if candidate else None,
        basis=(
            "the draft's opening paragraph — a starting point, not the claim: "
            "the opening usually sets a claim up rather than stating it, so "
            "cut this to the one thing the piece argues before pasting"
            if candidate
            else None
        ),
        guidance=(
            None
            if candidate
            else "State in one or two sentences the single thing this article "
            "argues — the claim, not the topic."
        ),
        placeholder=is_placeholder,
    )


def _target_audience_gap(handoff, pub_config, domains_ran):
    if not _is_missing(handoff.get("target_audience")):
        return None
    affected = _domain_list(domains_ran, FIELD_DOMAINS["target_audience"])
    pub_lines = _pub_audience_lines(pub_config)
    if pub_lines:
        fallback = (
            "Those domains fell back to the publication config's `audience` "
            "block, which describes who reads the publication rather than who "
            "this piece is for — so an audience-specific finding is either "
            "generic or aimed at the wrong reader."
        )
    else:
        fallback = (
            "The publication config supplies no `audience` block either, so no "
            "audience description reached any prompt at all: every "
            "audience-scoped judgement below was made against a reader the "
            "models invented."
        )
    return _gap(
        "target_audience",
        "TARGET AUDIENCE",
        "degrading",
        impact=(
            f"`TARGET AUDIENCE` was empty, so no `TARGET AUDIENCE` line appeared "
            f"in any domain's prompt. Of the domains that ran, "
            f"{_join_and(affected) or 'none'} "
            f"{_verb(affected, 'reasons', 'reason')} explicitly about audience "
            f"— voice_style gates its condescension check on it, completeness "
            f"and red_team score findings by reader segment, and "
            f"argument_integrity is asked where a skeptical reader would push "
            f"back. {fallback}"
        ),
        domains=affected,
        suggestion=("TARGET AUDIENCE\n" + "\n".join(pub_lines) if pub_lines else None),
        basis=(
            "the publication config's `audience` block — the description these "
            "domains actually fell back to; narrow it to this piece's reader"
            if pub_lines
            else None
        ),
        guidance=(
            None
            if pub_lines
            else "Name the reader by job title and what they will do with the "
            "piece, specifically enough that a model can tell when the draft "
            "talks past them."
        ),
        placeholder=_is_placeholder(handoff.get("target_audience")),
    )


def _pre_draft_analysis_gap(handoff, domains_ran):
    if not _is_missing(handoff.get("pre_draft_analysis")):
        return None
    affected = _domain_list(domains_ran, FIELD_DOMAINS["pre_draft_analysis"])
    return _gap(
        "pre_draft_analysis",
        "PRE-DRAFT ANALYSIS SUMMARY",
        "degrading",
        impact=(
            f"`PRE-DRAFT ANALYSIS SUMMARY` was empty. It is a system-prompt "
            f"variable for {_join_and(FIELD_DOMAINS['pre_draft_analysis'])}, and "
            f"{_ran_phrase(affected, FIELD_DOMAINS['pre_draft_analysis'])}. "
            f"Without it they do not know which objections you already "
            f"weighed, so findings you have considered and rejected come back "
            f"as new ones — "
            f"and will come back again on every re-run until the field is filled."
        ),
        domains=affected,
        guidance=(
            "Fill in at least `Steelmanned position:` and `Strawmanned "
            "position:` — your own argument at its strongest, and the weak link "
            "you expect to be challenged on. Nothing in the draft can be read "
            "to infer these, which is why no candidate is proposed."
        ),
        placeholder=_is_placeholder(handoff.get("pre_draft_analysis")),
    )


def _sources_cited_gap(handoff, draft, domains_ran):
    if not _is_missing(handoff.get("sources_cited")):
        return None
    affected = _domain_list(domains_ran, FIELD_DOMAINS["sources_cited"])
    cited = DraftCitations(draft or "")
    count = cited.marker_count
    if count:
        plural = "" if count == 1 else "s"
        measured = (
            f"The draft's own citation block carries {count} marker{plural} that "
            f"fact_check therefore spent budget rediscovering, and can re-raise "
            f"as needing a source."
        )
        shown = list(cited.entries.values())[:_MAX_PROPOSED_SOURCES]
        listed = [
            f"- {_truncate(entry.get('text', '').strip(), 200)}" for entry in shown
        ]
        if len(shown) < count:
            listed.append(
                f"- ...and {count - len(shown)} more in the draft's citation "
                f"block, not listed here."
            )
        suggestion = "SOURCES ALREADY CITED\n" + "\n".join(listed)
        basis = (
            f"the draft's own citation block ({count} marker{plural}); check it "
            f"is complete before pasting"
        )
        guidance = None
    else:
        measured = (
            "The draft carries no citation block either — that block is found "
            "by its heading (Sources, References, Citations, Works Cited, Notes "
            "or Bibliography), so bracketed markers alone do not make one — "
            "which leaves fact_check with no record of what you already sourced "
            "from any direction."
        )
        suggestion = None
        basis = None
        guidance = (
            'List the sources the draft cites, or write "None provided." if '
            "there genuinely are none."
        )
    return _gap(
        "sources_cited",
        "SOURCES ALREADY CITED",
        "degrading",
        impact=(
            f"`SOURCES ALREADY CITED` was empty, so the {_passes(affected)} "
            f"ran without the list of what you had already sourced. {measured}"
        ),
        domains=affected,
        suggestion=suggestion,
        basis=basis,
        guidance=guidance,
        placeholder=_is_placeholder(handoff.get("sources_cited")),
    )


def _uncertain_sections_gap(handoff, domains_ran):
    if not _is_missing(handoff.get("uncertain_sections")):
        return None
    affected = _domain_list(domains_ran, FIELD_DOMAINS["uncertain_sections"])
    return _gap(
        "uncertain_sections",
        "UNCERTAIN SECTIONS",
        "degrading",
        impact=(
            f"`UNCERTAIN SECTIONS` was empty, so nothing told the "
            f"{_passes(affected)} where to concentrate. Scrutiny was spread "
            f"evenly over the draft "
            f"instead of aimed at the passages you are least sure of — the "
            f"budget went where the models chose, not where you wanted it."
        ),
        domains=affected,
        guidance=(
            "Name the passages you are not confident about. These passes are "
            'told to focus there. Write "None identified by author." if you '
            "are confident throughout."
        ),
        placeholder=_is_placeholder(handoff.get("uncertain_sections")),
    )


def _known_gaps_gap(handoff, domains_ran):
    if not _is_missing(handoff.get("known_gaps")):
        return None
    affected = _domain_list(domains_ran, FIELD_DOMAINS["known_gaps"])
    return _gap(
        "known_gaps",
        "KNOWN GAPS",
        "degrading",
        impact=(
            f"`KNOWN GAPS` was empty, so the {_passes(affected)} could not "
            f"tell a gap you had already accepted from one you had missed. "
            f"Omissions you made "
            f"deliberately are reported back as findings, and will be reported "
            f"again on every re-run until the field says otherwise."
        ),
        domains=affected,
        guidance=(
            "List what you know is missing and why you left it out — the pass "
            "then judges whether the omission is acceptable instead of simply "
            'reporting it. Write "None identified by author." if nothing is.'
        ),
        placeholder=_is_placeholder(handoff.get("known_gaps")),
    )


def _additional_context_gap(handoff, domains_ran):
    if not _is_missing(handoff.get("additional_context")):
        return None
    affected = _domain_list(domains_ran, FIELD_DOMAINS["additional_context"])
    return _gap(
        "additional_context",
        "ADDITIONAL CONTEXT FOR REVIEW MODELS",
        "advisory",
        impact=(
            f"`ADDITIONAL CONTEXT FOR REVIEW MODELS` was empty. All "
            f"{len(affected)} domain{'s' if len(affected) != 1 else ''} that ran "
            f"read this field, so prior "
            f"articles this piece builds on, objections your readers have "
            f"raised before, and where the piece has to survive were all absent "
            f"from every prompt. Commonly left blank; worth filling when the "
            f"piece has history the draft does not restate."
        ),
        domains=(),
        guidance=(
            "Add anything that changes how the draft should be judged: prior "
            "articles, known reader objections, intended use."
        ),
        placeholder=_is_placeholder(handoff.get("additional_context")),
    )


def _history_key_gap(handoff):
    if not _is_missing(handoff.get("history_key")):
        return None
    title = (handoff.get("title") or "").strip()
    slug = _slug(title)
    # ``history._slug`` falls back to "untitled" for a title too short to make a
    # directory name. Proposing that back as the key would pin every such
    # article to one shared directory — the opposite of what the field is for.
    usable = slug and slug != "untitled"
    return _gap(
        "history_key",
        "History key:",
        "advisory",
        impact=(
            f"No `History key:`, so this run filed itself under the title "
            f"(`{slug or 'untitled'}`). Revise the headline before the next run "
            f"and that run starts a fresh history directory: it loses this run "
            f"as its delta baseline, and ci-voice-patterns counts the two as "
            f"separate articles when deciding whether a phrasing habit recurs."
        ),
        domains=(),
        suggestion=f"History key: {slug}" if usable else None,
        basis=(
            "a slug of the current title — set it once and keep it fixed "
            "however far the headline moves"
            if usable
            else None
        ),
        guidance=(
            None
            if usable
            else "Add a short stable name for this piece. The current title is "
            "too short to slug, so every run under it shares one `untitled` "
            "directory."
        ),
    )


def _drafted_with_gap(handoff, pipeline_cfg, domains_ran):
    """Only a gap when nothing anywhere declares the drafting model.

    ``pipeline.drafting_model`` in user.yaml is the standing default, so a
    handoff without a ``Drafted with:`` line is fully specified when that is
    set — reporting it would be reporting a gap the config already closed.
    """
    if not _is_missing(handoff.get("drafted_with")):
        return None
    if str((pipeline_cfg or {}).get("drafting_model") or "").strip():
        return None
    if "voice_style" not in set(domains_ran or ()):
        return None
    return _gap(
        "drafted_with",
        "Drafted with:",
        "advisory",
        impact=(
            "No `Drafted with:` line and no `pipeline.drafting_model` in "
            "user.yaml, so no model was excluded from voice_style. If one of "
            "the models below drafted this article, it reviewed its own prose "
            "for AI phrasing — which is asking it to notice its own habits, and "
            "its vote in SECTION 3 should be read accordingly."
        ),
        domains=("voice_style",),
        guidance=(
            "Name the model you drafted with (claude, openai, gemini, mistral, "
            "grok or perplexity) and it is dropped from the voice pass. Omit "
            "the line only if you wrote the piece yourself."
        ),
    )


def _first_person_claims(citations):
    """Resolved citations whose claim speaks in the first person singular."""
    return [
        c
        for c in (citations or [])
        if _FIRST_PERSON_RE.search(str(c.get("claim") or ""))
    ]


def _first_person_sentences(draft):
    """Draft sentences in the first person singular — the offline fallback."""
    return [
        part
        for part in re.split(r"(?<=[.!?])\s+", draft or "")
        if _FIRST_PERSON_RE.search(part)
    ]


def _author_gap(handoff, pub_config, draft, citations):
    """Report a missing author only when the draft actually speaks as one.

    Two gates, both about not crying wolf. The publication config's
    ``author_name`` is a real answer — a single-author publication sets it once
    and every run is covered, including ``--raw-draft`` and ``--url``, which
    carry no handoff metadata — so a handoff without an ``Author:`` line is
    fully specified when that default exists. And a draft with no first-person
    singular in it has nothing for an author name to anchor, so naming the
    field would be reporting a cost the run does not pay.

    What it costs when it *is* missing was measured on 2026-09-04 against
    fd-ix.com/about/team/, a page that does in fact name the author: told
    nothing, the verifier bound "I" to the first person it found on the page
    and offered a stranger's family as evidence; told only not to guess, it
    returned ``not_addressed`` against the page carrying the author's own bio.
    Both failures are silent — one produces a wrong citation, the other an
    unverifiable verdict, and neither says the cause was a missing name.
    """
    if not _is_missing(handoff.get("author")):
        return None
    if str((pub_config or {}).get("author_name") or "").strip():
        return None

    resolved = _first_person_claims(citations)
    if resolved:
        measured = (
            f"{len(resolved)} claim{'' if len(resolved) == 1 else 's'} put through "
            f"citation resolution speak{'s' if len(resolved) == 1 else ''} in the "
            f"first person"
        )
    else:
        sentences = _first_person_sentences(draft)
        if not sentences:
            return None
        measured = (
            f"{len(sentences)} sentence{'' if len(sentences) == 1 else 's'} in the "
            f"draft speak{'s' if len(sentences) == 1 else ''} in the first person"
        )

    return _gap(
        "author",
        "Author:",
        "degrading",
        impact=(
            f"No `Author:` line in the handoff and no `author_name` in the "
            f"publication config, so citation verification was never told who "
            f'"I" is — and {measured}. A first-person claim cannot be checked '
            f"against a page without a name to look for: the verifier either "
            f'binds "I" to whoever the page happens to name, which cites a '
            f"stranger as evidence about you, or refuses to guess and returns "
            f"`not_addressed` against a page that does name you. Both land in "
            f"{_CITATION_SECTION} looking like a sourcing problem in the draft "
            f"rather than a missing field in the handoff."
        ),
        sections=[_CITATION_SECTION],
        guidance=(
            "Set `author_name:` in the publication config if you are its usual "
            "byline — one line, and every run is covered, including --raw-draft "
            "and --url. Use the handoff's `Author:` line only for a guest or "
            "co-authored piece, where it overrides that default."
        ),
        placeholder=_is_placeholder(handoff.get("author")),
    )


def assess(
    handoff,
    *,
    pub_config=None,
    draft="",
    domains_ran=(),
    pipeline_cfg=None,
    has_prior_run=False,
    citations=(),
):
    """Return the handoff gaps for one run, worst first.

    Read-only in both directions: ``handoff`` is never mutated, and no value
    proposed here is written back into it. See the module docstring — the
    candidates exist to be pasted by the author, not applied by the pipeline.

    ``domains_ran`` is the set of review domains this run actually executed, so
    a run narrowed with ``--only-domain`` does not claim a field degraded a
    pass that never happened.

    ``citations`` is the run's resolved Section 9 entries. Only the author gap
    reads them, and only to count how many resolved claims actually speak in
    the first person — the number that says what a missing name cost. Offline
    runs resolve nothing, so it falls back to counting the draft's own
    first-person sentences.
    """
    draft = draft or handoff.get("draft") or ""
    gaps = [
        _title_gap(handoff, draft, domains_ran),
        _author_gap(handoff, pub_config, draft, citations),
        _primary_claim_gap(handoff, draft, domains_ran, has_prior_run),
        _target_audience_gap(handoff, pub_config, domains_ran),
        _pre_draft_analysis_gap(handoff, domains_ran),
        _sources_cited_gap(handoff, draft, domains_ran),
        _uncertain_sections_gap(handoff, domains_ran),
        _known_gaps_gap(handoff, domains_ran),
        _additional_context_gap(handoff, domains_ran),
        _history_key_gap(handoff),
        _drafted_with_gap(handoff, pipeline_cfg, domains_ran),
    ]
    present = [g for g in gaps if g]
    present.sort(key=lambda g: _SEVERITY_ORDER[g["severity"]])
    return present
