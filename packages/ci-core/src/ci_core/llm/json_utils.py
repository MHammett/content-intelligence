"""Best-effort JSON extraction shared across review adapters.

Reasoning/grounded models (sonar-reasoning-pro, gemini-2.5-pro, etc.) sometimes
wrap their JSON in markdown fences, prepend a chain-of-thought / ``<think>``
preamble, or surround it with prose. A bare ``json.loads`` fails on all of those.

This helper tries, in order:
  1. Direct parse of the raw content.
  2. Direct parse after stripping any ``<think>...</think>`` block(s) — sonar-
     reasoning-pro can emit more than one, and reasoning prose frequently
     contains stray ``{``/``}`` characters (e.g. discussing the target schema)
     that would otherwise poison step 4's brace-span search.
  3. A fenced code block (```` ```json ... ``` ```` or ```` ``` ... ``` ````),
     found anywhere in the (think-stripped) text — not only when the fence is
     the very first thing in the string, so leading prose before the fence
     doesn't defeat detection.
  4. The outermost ``{`` ... ``}`` span in the think-stripped text.

Returns the parsed object, or None if nothing parses.
"""

import json
import re

from .. import text_repair

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCED_BLOCK = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)
_JSON_SPAN = re.compile(r"\{.*\}", re.DOTALL)


def _try_parse(text):
    r"""Parse ``text``, repairing narrowed punctuation in whatever comes back.

    Every strategy below — and the salvage path — funnels through here, which
    makes this the one place provider JSON becomes Python data.

    The repair has to happen *after* the parse, not on the response text.
    Perplexity sends its damage as the escape ``\u0019`` rather than a literal
    control byte (a literal one would make the body unparseable, and these
    responses parse), so there is no control character to find until json has
    decoded the escape. See :mod:`ci_core.text_repair`.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return text_repair.repair_tree(parsed)


def extract_json(content):
    if not content:
        return None

    # 1. Direct parse.
    parsed = _try_parse(content.strip())
    if parsed is not None:
        return parsed

    # 2. Strip <think>...</think> block(s) — may be more than one, and may
    # contain braces that would otherwise corrupt the brace-span fallback.
    stripped = _THINK_BLOCK.sub("", content).strip()
    parsed = _try_parse(stripped)
    if parsed is not None:
        return parsed

    # 3. A fenced block, found anywhere (handles prose before AND after it).
    fence_match = _FENCED_BLOCK.search(stripped)
    if fence_match:
        parsed = _try_parse(fence_match.group(1).strip())
        if parsed is not None:
            return parsed

    # 4. Outermost {...} span in the think-stripped text.
    span_match = _JSON_SPAN.search(stripped)
    if span_match:
        parsed = _try_parse(span_match.group(0))
        if parsed is not None:
            return parsed

    return None


# ---------------------------------------------------------------------------
# Truncation salvage
# ---------------------------------------------------------------------------
#
# A response that hit an output-token ceiling mid-generation is well-formed
# JSON up to the cut point — it just stops, often mid-string, inside what
# would have been another complete array element. `extract_json` correctly
# gives up on this (it's not malformed, it's incomplete), so this is a
# separate, explicit best-effort recovery path: find the last point in the
# text where every currently-open string/array/object is either closed or
# can be closed by simply appending the matching brackets, and try parsing
# that. Only complete elements survive; whatever was mid-flight when the
# stream was cut is discarded.

_FENCE_OPEN = re.compile(r"```(?:json)?\s*\n?")
_CLOSERS = {"{": "}", "[": "]"}


def _safe_truncation_points(text):
    """Indices in `text` after which the open bracket/brace stack can be
    closed to yield structurally-complete (though possibly incomplete) JSON.

    Each point is `(index, stack)` where `stack` is the list of still-open
    container characters at that index, outermost first. Tracks string and
    backslash-escape state so a comma or bracket inside a string value is
    never mistaken for a structural one.
    """
    stack = []
    in_string = False
    escape = False
    points = []

    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                break  # more closers than openers — text isn't salvageable past here
            stack.pop()
            # A container just finished closing: the value ending here is complete.
            points.append((i + 1, list(stack)))
        elif ch == "," and stack and stack[-1] == "[":
            # A comma directly inside an array (not inside an object's
            # key/value pairs) marks a complete array element. Object-internal
            # commas are deliberately NOT treated as safe points: cutting
            # between an object's fields would "recover" a struct that's
            # missing keys the model meant to write — a fabricated-looking
            # partial record rather than a genuinely complete one.
            points.append((i, list(stack)))

    return points


def _salvage(text):
    """Recover the deepest complete JSON prefix of `text`, or None."""
    points = _safe_truncation_points(text)
    # Walk from the latest (most content recovered) to the earliest safe point.
    for idx, stack in reversed(points):
        candidate = text[:idx] + "".join(_CLOSERS[c] for c in reversed(stack))
        parsed = _try_parse(candidate)
        if parsed is not None:
            return parsed
    return None


def extract_json_with_salvage(content):
    """Like `extract_json`, but falls back to salvaging a truncated response.

    Returns `(data, truncated)`. `truncated` is True only when `data` came
    from the salvage path (i.e. the response was genuinely incomplete and
    some trailing content — a partial array element — had to be discarded).
    A `data` of None means nothing at all could be recovered.
    """
    parsed = extract_json(content)
    if parsed is not None:
        return parsed, False

    if not content:
        return None, False

    text = _THINK_BLOCK.sub("", content).strip()

    # An opening fence with no matching close means the stream was cut off
    # before the closing ```` ``` ````; drop the opener and salvage what's inside.
    fence_open = _FENCE_OPEN.match(text)
    if fence_open and not _FENCED_BLOCK.search(text):
        text = text[fence_open.end() :]

    salvaged = _salvage(text)
    if salvaged is not None:
        return salvaged, True
    return None, False
