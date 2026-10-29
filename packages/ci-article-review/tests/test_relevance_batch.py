"""Batched relevance checks (issue #453): claims citing one page share one
mistral-small call when the page is sent whole, only a grounded "supports" is
taken from that call, and every other case keeps the single-claim check it had
before.

``llm.call_provider`` is replaced with a recording fake, so nothing here makes
a model call.
"""

import json
import threading
import time

import pytest

from ci_article_review.adapters.citation import resolver

URL = "https://example.com/report"
PAGE = (
    "The district heating network delivers 40 MW of recovered heat. "
    "It began operating in 2015 under a twenty-year contract. "
    "Westin Building Exchange supplies the waste heat."
)
A = "The network delivers 40 MW of recovered heat."
B = "The network began operating in 2015."
C = "The heat comes from the Westin Building Exchange."


def _verdict(number, verdict="supports", quote=None):
    quotes = {
        1: "The district heating network delivers 40 MW of recovered heat.",
        2: "It began operating in 2015 under a twenty-year contract.",
        3: "Westin Building Exchange supplies the waste heat.",
    }
    return {
        "id": number,
        "verdict": verdict,
        "quote": quote if quote is not None else quotes.get(number, ""),
        "reason": "r",
    }


class _Recorder(list):
    """The calls made, carrying the fixture's knobs as attributes."""


@pytest.fixture
def calls(monkeypatch):
    """Records (system prompt, user prompt) per call. ``calls.answer[0]`` is a
    callable from the prompts to the parsed ``data`` the model returns."""
    made = _Recorder()
    lock = threading.Lock()

    def fake(provider, system, user, api_key, **kwargs):
        with lock:
            made.append((system, user))
        time.sleep(calls_delay[0])
        return {
            "failed": False,
            "data": made_answer[0](system, user),
            "model": "mistral-small-latest",
            "tokens": {"prompt": 100, "completion": 20},
        }

    calls_delay = [0.0]
    made_answer = [_default_answer]
    monkeypatch.setattr(resolver.llm, "call_provider", fake)
    made.answer = made_answer
    made.delay = calls_delay
    yield made
    resolver._relevance_batch = None


def _default_answer(system, user):
    if system is resolver._BATCH_VERIFICATION_SYSTEM_PROMPT:
        n = sum(1 for line in user.splitlines() if line.startswith("Claim "))
        return {"results": [_verdict(i) for i in range(1, n + 1)]}
    if A in user:
        return _verdict(1)
    if B in user:
        return _verdict(2)
    return _verdict(3)


def _with_batch(claims_by_url):
    resolver._relevance_batch = resolver._RelevanceBatch.from_claims(claims_by_url)
    return resolver._relevance_batch


def _verify(claim, content=PAGE, url=URL):
    return resolver._verify_relevance(
        claim, content, {"mistral": {"api_key": "k"}}, None, url=url
    )


