"""Resolve a fact-check claim to the citation *the draft itself* attaches to it.

The fact-check models return claims, not locations. Something has to decide which
URL each claim gets checked against, and the answer that matters to an author is
"the source you cited for this sentence" — Section 9 exists to audit the article's
own citations, not to find some page on the internet that happens to agree.

Before this module, a claim whose fact-check item named no URL fell back to a
response-level grounded URL: the *first* entry of the provider's search-results
list for the whole response. That URL has nothing to do with any particular
claim. In one real run it stamped a single LBNL energy report onto 44 unrelated
claims — Yorkville water rates, ICNIRP exposure limits, IARC classifications —
and the relevance checker dutifully reported that an energy report does not
discuss any of them. Correct observation, wrong source, 47 false positives
burying two real findings. A section that flags everything flags nothing.

The draft already carries the answer. It ends with a numbered citation block,
and its body carries the markers that point into it.

Two things about the markup are load-bearing:

* **Markers trail the text they support** — ``...online by 2030. [24a] Microsoft
  signed...``. Attaching a marker to the sentence that *follows* it shifts every
  citation in a paragraph one sentence late, which reads as a plausible mapping
  and is wrong throughout. Segmentation here cuts at each marker run and keeps
  the text *before* it.
* **A marker covers back to the previous marker, but authors do not always mean
  it to.** A span cited ``[2]`` may open with two sentences that belong to the
  ``[1]`` before it. So the enclosing marker is the first candidate, not the only
  one: the preceding span's markers follow it, and the caller escalates rather
  than reporting a mismatch against a source the author may not have meant.

Anything this module cannot anchor gets no URL at all. That is the point — an
honest "the draft attaches no citation to this claim" is useful, and a fabricated
association is worse than silence.
"""

import re

#: One citation marker: a number with an optional letter suffix. Sub-numbering
#: (``[11a]``, ``[24d]``) is how a draft cites several sources for one point, and
#: it is common enough in practice that dropping the suffix would collapse
#: distinct sources onto one number.
_ONE = r"\d+[a-z]?"

#: A run of adjacent markers — ``[7][8][20]`` and ``[7, 8]`` are both one run
#: citing the same span.
_MARKER_RUN = re.compile(rf"(?:\[{_ONE}(?:\s*,\s*{_ONE})*\]\s*)+")
_MARKER_KEY = re.compile(_ONE)

#: Heading that opens the citation block. Publications differ on the word, and
#: the block is the only place the URLs live, so all the common spellings are
#: accepted.
_BLOCK_HEADING = re.compile(
    r"(?mi)^#{1,6}\s*(?:works\s+cited|citations|sources|references|notes|"
    r"bibliography)\s*:?\s*$"
)

#: A citation entry: ``[7] Description... https://url``, running until the next
#: entry starts at the beginning of a line.
_ENTRY = re.compile(rf"(?ms)^\[({_ONE})\]\s+(.*?)(?=^\[{_ONE}\]\s|\Z)")

_URL = re.compile(r"https?://[^\s<>\"'\)\]]+")

#: Start of a markdown list item — ``- ``, ``* ``, ``+ ``, or ``1. ``.
_LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+")

#: Words carrying no topical signal. Claim-to-span matching is a bag-of-words
#: overlap; without this the function words dominate and every span scores alike.
_STOP = frozenset(
    """a an the of to in and or is are was were be been being for on at by with as
    that this these those it its from has have had not but if then than so such can
    could may might will would shall should about into over under between during per
    each other there their they them we you our your i he she his her him one two
    also more most some any all no nor only own same too very just which who whom
    what when where while after before both few own s t don now""".split()
)

_WORD = re.compile(r"[a-z][a-z-]{2,}")
#: Numbers are the highest-signal token in a factual claim — two spans about
#: water use are told apart by ``42,000`` versus ``350,000``, not by vocabulary.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?%?")

#: Minimum combined similarity for a claim to count as located in the draft.
#: Set from a real 133-claim run: below this the top span is no better than the
#: runner-up and the "match" is noise. A claim that does not clear it gets no
#: URL rather than a guessed one.
_MIN_SCORE = 0.55

#: How many candidate URLs one claim may be checked against. The enclosing span
#: usually answers it; the allowance exists so a claim that opens a span cited to
#: the *next* marker still reaches the source the author meant. Each candidate
#: past the first costs a fetch and a verification call, and only claims the
#: first candidate failed to support ever reach them.
#:
#: Raised from 3 once ``_urls_for_keys`` became breadth-first. At 3, a claim
#: citing three markers was exactly at the limit, so a single marker carrying
#: two URLs (a bulletin published in two versions) pushed a cited source off the
#: end. The ordering change alone stops whole markers being dropped; this leaves
#: room to also reach a second version when the first does not carry the text.
MAX_CANDIDATES = 5

