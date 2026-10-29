"""A/B for issue #453: batched relevance checks against the single-claim check,
on real claims and the real pages they cite.

Three arms over the same claims:

* ``a`` and ``b``: the single-claim check, twice. Their agreement is the noise
  floor, the most any other arm can be expected to agree with ``a``.
* ``batch``: what ships. ``resolver._verify_relevance`` with a run's
  ``_RelevanceBatch`` installed, exactly as ``resolve_citations`` installs it, so
  a batched "supports" is kept and every other claim is checked again alone.

Claims are taken from saved reports: every verdict whose page was sent whole
(``read_coverage.window == "whole"``) on a URL that two or more claims cite.
Writes every verdict and call to a JSON file so the comparison can be re-read.

Arguments: history root, ``.env`` path, output JSON, then optionally
``slug/run_prefix`` pairs (default: the two runs the 2026-10-28 measurement
used). Makes mistral-small calls: about $0.10 a run on the default claims.
"""

import collections
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

from ci_core import extract
from ci_article_review.adapters.citation import resolver

history, env_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
runs = [tuple(a.split("/", 1)) for a in sys.argv[4:]] or [
    ("data-center-district-heating", "run_4_"),
    ("brockovich-fact-check", "run_2_"),
]
load_dotenv(env_path)
api_keys = {"mistral": {"api_key": os.environ["MISTRAL_API_KEY"]}}

reports = []
for slug, run in runs:
    reports += glob.glob(os.path.join(history, slug, run + "*_report.json"))

groups = collections.defaultdict(list)
for path in reports:
    r = json.load(open(path, encoding="utf-8"))
    for c in r.get("section_9_citations") or []:
        rc = c.get("read_coverage") or {}
        if c.get("relevance_verdict") and rc.get("window") == "whole" and c.get("url"):
            text = c.get("verified_as") or c["claim"]
            if text not in groups[c["url"]]:
                groups[c["url"]].append(text)
groups = {u: cs for u, cs in groups.items() if len(cs) >= 2}
print(
    f"{len(reports)} report(s); {len(groups)} pages with 2+ claims, "
    f"{sum(map(len, groups.values()))} claims",
    flush=True,
)


def page_text(url):
    try:
        resp = resolver._get_page(url, timeout=20)
        text, _kind, _cov = resolver._extract_fetched_with_coverage(resp, url)
        return text
    except Exception:  # noqa: BLE001
        return None


with ThreadPoolExecutor(6) as ex:
    texts = dict(zip(groups, ex.map(page_text, groups)))
usable = {
    u: cs[: resolver._MAX_BATCH_CLAIMS]
    for u, cs in groups.items()
    if texts.get(u) and len(texts[u]) <= resolver._EXCERPT_HEAD + resolver._EXCERPT_TAIL
}
print(f"{len(usable)} pages fetched and still sent whole", flush=True)


def single(url, claim):
    content = texts[url]
    excerpt, window = extract.select_excerpt_with_window(
        content, claim, head=resolver._EXCERPT_HEAD, tail=resolver._EXCERPT_TAIL
    )
    return resolver._verify_one(
        claim, content, excerpt, window, api_keys["mistral"]["api_key"], None
    )


def arm_single(url):
    return [single(url, c) for c in usable[url]]


def arm_batch(url):
    return [
        resolver._verify_relevance(c, texts[url], api_keys, None, url=url)
        for c in usable[url]
    ]


def verdict(info):
    return info.get("verdict") if info.get("checked") else "unchecked"


with ThreadPoolExecutor(3) as ex:
    a = dict(zip(usable, ex.map(arm_single, usable)))
    b = dict(zip(usable, ex.map(arm_single, usable)))
batch = resolver._RelevanceBatch(dict(usable))
resolver._relevance_batch = batch
try:
    with ThreadPoolExecutor(3) as ex:
        h = dict(zip(usable, ex.map(arm_batch, usable)))
finally:
    resolver._relevance_batch = None

rows, calls = [], {"a": [], "b": [], "batch": list(batch.orphan_calls)}
for url, claims in usable.items():
    for i, claim in enumerate(claims):
        rows.append(
            {
                "url": url,
                "claim": claim,
                "a": verdict(a[url][i][0]),
                "b": verdict(b[url][i][0]),
                "batch": verdict(h[url][i][0]),
                "batch_size": h[url][i][0].get("batch_size"),
                "a_quote": a[url][i][0].get("quote"),
                "batch_quote": h[url][i][0].get("quote"),
                "batch_reason": h[url][i][0].get("reason"),
            }
        )
        for arm, got in (("a", a), ("b", b), ("batch", h)):
            if got[url][i][1] is not None:
                calls[arm].append(got[url][i][1])

json.dump(
    {"rows": rows, "calls": calls, "stats": batch.stats},
    open(out_path, "w", encoding="utf-8"),
    indent=1,
    default=str,
)
n = len(rows)


def agree(x, y):
    k = sum(r[x] == r[y] for r in rows)
    return f"{k}/{n} = {k / n:.0%}"


def tokens(arm, kind):
    return sum(((e or {}).get("tokens") or {}).get(kind) or 0 for e in calls[arm])


print(f"claims compared: {n}")
print(f"single A vs single B (noise floor): {agree('a', 'b')}")
print(f"single A vs batch:                  {agree('a', 'batch')}")
print(f"single B vs batch:                  {agree('b', 'batch')}")
print(
    "supports  A/B/batch:",
    *(sum(r[x] == "supports" for r in rows) for x in ("a", "b", "batch")),
)
print("batch stats:", batch.stats)
for arm in ("a", "b", "batch"):
    print(
        f"{arm:>5}: {len(calls[arm])} calls, {tokens(arm, 'prompt')} prompt + "
        f"{tokens(arm, 'completion')} completion tokens"
    )
print(
    "A -> batch:",
    collections.Counter((r["a"], r["batch"]) for r in rows if r["a"] != r["batch"]),
)
