"""The link check's Wayback lookups run alongside the model reviews (issue #497).

On a wide run of a 99-link draft (2026-11-15) link validation took five
minutes, most of it archive.org lookups the models never read, and the
ensemble waited for all of it. The HTTP checks still run first, because the
models' review context names the broken links; the lookups now run on a
thread and the run waits for them only at Pass 3, their first reader.
"""

import copy
import threading
from unittest.mock import patch

from ci_article_review.analysis import links

from .test_pipeline_end_to_end import _CONFIG, _stubbed_run

OK = {"url": "https://example.org/a", "ok": True, "status_code": 200}
BLOCKED = {"url": "http://10.0.0.1/x", "ok": False, "error": "refused by SSRF guard"}


class TestLookupArchives:
    def test_each_result_gets_the_lookup_validate_links_would_have_made(self):
        asked = []

        def check(url, timeout, stale_days):
            asked.append((url, stale_days))
            return {"url": url, "archived": False}

        results = [dict(OK), dict(BLOCKED)]
        with patch.object(links, "wayback_check", side_effect=check):
            links.lookup_archives(results, wayback_stale_days=90)
        assert asked == [("https://example.org/a", 90)]
        assert results[0]["wayback"] == {
            "url": "https://example.org/a",
            "archived": False,
        }
        assert "wayback" not in results[1]  # never sent to archive.org

    def test_it_matches_the_inline_path(self):
        """Same lookup, same field, as ``validate_links(check_wayback=True)``."""
        answer = {"url": "https://example.org/a", "archived": True, "snapshot_url": "s"}
        with (
            patch.object(links, "wayback_check", return_value=dict(answer)),
            patch.object(
                links,
                "_check_http",
                return_value={"status_code": 200, "ok": True, "verified_via": "direct"},
            ),
        ):
            inline = links.validate_links(
                "see https://example.org/a", check_wayback=True
            )
            later = links.validate_links(
                "see https://example.org/a", check_wayback=False
            )
            links.lookup_archives(later)
        assert later == inline

    def test_a_lookup_that_never_finishes_is_recorded_as_unknown(self):
        results = [dict(OK)]

        def outcomes(jobs, max_parallel):
            return {
                name: (None, TimeoutError("past its budget")) for name, _, _ in jobs
            }

        with patch.object(links, "run_all_bounded", side_effect=outcomes):
            links.lookup_archives(results)
        assert results[0]["wayback"]["archived"] is None
        assert "did not finish" in results[0]["wayback"]["error"]


class TestTheModelsDoNotWait:
    def test_the_ensemble_starts_before_the_lookups_finish(self, tmp_path):
        models_started = threading.Event()
        order = []

        def slow_lookups(results, **kw):
            # Holds until a model review has started, as a slow archive.org
            # would; with the old order this would wait out its timeout.
            order.append(("lookups began", models_started.wait(timeout=20)))
            for r in results:
                r["wayback"] = {"url": r["url"], "archived": False}

        from .test_pipeline_end_to_end import _fake_run_domain

        def run_domain(*a, **kw):
            models_started.set()
            return _fake_run_domain(*a, **kw)

        seen_at_pass3 = []

        def resolve(claims, *a, **kw):
            seen_at_pass3.append(True)
            return []

        config = copy.deepcopy(_CONFIG)
        config["pipeline"]["link_validation"] = True
        found = [dict(OK)]
        patches = [
            patch("ci_article_review.pipeline.merge_configs", return_value=config),
            patch(
                "ci_article_review.analysis.links.validate_links",
                side_effect=lambda text, **kw: [dict(r) for r in found],
            ),
            patch(
                "ci_article_review.analysis.links.lookup_archives",
                side_effect=slow_lookups,
            ),
            patch("ci_article_review.pipeline._run_domain", side_effect=run_domain),
            patch(
                "ci_article_review.adapters.citation.resolver.resolve_citations",
                side_effect=resolve,
            ),
        ]
        with _stubbed_run(tmp_path, extra_patches=patches) as report:
            pass
        assert order == [("lookups began", True)]
        # Joined before Pass 3: the report carries every lookup.
        assert report["pre_analysis"]["links"][0]["wayback"]["archived"] is False

    def test_the_http_check_is_still_asked_not_to_look_up(self, tmp_path):
        calls = []
        config = copy.deepcopy(_CONFIG)
        config["pipeline"]["link_validation"] = True

        def checker(text, **kw):
            calls.append(kw)
            return []

        patches = [
            patch("ci_article_review.pipeline.merge_configs", return_value=config),
            patch(
                "ci_article_review.analysis.links.validate_links", side_effect=checker
            ),
        ]
        with _stubbed_run(tmp_path, extra_patches=patches):
            pass
        assert calls and calls[0]["check_wayback"] is False