class TestOneCallPerPage:
    def test_two_claims_on_one_page_make_one_call(self, calls):
        batch = _with_batch([(A, [URL]), (B, [URL])])
        first, first_log = _verify(A)
        second, second_log = _verify(B)
        assert len(calls) == 1
        assert calls[0][0] is resolver._BATCH_VERIFICATION_SYSTEM_PROMPT
        assert first["verdict"] == second["verdict"] == "supports"
        assert first["batch_size"] == second["batch_size"] == 2
        assert first_log["pass"] == "citation_verification:known_url_batch"
        # The call is logged once, by the claim that made it.
        assert second_log is None
        assert batch.stats == {
            "batches": 1,
            "claims_in_batches": 2,
            "served": 1,
            "kept": 2,
            "rechecked": 0,
        }

    def test_the_page_is_sent_once_and_the_claims_before_it(self, calls):
        _with_batch([(A, [URL]), (B, [URL]), (C, [URL])])
        _verify(A)
        user = calls[0][1]
        assert user.count(PAGE) == 1
        assert user.index('Claim 3: "') < user.index("<<<PAGE_CONTENT_")

    def test_a_sibling_that_read_different_bytes_is_checked_alone(self, calls):
        _with_batch([(A, [URL]), (B, [URL])])
        _verify(A)
        changed = PAGE + " A later edit."
        info, entry = _verify(B, content=changed)
        assert len(calls) == 2
        assert calls[1][0] is resolver._VERIFICATION_SYSTEM_PROMPT
        assert entry["pass"] == "citation_verification:known_url"
        assert "batch_size" not in info

    def test_more_siblings_than_the_cap_spend_a_second_batch(self, calls, monkeypatch):
        monkeypatch.setattr(resolver, "_MAX_BATCH_CLAIMS", 2)
        _with_batch([(A, [URL]), (B, [URL]), (C, [URL])])
        for claim in (A, B, C):
            assert _verify(claim)[0]["checked"]
        # A and B in one call; C finds no stored verdict, and alone it is not
        # a batch, so it takes the single check.
        assert [c[0] is resolver._BATCH_VERIFICATION_SYSTEM_PROMPT for c in calls] == [
            True,
            False,
        ]

    def test_concurrent_siblings_wait_for_one_call(self, calls):
        calls.delay[0] = 0.2
        claims = [A, B, C]
        _with_batch([(c, [URL]) for c in claims])
        results = {}

        def run(claim):
            results[claim] = _verify(claim)[0]

        threads = [threading.Thread(target=run, args=(c,)) for c in claims]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1
        assert all(results[c]["checked"] for c in claims)


class TestEverythingElseKeepsTheSingleCheck:
    def test_a_url_only_one_claim_names(self, calls):
        _with_batch([(A, [URL]), (B, ["https://example.org/other"])])
        _verify(A)
        assert calls[0][0] is resolver._VERIFICATION_SYSTEM_PROMPT

    def test_a_page_too_long_to_send_whole(self, calls):
        long_page = PAGE + " filler." * 4000
        assert len(long_page) > resolver._EXCERPT_HEAD + resolver._EXCERPT_TAIL
        _with_batch([(A, [URL]), (B, [URL])])
        _verify(A, content=long_page)
        _verify(B, content=long_page)
        assert [c[0] is resolver._VERIFICATION_SYSTEM_PROMPT for c in calls] == [
            True,
            True,
        ]

    def test_no_batch_outside_resolve_citations(self, calls):
        resolver._relevance_batch = None
        _verify(A)
        assert calls[0][0] is resolver._VERIFICATION_SYSTEM_PROMPT

    def test_a_sibling_the_batch_answer_left_out_is_checked_alone(self, calls):
        def only_first(system, user):
            if system is resolver._BATCH_VERIFICATION_SYSTEM_PROMPT:
                return {"results": [_verdict(1)]}
            return _default_answer(system, user)

        calls.answer[0] = only_first
        _with_batch([(A, [URL]), (B, [URL])])
        assert _verify(A)[0]["batch_size"] == 2
        info, entry = _verify(B)
        assert entry["pass"] == "citation_verification:known_url"
        assert info["checked"] and "batch_size" not in info

    def test_a_batch_that_answers_nothing_falls_back_and_keeps_its_cost(self, calls):
        def garbage(system, user):
            if system is resolver._BATCH_VERIFICATION_SYSTEM_PROMPT:
                return {"not": "what was asked"}
            return _default_answer(system, user)

        calls.answer[0] = garbage
        batch = _with_batch([(A, [URL]), (B, [URL])])
        info, entry = _verify(A)
        assert info["checked"]
        assert entry["pass"] == "citation_verification:known_url"
        assert [e["pass"] for e in batch.orphan_calls] == [
            "citation_verification:known_url_batch"
        ]

    def test_a_batched_supports_without_a_quote_on_the_page_is_rechecked(self, calls):
        # 12 of the 19 "supports" full batching lost were this: the batch
        # paraphrased the quote. The single check is asked instead.
        def invented(system, user):
            if system is resolver._BATCH_VERIFICATION_SYSTEM_PROMPT:
                return {
                    "results": [
                        _verdict(1, quote="Text that is not on the page at all."),
                        _verdict(2),
                    ]
                }
            return _default_answer(system, user)

        calls.answer[0] = invented
        batch = _with_batch([(A, [URL]), (B, [URL])])
        info, entry = _verify(A)
        assert info["checked"] and info["verdict"] == "supports"
        assert "batch_size" not in info
        assert entry["pass"] == "citation_verification:known_url"
        assert [e["pass"] for e in batch.orphan_calls] == [
            "citation_verification:known_url_batch"
        ]
        assert _verify(B)[0]["batch_size"] == 2
        assert batch.stats["kept"] == 1 and batch.stats["rechecked"] == 1


