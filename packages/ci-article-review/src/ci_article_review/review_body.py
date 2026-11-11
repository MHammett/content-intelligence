"""The body of the pull request review a run posts (``--write-back``, ``ci-docs push-run``).

One run delivers its review twice (design: ``docs/GITHUB-DOCUMENTS.md``, "Delivering
the review"): the whole of it as ``review.md`` committed to the round's branch, and
this, a pull request review whose body the drafting chat reads through its
connector::

    <!-- ci-review run=7 commit=<S> -->     the marker: find this run's review again
    the run, preset, cost, the commit reviewed, and any drift since
    one line per section with its count of findings
    - [ ] **F1** "passage": the finding in a line     Section 1, numbered as review.md
    where the whole review is

``F<k>`` is the number ``review.md`` prints before that finding in Section 1
(``report_markdown.consensus_findings`` numbers both), so the chat's revision
commits can cite findings by it. The worklist is **never** here: it lists documents
nobody has read yet, and the revision model must not see it (``README`` "The
worklist"; it lives only in ``pipeline/worklist.md`` and the WORKLIST block that
opens ``review.md``).

GitHub caps a review body at 65,536 characters (undocumented, consistently
reported). The body is built to stay under ``MAX_BODY``: the per-section lines go
first, then checklist items from the end, whole items, and what was cut is said at
the end. ``review.md`` is never cut.
"""

import re
from dataclasses import dataclass, field

from .report_markdown import _EXPANSION_LAYOUT, _dicts, consensus_findings

#: Under GitHub's 65,536, with room for the characters it may count differently.
MAX_BODY = 60_000

#: The first line of a review the pipeline posted: ``push-run`` finds it again and
#: posts nothing a second time, and tagging reads the run and commit from it.
_MARKER_RE = re.compile(r"^<!-- ci-review run=(\d+) commit=([0-9a-f]{7,40}) -->$")

#: How long the quoted passage and the finding are in a checklist line.
_PASSAGE_CHARS = 120
_GIST_CHARS = 180

#: The keys that hold what a flag says is wrong, best first; they differ by domain.
_GIST_KEYS = ("problem", "logical_problem", "risk", "attack_vector", "issue", "reason")

CAVEAT = (
    "One run is not a measurement: repeat runs over an unedited draft return "
    "substantially different findings, so read these counts as one sample "
    "(“Reading this report” in review.md says more)."
)


def marker(run_number, commit):
    """The line that opens this run's review: ``<!-- ci-review run=7 commit=<S> -->``."""
    return f"<!-- ci-review run={run_number} commit={commit} -->"


def parse_marker(body):
    """``(run_number, commit)`` from the first line of a review body, else ``None``."""
    first = (body or "").split("\n", 1)[0].strip()
    found = _MARKER_RE.match(first)
    return (int(found.group(1)), found.group(2)) if found else None


def is_review_of(body, run_number, commit):
    """Whether ``body`` is the review of this run at this commit."""
    return parse_marker(body) == (run_number, commit)


@dataclass
class RunFacts:
    """What the body says about the run, taken from the report and where it was read."""

    slug: str
    run_number: int
    run_name: str
    reviewed_commit: str
    pull_number: int
    repo: str
    #: Path of ``review.md`` in the repository and the commit that holds it.
    review_path: str
    review_commit: str
    round_number: int | None = None
    preset: str = "unknown"
    cost_usd: float | None = None
    files_read: list = field(default_factory=list)
    #: ``Note: ... changed at its own path since it was read`` lines.
    drift: list = field(default_factory=list)


def _one_line(text, limit):
    """``text`` on one line, at most ``limit`` characters, safe to put in a list item."""
    flat = " ".join(str(text or "").split())
    # A raw "<" could open an HTML comment or tag that swallows the lines after it.
    flat = flat.replace("<", "&lt;")
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _gist(entry):
    """What the finding says, in the words of the first flag that says something."""
    for flag in entry.get("flags") or []:
        if not isinstance(flag, dict):
            continue
        for key in _GIST_KEYS:
            if str(flag.get(key) or "").strip():
                return _one_line(flag[key], _GIST_CHARS)
        if flag.get("domain") == "fact_check":
            kind = str(flag.get("type") or "").strip()
            value = str(flag.get("current_value") or "").strip()
            if kind or value:
                return _one_line(f"{kind}: {value}".strip(": "), _GIST_CHARS)
    return ""


def checklist_item(number, entry):
    """One Section 1 finding as ``- [ ] **F<number>** "passage": the finding``."""
    passage = entry.get("passage")
    if not str(passage or "").strip():
        for flag in entry.get("flags") or []:
            if isinstance(flag, dict) and str(flag.get("claim") or "").strip():
                passage = flag["claim"]
                break
    models = entry.get("models") or []
    gist = _gist(entry) or f"flagged by {len(models)} pass(es)"
    return f'- [ ] **F{number}** "{_one_line(passage, _PASSAGE_CHARS)}": {gist}'


def _count(value):
    return len(_dicts(value)[0]) if isinstance(value, list) else 0


