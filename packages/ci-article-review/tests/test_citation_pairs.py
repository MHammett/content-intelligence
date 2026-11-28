"""A draft's cited addresses each carry their archive copy, or are reported."""

from ci_article_review.adapters.cms import citation_pairs as cp

WB = "https://web.archive.org/web/20261012204141/"


def test_paired_source_is_not_reported():
    text = f"x https://a.org/r (archived: {WB}https://a.org/r)."
    assert cp.unpaired_citations(text) == []
    assert cp.describe_unpaired(text) == ""


def test_live_only_source_is_reported():
    text = f"https://a.org/r and https://b.org/p (archived: {WB}https://b.org/p)"
    assert cp.unpaired_citations(text) == ["https://a.org/r"]
    msg = cp.describe_unpaired(text)
    assert "1 of 2" in msg and "https://a.org/r" in msg


def test_scheme_www_slash_and_fragment_do_not_break_pairing():
    text = f"http://www.a.org/r/#x {WB}https://a.org/r"
    assert cp.unpaired_citations(text) == []


def test_own_site_and_markdown_links_and_trailing_punctuation():
    text = f"[s](https://a.org/r). [me](https://www.mine.net/x) {WB}https://a.org/r"
    assert cp.unpaired_citations(text, own_hosts=["mine.net"]) == []
    assert cp.unpaired_citations("[s](https://a.org/r).") == ["https://a.org/r"]


def test_query_string_is_part_of_the_address():
    text = f"https://a.org/r?id=2 {WB}https://a.org/r?id=1"
    assert cp.unpaired_citations(text) == ["https://a.org/r?id=2"]


def test_checklist_asks_for_archive_pairs_and_search_console():
    from ci_article_review.adapters.cms import wordpress as wp

    text = wp.checklist_text({})
    assert "archive.org copy" in text
    assert "Google Search Console" in text