class TestOnlyASupportsIsTakenFromABatch:
    """The rest of the measured losses: a batch flipping a claim negative."""

    @staticmethod
    def _batch_says(verdicts):
        def answer(system, user):
            if system is resolver._BATCH_VERIFICATION_SYSTEM_PROMPT:
                return {
                    "results": [
                        _verdict(i, verdict=v, quote="" if v != "supports" else None)
                        for i, v in enumerate(verdicts, 1)
                    ]
                }
            return _default_answer(system, user)

        return answer

    @pytest.mark.parametrize(
        "verdict", ["not_addressed", "contradicts", "inconclusive"]
    )
    def test_a_negative_for_a_sibling_sends_it_to_the_single_check(
        self, calls, verdict
    ):
        calls.answer[0] = self._batch_says(["supports", verdict])
        batch = _with_batch([(A, [URL]), (B, [URL])])
        assert _verify(A)[0]["batch_size"] == 2
        info, entry = _verify(B)
        # The single check, which the fake answers "supports" with a real quote.
        assert entry["pass"] == "citation_verification:known_url"
        assert info["verdict"] == "supports" and "batch_size" not in info
        assert batch.stats["rechecked"] == 1
        assert batch.orphan_calls == []

    def test_a_negative_for_the_claim_that_asked_keeps_the_batch_cost(self, calls):
        calls.answer[0] = self._batch_says(["not_addressed", "supports"])
        batch = _with_batch([(A, [URL]), (B, [URL])])
        info, entry = _verify(A)
        assert entry["pass"] == "citation_verification:known_url"
        assert "batch_size" not in info
        # The batch call was still made and billed: logged once, as an orphan.
        assert [e["pass"] for e in batch.orphan_calls] == [
            "citation_verification:known_url_batch"
        ]
        second, second_log = _verify(B)
        assert second["batch_size"] == 2 and second_log is None
        assert len(calls) == 2

    def test_two_supports_resting_on_one_sentence_are_both_rechecked(self, calls):
        # The 2026-10-29 Microsoft case: a fragment came back "supports" by
        # quoting the sentence that answered its sibling.
        shared = "The district heating network delivers 40 MW of recovered heat."

        def answer(system, user):
            if system is resolver._BATCH_VERIFICATION_SYSTEM_PROMPT:
                return {
                    "results": [
                        _verdict(1, quote=shared),
                        _verdict(2, quote=shared + " It began operating in 2015"),
                        _verdict(3),
                    ]
                }
            return _default_answer(system, user)

        calls.answer[0] = answer
        batch = _with_batch([(A, [URL]), (B, [URL]), (C, [URL])])
        first, first_log = _verify(A)
        assert "batch_size" not in first
        assert first_log["pass"] == "citation_verification:known_url"
        second, second_log = _verify(B)
        assert "batch_size" not in second
        assert second_log["pass"] == "citation_verification:known_url"
        # A quote of its own is kept.
        assert _verify(C)[0]["batch_size"] == 3
        assert batch.stats["kept"] == 1 and batch.stats["rechecked"] == 2

    def test_a_negative_from_the_single_check_stands(self, calls):
        def answer(system, user):
            if system is resolver._BATCH_VERIFICATION_SYSTEM_PROMPT:
                return {"results": [_verdict(1), _verdict(2, "not_addressed", "")]}
            return {"verdict": "not_addressed", "quote": "", "reason": "r"}

        calls.answer[0] = answer
        _with_batch([(A, [URL]), (B, [URL])])
        _verify(A)
        info, _ = _verify(B)
        assert info["checked"] and info["verdict"] == "not_addressed"