def _red_team_count(red_team):
    if not isinstance(red_team, dict):
        return 0
    keys = (
        "most_vulnerable_claim",
        "highest_audience_risk",
        "highest_credibility_risk",
    )
    if any(k in red_team for k in keys[:2]):
        return sum(isinstance(red_team.get(k), dict) for k in keys)
    return sum(
        isinstance(v.get(k), dict)
        for v in red_team.values()
        if isinstance(v, dict)
        for k in keys
    )


def section_lines(report):
    """One line per section with its count of findings, as the review's headings name them."""
    fact = report.get("section_2_fact_check")
    fact = fact if isinstance(fact, dict) else {}
    buckets = (
        "confirmed",
        "outdated",
        "contradicted",
        "unverifiable",
        "primary_source_needed",
    )
    claims = sum(_count(fact.get(b)) for b in buckets)
    unconfirmed = claims - _count(fact.get("confirmed"))
    expansion = report.get("section_10_expansion")
    expansion = expansion if isinstance(expansion, dict) else {}
    proposed = sum(
        _count(expansion.get(b)) for b, _heading, _field in _EXPANSION_LAYOUT
    )
    rows = (
        ("Section 1, consensus flags", str(len(consensus_findings(report)))),
        ("Section 2, fact-check", f"{claims} claim(s), {unconfirmed} not confirmed"),
        ("Section 3, voice and AI-speak", str(_count(report.get("section_3_voice")))),
        (
            "Section 4, argument integrity",
            str(_count(report.get("section_4_argument"))),
        ),
        (
            "Section 5, completeness and framing",
            str(_count(report.get("section_5_completeness"))),
        ),
        ("Section 6, red team", str(_red_team_count(report.get("section_6_red_team")))),
        (
            "Section 7, low confidence",
            str(_count(report.get("section_7_low_confidence"))),
        ),
        (
            "Section 8, additional observations",
            str(_count(report.get("section_8_additional"))),
        ),
        ("Section 9, citations", str(_count(report.get("section_9_citations")))),
        ("Section 10, expansion candidates", str(proposed)),
    )
    return [f"- {label}: {count}" for label, count in rows]


def _head(facts):
    cost = (
        f"${facts.cost_usd:.2f} reported"
        if isinstance(facts.cost_usd, (int, float))
        else "cost unknown"
    )
    where = f", round {facts.round_number}" if facts.round_number is not None else ""
    lines = [
        marker(facts.run_number, facts.reviewed_commit),
        f"## ci-review: {facts.slug}{where}, run {facts.run_number}",
        "",
        f"- **Run:** `{facts.run_name}`, preset {facts.preset}, {cost}",
        f"- **Reviewed:** {', '.join(facts.files_read) or 'the article'} at "
        f"`{facts.reviewed_commit[:7]}` (PR #{facts.pull_number})",
    ]
    if facts.drift:
        lines.append(
            f"- **This review is of `{facts.reviewed_commit[:7]}`**, not of the "
            "branch as it stands:"
        )
        lines.extend(f"  - {line}" for line in facts.drift)
    lines += ["", f"_{CAVEAT}_", ""]
    return lines


def _foot(facts):
    url = (
        f"https://github.com/{facts.repo}/blob/{facts.review_commit}/"
        f"{facts.review_path}"
    )
    return [
        "### The whole review",
        "",
        f"[`{facts.review_path}`]({url}) at `{facts.review_commit[:7]}` has every "
        "section. To read it through the connector, call `get_file_contents` with "
        f"`path` `{facts.review_path}` and `ref` `refs/pull/{facts.pull_number}/head`.",
    ]


def build_review_body(report, facts, *, limit=MAX_BODY):
    """The pull request review's body for ``report``, at most ``limit`` characters.

    Over the limit, the per-section lines are left out first and then checklist
    items, from the end and whole, with a closing note saying what was cut.
    """
    items = [checklist_item(k, entry) for k, entry in consensus_findings(report)]
    head, foot = _head(facts), _foot(facts)
    sections = ["### Findings by section", "", *section_lines(report), ""]

    def compose(with_sections, kept, note=""):
        lines = list(head)
        if with_sections:
            lines += sections
        lines += ["### Section 1 findings", ""]
        if items:
            lines += [
                "Cite these by their F number; it is the number before the finding "
                "in Section 1 of `review.md`.",
                "",
                *items[:kept],
                "",
            ]
        else:
            lines += ["_No consensus findings in this run._", ""]
        lines += foot
        if note:
            lines += ["", note]
        return "\n".join(lines) + "\n"

    body = compose(True, len(items))
    if len(body) <= limit:
        return body

    total = len(items)
    leaving = "The per-section counts are left out and review.md has them."
    body = compose(False, total, f"_{leaving}_")
    if len(body) <= limit:
        return body

    def note(kept):
        return (
            f"_{total - kept} of {total} findings (F{kept + 1} to F{total}) and "
            f"the per-section counts are not listed here to keep this under "
            f"GitHub's size limit. review.md has all of it._"
        )

    low, high = 0, total
    while low < high:  # the most items that fit
        middle = (low + high + 1) // 2
        if len(compose(False, middle, note(middle))) <= limit:
            low = middle
        else:
            high = middle - 1
    return compose(False, low, note(low) if low < total else f"_{leaving}_")