#: Weighting between vocabulary overlap and figure agreement, for claims that
#: carry figures. Numbers are more discriminating but sparser, so neither alone
#: is enough.
_WORD_WEIGHT = 0.55
_NUMBER_WEIGHT = 0.45

#: How much a citation entry's own description counts when ordering the markers
#: a claim could plausibly belong to.
#:
#: Span position alone is not enough, because a draft's summary bullets restate
#: several unrelated findings in one span and then cite the span once. A claim
#: about xAI's turbines matched such a bullet almost verbatim and inherited its
#: markers, which pointed at Virginia noise studies — while the body passage that
#: actually discusses xAI, cited to the SELC and Earthjustice releases, scored
#: lower for being written in the author's own words rather than the summary's.
#:
#: The citation block breaks the tie: entry [6a] names xAI and Memphis in its
#: description, entry [4] does not. Kept below 1.0 so a strong span match still
#: leads — the description is a title, not the claim's location.
_DESCRIPTION_WEIGHT = 0.6


def _tokens(text):
    """Content words, crudely singularised.

    A claim says "data centers" where the citation entry's title says "Data
    Center", and "turbines" where the title says "turbine". Both sides get the
    same treatment, so the only thing that matters is that it is consistent —
    this is a similarity key, never anything the reader sees.
    """
    words = set()
    for word in _WORD.findall(text.lower()):
        if word in _STOP:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.add(word)
    return words


def _numbers(text):
    return {n.rstrip(".,").replace(",", "") for n in _NUMBER.findall(text)}


def _marker_keys(text):
    """Marker keys in ``text``, in document order, lowercased and de-duplicated."""
    seen = []
    for run in _MARKER_RUN.finditer(text):
        for key in _MARKER_KEY.findall(run.group(0)):
            key = key.lower()
            if key not in seen:
                seen.append(key)
    return seen


def split_citation_block(draft):
    """Split ``draft`` into (body, citation block).

    The block is found from its heading. Falling back to "the last run of
    ``[n] ...`` lines" was considered and rejected: a draft that quotes a
    bracketed list mid-body would have its body truncated there, silently
    dropping every citation after it.
    """
    heading = None
    for match in _BLOCK_HEADING.finditer(draft):
        heading = match  # last heading wins; a body section may share the word
    if heading is None:
        return draft, ""
    return draft[: heading.start()], draft[heading.end() :]


def parse_citation_block(block):
    """Parse a citation block into ``{marker key: {"urls": [...], "text": str}}``.

    Entries with no URL are kept. They cannot be fetched, but their presence is
    what distinguishes "the draft cites nothing here" from "the draft cites
    something unfetchable here", and only the first is the author's problem to
    fix by adding a citation.
    """
    entries = {}
    for key, text in _ENTRY.findall(block):
        urls = []
        for url in _URL.findall(text):
            url = url.rstrip(".,;:")
            if url not in urls:
                urls.append(url)
        entries[key.lower()] = {"urls": urls, "text": text.strip()}
    return entries


def iter_blocks(body):
    """Yield the body's citation-bearing blocks: paragraphs, and list items.

    A list item is its own block even though the list is one paragraph of text.
    Markers cite backwards, and without this a bullet that carries no citation
    inherits the next bullet's. That is not hypothetical: a summary list whose
    middle bullet (xAI's turbines, QTS's water, Meta's EPA review) was
    deliberately uncited took the markers off the bullet below it and pointed
    three Virginia noise sources at a Tennessee air-permit claim.

    An uncited bullet should anchor nothing. Splitting on the item boundary is
    what makes that the outcome.
    """
    for para in re.split(r"\n\s*\n", body):
        if not para.strip():
            continue
        block = []
        for line in para.split("\n"):
            if _LIST_ITEM.match(line) and block:
                yield "\n".join(block)
                block = []
            block.append(line)
        if block:
            yield "\n".join(block)


def segment_body(body, known_keys):
    """Cut ``body`` into spans, each tagged with the markers that cite it.

    A span runs from the end of the previous marker run to the start of the next,
    so the text a marker cites is the text *before* it. Trailing text after a
    block's last marker is dropped: nothing cites it.

    Markers not present in ``known_keys`` are ignored, so a bracketed number in
    prose that the citation block never defines cannot invent a citation.
    """
    segments = []
    for block in iter_blocks(body):
        pos = 0
        for run in _MARKER_RUN.finditer(block):
            span = block[pos : run.start()].strip()
            pos = run.end()
            keys = [k for k in _marker_keys(run.group(0)) if k in known_keys]
            if span and keys:
                segments.append({"text": span, "markers": keys})
    return segments