class TestClaimsSharingAQuote:
    def test_sentences_shorter_than_the_floor_do_not_count(self):
        kept = {1: {"quote": "Yes. The plant opened in 2015."}, 2: {"quote": "Yes."}}
        assert resolver._claims_sharing_a_quote(kept) == []

    def test_case_and_whitespace_do_not_hide_a_shared_sentence(self):
        kept = {
            1: {"quote": "The plant opened in 2015."},
            2: {"quote": "It is big.  the PLANT opened   in 2015."},
            3: {"quote": "Something else entirely here."},
        }
        assert resolver._claims_sharing_a_quote(kept) == [1, 2]


class TestPrompts:
    def test_the_single_claim_prompt_is_intro_plus_the_shared_rules(self):
        assert (
            resolver._VERIFICATION_SYSTEM_PROMPT
            == resolver._VERIFICATION_SYSTEM_PROMPT_INTRO + resolver._VERIFICATION_RULES
        )
        assert resolver._BATCH_VERIFICATION_SYSTEM_PROMPT.endswith(
            resolver._VERIFICATION_RULES
        )

    def test_the_author_is_named_only_beside_a_first_person_claim(self):
        prompt = resolver._build_batch_verification_prompt(
            [A, "I visited the plant in 2019."], PAGE, author="Mike Hammett"
        )
        lines = prompt.splitlines()
        assert lines[1].startswith('Claim 1: "')
        assert lines[2].startswith('Claim 2: "')
        assert lines[3] == (
            "  Claim 2's author is Mike Hammett. Its first-person wording refers "
            "to them."
        )
        assert prompt.count("Mike Hammett") == 1


class TestResolveCitationsScopesTheBatch:
    def test_cleared_after_the_run_and_orphans_logged(self, calls, monkeypatch):
        monkeypatch.setattr(resolver, "_BATCH_RELEVANCE_ENABLED", True)
        seen = {}

        def fake_resolve_one(claim, *args, **kwargs):
            seen[claim] = resolver._relevance_batch
            return {"claim": claim, "resolved": False}

        monkeypatch.setattr(resolver, "_resolve_one", fake_resolve_one)
        monkeypatch.setattr(resolver, "_submit_missing_archives", lambda *a, **k: None)
        log = []
        resolver.resolve_citations(
            [
                {"claim": A, "known_urls": [URL]},
                {"claim": B, "known_urls": [URL]},
            ],
            [],
            verification_call_log=log,
        )
        batch = seen[A]
        assert batch is seen[B] and batch is not None
        assert batch.claims_by_url == {URL: [A, B]}
        assert resolver._relevance_batch is None
        json.dumps(log)


def test_off_by_default_every_claim_keeps_its_own_check(calls, monkeypatch):
    """The measured default (see ``_BATCH_RELEVANCE_ENABLED``)."""
    assert resolver._BATCH_RELEVANCE_ENABLED is False
    seen = {}

    def fake_resolve_one(claim, *args, **kwargs):
        seen[claim] = resolver._relevance_batch
        return {"claim": claim, "resolved": False}

    monkeypatch.setattr(resolver, "_resolve_one", fake_resolve_one)
    monkeypatch.setattr(resolver, "_submit_missing_archives", lambda *a, **k: None)
    resolver.resolve_citations(
        [{"claim": A, "known_urls": [URL]}, {"claim": B, "known_urls": [URL]}], []
    )
    assert seen[A].claims_by_url == {}
