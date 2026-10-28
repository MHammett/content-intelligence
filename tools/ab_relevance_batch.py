"""A/B for issue #453: batched vs single-claim relevance verdicts, on real
claims against the real pages they cite. Single A and single B measure the
noise floor (the same single-claim check twice); batch is compared to A.
Writes every verdict to a JSON file so the comparison can be re-read."""

import collections
import glob
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

from ci_core import extract
from ci_article_review.adapters.citation import resolver

history, env_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
load_dotenv(env_path)
api_key = os.environ["MISTRAL_API_KEY"]

reports = []
for slug, run in (
    ("data-center-district-heating", "run_4_"),
    ("brockovich-fact-check", "run_2_"),
):
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
    f"{len(groups)} pages with 2+ claims, {sum(map(len, groups.values()))} claims",
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
    u: cs
    for u, cs in groups.items()
    if texts.get(u) and len(texts[u]) <= resolver._EXCERPT_HEAD + resolver._EXCERPT_TAIL
}
print(f"{len(usable)} pages fetched and still sent whole", flush=True)

rows = []
lock = threading.Lock()


def single(url, claim):
    content = texts[url]
    excerpt, window = extract.select_excerpt_with_window(
        content, claim, head=resolver._EXCERPT_HEAD, tail=resolver._EXCERPT_TAIL
    )
    info, entry = resolver._verify_one(claim, content, excerpt, window, api_key, None)
    return info, entry


def run_group(url):
    claims = usable[url][: resolver._MAX_BATCH_CLAIMS]
    a = [single(url, c) for c in claims]
    b = [single(url, c) for c in claims]
    batch = resolver._RelevanceBatch({url: claims})
    content = texts[url]
    excerpt, _ = extract.select_excerpt_with_window(
        content, claims[0], head=resolver._EXCERPT_HEAD, tail=resolver._EXCERPT_TAIL
    )
    got = []
    for c in claims:
        res = batch.verify(url, c, content, excerpt, api_key, None)
        got.append(res)
    with lock:
        for i, c in enumerate(claims):
            rows.append(
                {
                    "url": url,
                    "claim": c,
                    "a": a[i][0].get("verdict")
                    if a[i][0].get("checked")
                    else "unchecked",
                    "b": b[i][0].get("verdict")
                    if b[i][0].get("checked")
                    else "unchecked",
                    "batch": (
                        got[i][0].get("verdict")
                        if got[i] and got[i][0].get("checked")
                        else ("unchecked" if got[i] else "fell_back")
                    ),
                    "a_tokens": (a[i][1] or {}).get("input_tokens")
                    or (a[i][1] or {}).get("prompt_tokens"),
                    "batch_entry": got[i][1] if got[i] else None,
                    "a_entry": a[i][1],
                }
            )


with ThreadPoolExecutor(3) as ex:
    list(ex.map(run_group, usable))

json.dump(rows, open(out_path, "w", encoding="utf-8"), indent=1, default=str)
n = len(rows)
ab = sum(r["a"] == r["b"] for r in rows)
abatch = sum(r["a"] == r["batch"] for r in rows)
bbatch = sum(r["b"] == r["batch"] for r in rows)
print(f"claims compared: {n}")
print(f"single A vs single B (noise floor): {ab}/{n} = {ab / n:.0%}")
print(f"single A vs batch:                  {abatch}/{n} = {abatch / n:.0%}")
print(f"single B vs batch:                  {bbatch}/{n} = {bbatch / n:.0%}")
sup = lambda v: v == "supports"  # noqa: E731
print(
    "supports  A/B/batch:",
    sum(sup(r["a"]) for r in rows),
    sum(sup(r["b"]) for r in rows),
    sum(sup(r["batch"]) for r in rows),
)
print("batch outcomes:", collections.Counter(r["batch"] for r in rows))
print("A outcomes:    ", collections.Counter(r["a"] for r in rows))
