"""A Google Drive viewer link is fetched through its download URL (issue #447).

The viewer page is a JavaScript shell with no document text, so a claim cited
to one could never be read. ``uc?export=download&id=`` returns the file.
"""

import pytest

from ci_article_review.adapters.citation import resolver

FID = "165w-r6rg7bwOUtSLBx53Jf70q2LM5Q7y"
DOWNLOAD = f"https://drive.google.com/uc?export=download&id={FID}"


@pytest.mark.parametrize(
    "url",
    [
        f"https://drive.google.com/file/d/{FID}/view",
        f"https://drive.google.com/file/d/{FID}/view?usp=sharing",
        f"https://drive.google.com/file/d/{FID}/preview",
        f"https://drive.google.com/file/d/{FID}",
        f"https://drive.google.com/open?id={FID}",
        f"https://drive.google.com/open?usp=sharing&id={FID}",
    ],
)
def test_viewer_links_become_download_links(url):
    assert resolver.direct_document_url(url) == DOWNLOAD


@pytest.mark.parametrize(
    "url",
    [
        "https://www.epa.gov/sites/default/files/report.pdf",
        "https://docs.google.com/document/d/abcdefghijk/edit",
        "https://drive.google.com/drive/folders/abcdefghijk",
        DOWNLOAD,
        "",
        None,
    ],
)
def test_everything_else_is_unchanged(url):
    assert resolver.direct_document_url(url) == url


def test_the_resolver_fetches_the_download_and_reports_the_cited_link(monkeypatch):
    fetched = []

    class _Resp:
        status_code = 200
        url = "https://drive.usercontent.google.com/download?id=" + FID
        content = (
            "<html><body><article>"
            + "The court reissued the injunction in September. " * 20
            + "</article></body></html>"
        ).encode()
        headers = {"Content-Type": "text/html; charset=utf-8"}
        encoding = "utf-8"

        def raise_for_status(self):
            return None

    def fake_get(url, **_kw):
        fetched.append(url)
        return _Resp()

    def no_model_call(*_a, **_k):
        raise RuntimeError("no model call in this test")

    monkeypatch.setattr(resolver, "safe_get", fake_get)
    monkeypatch.setattr(resolver.llm, "call_provider", no_model_call)
    monkeypatch.setattr(
        resolver.wayback,
        "check",
        lambda *a, **k: {"archived": True, "snapshot_url": "x"},
    )
    cited = f"https://drive.google.com/file/d/{FID}/view"
    result = resolver._resolve_known_url(
        "the court reissued the injunction",
        cited,
        api_keys={"mistral": {"api_key": "k"}},
    )
    assert fetched[0] == DOWNLOAD
    assert result["url"] == cited
    assert not result.get("final_url")


class TestScannedPdfIsNotJudged:
    """A scan with a stamp on each page is unreadable, not unsupporting (#447)."""

    STAMP = "\n\n".join(["Copy from re:SearchTX"] * 11)

    def test_the_helper_sees_a_stamp_per_page(self):
        assert resolver.extract.looks_like_scanned_pdf(self.STAMP, 11)
        assert not resolver.extract.looks_like_scanned_pdf("word " * 400, 1)
        assert not resolver.extract.looks_like_scanned_pdf(self.STAMP, None)

    def test_the_resolver_reports_it_unverifiable_without_asking(self, monkeypatch):
        coverage = {
            "kind": "pdf",
            "pages_total": 11,
            "pages_read": 11,
            "partial": False,
        }

        class _Resp:
            status_code = 200
            url = "https://court.example/order.pdf"
            content = b"%PDF-1.4 stub"
            headers = {"Content-Type": "application/pdf"}
            encoding = None

            def raise_for_status(self):
                return None

        def model_must_not_be_called(*_a, **_k):
            raise AssertionError("the verifier was asked about a scan")

        monkeypatch.setattr(resolver, "safe_get", lambda url, **kw: _Resp())
        monkeypatch.setattr(
            resolver,
            "_extract_fetched_with_coverage",
            lambda resp, url: (self.STAMP, "pdf", coverage),
        )
        monkeypatch.setattr(resolver.llm, "call_provider", model_must_not_be_called)
        monkeypatch.setattr(
            resolver.wayback,
            "check",
            lambda *a, **k: {"archived": True, "snapshot_url": "x"},
        )
        result = resolver._resolve_known_url(
            "the court reissued the injunction",
            "https://court.example/order.pdf",
            api_keys={"mistral": {"api_key": "k"}},
        )
        assert result["verification"] == "unverifiable"
        assert "scanned image" in result["note"]
