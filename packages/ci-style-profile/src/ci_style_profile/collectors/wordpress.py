"""WordPress REST API collector."""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Iterator

import requests

from ci_core.concurrency import run_all_bounded

from .base import Collector, CollectorError, ConfigError, Document

log = logging.getLogger(__name__)

SOURCE_NAME = "wordpress"

#: Pages fetched concurrently. Low on purpose — this is someone's live blog,
#: and the REST API is served by the same PHP workers as the public site.
_MAX_PAGE_PARALLEL = 5

#: Connect and inter-read-gap budgets for one page request.
_PAGE_HTTP_TIMEOUT = (10, 60)

#: Wall-clock safety net for one page fetch, well above _PAGE_HTTP_TIMEOUT
#: because that pair bounds socket *gaps*, not total time: a site that emits a
#: byte every 59s satisfies the read timeout forever. Only this catches that.
_PAGE_TIMEOUT_SECONDS = 300


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self):
        return " ".join(self._parts)


def _strip_html(html: str) -> str:
    stripper = _HTMLStripper()
    try:
        stripper.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return stripper.get_text()


def _strip_shortcodes(text: str) -> str:
    return re.sub(r"\[[^\]]+\]", "", text)


def _is_public_host(url: str) -> bool:
    try:
        from analysis.links import _is_public_host as _pipeline_check

        return _pipeline_check(url)
    except ImportError:
        pass
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    private = ("localhost", "127.", "192.168.", "10.", "172.")
    return bool(host) and not any(host == p or host.startswith(p) for p in private)


class WordPressCollector(Collector):
    SOURCE_NAME = "wordpress"

    REQUIRED_KEYS = ["site_url", "username", "application_password"]

    @classmethod
    def validate_config(cls, config: dict) -> None:
        missing = [k for k in cls.REQUIRED_KEYS if not config.get(k)]
        if missing:
            raise ConfigError(cls.SOURCE_NAME, missing_keys=missing)
        site_url = config.get("site_url", "")
        if not _is_public_host(site_url):
            raise ConfigError(
                cls.SOURCE_NAME, message=f"site_url {site_url!r} is not a public host"
            )

    def estimate_count(self) -> int | None:
        site_url = self.config["site_url"].rstrip("/")
        try:
            params: dict[str, str | int] = {"per_page": 1, "status": "publish"}
            resp = requests.get(
                f"{site_url}/wp-json/wp/v2/posts",
                params=params,
                auth=(self.config["username"], self.config["application_password"]),
                timeout=(10, 30),
                allow_redirects=False,
            )
            if resp.status_code == 200:
                return int(resp.headers.get("X-WP-Total", 0))
        except Exception:
            pass
        return None

    def fetch(self, since: str | None = None) -> Iterator[Document]:
        site_url = self.config["site_url"].rstrip("/")
        auth = (self.config["username"], self.config["application_password"])
        base_url = f"{site_url}/wp-json/wp/v2/posts"

        params: dict = {"per_page": 100, "status": "publish"}
        if since:
            params["after"] = since

        try:
            resp = requests.get(
                base_url,
                params=params,
                auth=auth,
                timeout=(10, 60),
                allow_redirects=False,
            )
            resp.raise_for_status()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            raise CollectorError(self.SOURCE_NAME, f"HTTP {status}: {e}")
        except Exception as e:
            raise CollectorError(self.SOURCE_NAME, f"Request failed: {e}")

        total = int(resp.headers.get("X-WP-Total", 0))
        total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
        log.info(
            "WordPress: %d posts across %d pages",
            total,
            total_pages,
            extra={"source": self.SOURCE_NAME},
        )

        page1_posts = resp.json()
        yield from self._posts_to_docs(page1_posts)
        fetched = len(page1_posts)
        log.info("WordPress: fetched page 1/%d (%d posts)", total_pages, fetched)

        if total_pages > 1:
            pages = list(range(2, total_pages + 1))

            def _fetch_page(page_num):
                p = dict(params)
                p["page"] = page_num
                r = requests.get(
                    base_url,
                    params=p,
                    auth=auth,
                    timeout=_PAGE_HTTP_TIMEOUT,
                    allow_redirects=False,
                )
                r.raise_for_status()
                return r.json()

            # Daemon threads, at most _MAX_PAGE_PARALLEL in flight — never a
            # ThreadPoolExecutor. Two independent reasons here, not one.
            #
            # The shared reason (see :mod:`ci_core.concurrency`): a pool worker
            # still inside a fetch at interpreter exit is joined by
            # concurrent.futures' atexit hook with a bare, untimed t.join(),
            # which is how finished ci-review processes were found alive two
            # days later.
            #
            # The reason specific to this function: it is a *generator*, and the
            # old code yielded from inside the `with ThreadPoolExecutor` block.
            # A caller that stopped consuming — a break, an exception, or just
            # dropping the iterator — left the generator suspended inside that
            # block with the pool still open and its workers unjoined, for as
            # long as the generator went un-finalized. Fetching a wave at a time
            # and yielding only after it has been collected means no thread of
            # ours is ever alive while this generator is suspended.
            for start in range(0, len(pages), _MAX_PAGE_PARALLEL):
                wave = pages[start : start + _MAX_PAGE_PARALLEL]
                outcomes = run_all_bounded(
                    [
                        (str(n), lambda n=n: _fetch_page(n), _PAGE_TIMEOUT_SECONDS)
                        for n in wave
                    ],
                    max_parallel=_MAX_PAGE_PARALLEL,
                )
                # Page order, not completion order: document order across a run
                # is now reproducible rather than however the network raced.
                for page_num in wave:
                    posts, error = outcomes[str(page_num)]
                    if error is not None:
                        log.warning("WordPress: page %d failed: %s", page_num, error)
                        continue
                    fetched += len(posts)
                    log.info(
                        "WordPress: fetched page %d/%d (%d posts so far)",
                        page_num,
                        total_pages,
                        fetched,
                        extra={
                            "page": page_num,
                            "total": total_pages,
                            "count": fetched,
                        },
                    )
                    yield from self._posts_to_docs(posts)

    def _posts_to_docs(self, posts: list) -> list[Document]:
        docs = []
        for post in posts:
            raw_html = post.get("content", {}).get("rendered", "")
            text = _strip_shortcodes(_strip_html(raw_html)).strip()
            if not text:
                continue
            date = post.get("date", "")[:10]
            url = post.get("link", str(post.get("id", "")))
            metadata = {
                "post_id": post.get("id"),
                "categories": [str(c) for c in post.get("categories", [])],
                "tags": [str(t) for t in post.get("tags", [])],
            }
            docs.append(
                Document.from_text(
                    text=text,
                    source=self.SOURCE_NAME,
                    register="long_form",
                    date=date,
                    url_or_id=url,
                    metadata=metadata,
                )
            )
        return docs