def _score(claim_words, claim_numbers, span):
    if not claim_words:
        return 0.0
    overlap = len(claim_words & span["words"]) / len(claim_words)
    if not claim_numbers:
        return overlap
    agreement = len(claim_numbers & span["numbers"]) / len(claim_numbers)
    return _WORD_WEIGHT * overlap + _NUMBER_WEIGHT * agreement


class DraftCitations:
    """The draft's citation block, indexed for claim lookup.

    ``candidates_for`` is the whole interface: it answers "which URLs does this
    draft cite for this claim", ordered best-first, and returns an empty list
    when the draft cites nothing for it.
    """

    def __init__(self, draft):
        body, block = split_citation_block(draft or "")
        self.entries = parse_citation_block(block)
        for entry in self.entries.values():
            # The description minus its URLs — a domain name shares tokens with
            # every other URL on the same host and tells us nothing about topic.
            entry["words"] = _tokens(_URL.sub(" ", entry["text"]))
        self._segments = []
        for segment in segment_body(body, self.entries):
            self._segments.append(
                {
                    "markers": segment["markers"],
                    "words": _tokens(segment["text"]),
                    "numbers": _numbers(segment["text"]),
                }
            )

    def __bool__(self):
        """True when there is a citation block worth consulting."""
        return bool(self.entries)

    @property
    def marker_count(self):
        return len(self.entries)

    def _urls_for_keys(self, keys):
        """Candidate URLs for ``keys``, breadth-first across the markers.

        Order is one URL from each marker, then each marker's second, and so on
        — not marker 1's URLs exhausted before marker 2 is reached. The caller
        truncates this list to ``MAX_CANDIDATES``, and with a depth-first order
        that truncation silently drops whole markers: a claim cited ``[9][3][4]``
        where ``[9]`` carries two URLs spent two of its three slots inside
        ``[9]`` and never checked ``[4]`` at all. That is a real case from the
        2026-09-18 GPS run — the claim was then reported unsupported having
        never been compared against one of the sources the draft cited for it.

        Breadth-first makes the cap bound *depth per marker* rather than
        deciding which markers get looked at, so every cited source is
        represented before any source is examined twice.
        """
        per_key = [self.entries.get(key, {}).get("urls", []) for key in keys]
        urls = []
        for depth in range(max((len(u) for u in per_key), default=0)):
            for candidate in per_key:
                if depth < len(candidate) and candidate[depth] not in urls:
                    urls.append(candidate[depth])
        return urls

    def candidates_for(self, claim, source_text=""):
        """URLs the draft cites for ``claim``, best first (empty when none).

        ``source_text`` is the fact-check item's own source field. It is read
        only for explicit markers: a model that writes "cited in [29] but not
        public" is naming the citation directly, which beats any similarity
        score this module could compute.

        Returns at most ``MAX_CANDIDATES`` URLs.
        """
        if not self.entries:
            return []

        # 1. The claim (or the model's note about it) names a marker outright.
        named = [k for k in _marker_keys(f"{claim} {source_text}") if k in self.entries]
        if named:
            urls = self._urls_for_keys(named)
            if urls:
                return urls[:MAX_CANDIDATES]

        # 2. Locate the claim in the body and read the markers off its spans.
        claim_words, claim_numbers = _tokens(claim), _numbers(claim)
        scored = [
            (_score(claim_words, claim_numbers, span), index, span)
            for index, span in enumerate(self._segments)
        ]
        matched = [row for row in scored if row[0] >= _MIN_SCORE]
        if not matched:
            return []

        # Every marker on a span the claim plausibly belongs to is a candidate,
        # scored by its best such span. A claim restated in a summary bullet and
        # again in the body legitimately has two homes; which one the author
        # meant is settled below, not by whichever wording the model echoed.
        by_key = {}
        for score, index, span in matched:
            keys = list(span["markers"])
            # The preceding span's markers too — see the module docstring on why
            # the enclosing marker is not always the one meant for the first
            # sentence of a span. Discounted so it never outranks a direct hit.
            if index > 0:
                keys += [
                    k for k in self._segments[index - 1]["markers"] if k not in keys
                ]
            for position, key in enumerate(keys):
                value = score if position < len(span["markers"]) else score * 0.5
                by_key[key] = max(by_key.get(key, 0.0), value)

        def rank(key):
            description = self.entries.get(key, {}).get("words", set())
            overlap = (
                len(claim_words & description) / len(claim_words)
                if claim_words and description
                else 0.0
            )
            return by_key[key] + _DESCRIPTION_WEIGHT * overlap

        ordered = sorted(by_key, key=lambda k: (-rank(k), k))
        return self._urls_for_keys(ordered)[:MAX_CANDIDATES]
