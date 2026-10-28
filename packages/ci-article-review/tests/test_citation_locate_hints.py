"""Section 9 tells the excerpt window where the evidence probably is (issue #440).

A long document is shown to the verifier as a 20,000-character window. The
draft's own citation entry often names the place ("p. 110 (Council Bluffs, IA:
1,410.3 million gallons withdrawn)") and the fact-check model's quote carries
the page's figures; both are now handed to the window as hints. These tests
pin the plumbing from the pipeline's claim list to ``select_excerpt_with_window``.
"""

import ci_article_review.pipeline as pipeline
from ci_article_review.adapters.citation import draft_citations as dc
from ci_article_review.adapters.citation import resolver

DRAFT = """\
Google's thirstiest site, Council Bluffs, withdrew 1.41 billion gallons in 2024. [9]

## Citations
[9] Google, 2025 Environmental Report, p. 110 (Council Bluffs, IA: 1,410.3 million gallons withdrawn): https://google.example/report.pdf
"""


class TestDescriptionsFor:
    def test_the_entry_text_comes_back_without_its_url(self):
        cited = dc.DraftCitations(DRAFT)
        (text,) = cited.descriptions_for("https://google.example/report.pdf")
        assert "1,410.3 million gallons withdrawn" in text
        assert "google.example" not in text

    def test_an_uncited_url_has_none(self):
        assert dc.DraftCitations(DRAFT).descriptions_for("https://other.example/") == []


class TestClaimEntriesCarryHints:
    def test_the_citation_entry_and_the_models_quote_are_attached(self):
        fact_check = {
            "confirmed": [
                {
                    "claim": "Council Bluffs withdrew 1.41 billion gallons in 2024 [9]",
                    "source_url": "https://google.example/report.pdf",
                    "supporting_quote": "Council Bluffs, IA Million gallons 1,410.3 400.1 1,010.2",
                }
            ]
        }
        (entry,) = pipeline._collect_citation_claims(fact_check, DRAFT)
        hints = entry["locate_hints"]["https://google.example/report.pdf"]
        assert any("p. 110" in h for h in hints)
        assert "Council Bluffs, IA Million gallons 1,410.3 400.1 1,010.2" in hints

    def test_a_quote_is_attached_only_to_the_url_the_model_named(self):
        fact_check = {
            "confirmed": [
                {
                    "claim": "Council Bluffs withdrew 1.41 billion gallons in 2024 [9]",
                    "source_url": "https://elsewhere.example/copy.pdf",
                    "supporting_quote": "1,410.3 400.1 1,010.2",
                }
            ]
        }
        (entry,) = pipeline._collect_citation_claims(fact_check, DRAFT)
        hints = entry["locate_hints"]
        assert "1,410.3 400.1 1,010.2" in hints["https://elsewhere.example/copy.pdf"]
        assert "1,410.3 400.1 1,010.2" not in hints["https://google.example/report.pdf"]

    def test_no_draft_and_no_quote_means_no_hints(self):
        (entry,) = pipeline._collect_citation_claims(
            {"confirmed": [{"claim": "c"}]}, ""
        )
        assert "locate_hints" not in entry


class TestTheResolverHandsThemToTheWindow:
    def test_resolve_citations_passes_each_urls_hints_to_the_window(self, monkeypatch):
        seen = []

        def fake_window(content, claim, head=4000, tail=1000, hints=()):
            seen.append(tuple(hints))
            return content[:100], {
                "chars_total": len(content),
                "chars_read": 100,
                "windowed": False,
                "how": "whole",
            }

        class _Resp:
            status_code = 200
            url = "https://google.example/report"
            content = (
                "<html><body><article>"
                + "Council Bluffs water. " * 40
                + "</article></body></html>"
            ).encode()
            headers = {"Content-Type": "text/html; charset=utf-8"}
            encoding = "utf-8"

            def raise_for_status(self):
                return None

        monkeypatch.setattr(resolver, "safe_get", lambda url, **kw: _Resp())
        monkeypatch.setattr(resolver.extract, "select_excerpt_with_window", fake_window)

        def no_model_call(*_a, **_k):
            raise RuntimeError("no model call in this test")

        # The window is chosen before the verifier is called; stop there.
        monkeypatch.setattr(resolver.llm, "call_provider", no_model_call)
        monkeypatch.setattr(
            resolver.wayback,
            "check",
            lambda *a, **k: {"archived": True, "snapshot_url": "x"},
        )
        monkeypatch.setattr(
            resolver, "_submit_missing_archives", lambda *a, **k: None, raising=False
        )
        claims = [
            {
                "claim": "Council Bluffs withdrew 1.41 billion gallons",
                "known_urls": ["https://google.example/report"],
                "locate_hints": {
                    "https://google.example/report": [
                        "p. 110 (Council Bluffs, IA: 1,410.3)"
                    ]
                },
            }
        ]
        resolver.resolve_citations(
            claims, [], api_keys={"mistral": {"api_key": "k"}}, verification_call_log=[]
        )
        assert ("p. 110 (Council Bluffs, IA: 1,410.3)",) in seen
