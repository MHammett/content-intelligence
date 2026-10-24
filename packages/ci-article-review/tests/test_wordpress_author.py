"""``WordPress author:`` is a login in the handoff and a user id on the wire.

The template tells the author to write a login name. The REST API takes only an
integer user id in ``author``, so a login sent as it stood was a 400 ("author is
not of type integer") and no post, found by really publishing one (issue #369).
The suite's fake WordPress took any value there, so a rehearsal passed.

These assert what leaves the process: the lookup that turns a login into an id
and the credentials it carries, what is sent as ``author``, and what is said when
no account matches, for a draft and for a live publish.
"""

from .test_wordpress_images import PNG, FakeWordPress, Resp, _push

import ci_article_review.adapters.cms.wordpress as wp


def _users_gets(fake):
    return [url for method, url in fake.requests if method == "GET" and "/users" in url]


class TestFakeIsStrict:
    """The fake has to refuse what WordPress refuses, or nothing above means much."""

    def test_a_string_author_is_a_400(self):
        fake = FakeWordPress()
        resp = fake.post(
            "https://example.com/wp-json/wp/v2/posts",
            json={"title": "T", "author": "admin"},
        )
        assert resp.status_code == 400
        assert resp.json()["data"]["params"] == {
            "author": "author is not of type integer."
        }

    def test_an_integer_author_is_accepted(self):
        fake = FakeWordPress()
        resp = fake.post(
            "https://example.com/wp-json/wp/v2/posts",
            json={"title": "T", "author": 2},
        )
        assert resp.status_code == 200

    def test_the_users_endpoint_wants_credentials(self):
        fake = FakeWordPress()
        resp = fake.get(
            "https://example.com/wp-json/wp/v2/users",
            params={"slug": "admin"},
            headers={},
        )
        assert resp.status_code == 401


class TestAuthorLogin:
    def test_a_login_is_sent_as_the_users_id(self, tmp_path):
        result, fake = _push(
            tmp_path, "Body.", params={"post_type": "post", "author": "admin"}
        )
        assert result["success"] is True, result
        assert fake.created[0]["author"] == 2
        assert "unresolved_author" not in result

    def test_the_lookup_carries_the_push_credentials(self, tmp_path):
        """The users endpoint is not public; the fake answers 401 without them."""
        seen = []
        real_get = FakeWordPress.get

        def spy(self, url, **kw):
            if url.endswith("/users"):
                seen.append(kw)
            return real_get(self, url, **kw)

        FakeWordPress.get = spy
        try:
            result, _ = _push(tmp_path, "Body.", params={"author": "admin"})
        finally:
            FakeWordPress.get = real_get
        assert result["success"] is True, result
        (call,) = seen
        assert call["params"] == {"slug": "admin"}
        assert call["headers"]["Authorization"].startswith("Basic ")

    def test_the_lookup_is_by_the_login_as_written(self, tmp_path):
        """WordPress sanitises the slug, so a capitalised login still finds it."""
        result, fake = _push(tmp_path, "Body.", params={"author": "Admin"})
        assert result["success"] is True, result
        assert fake.created[0]["author"] == 2

    def test_a_page_is_filed_under_the_author_too(self, tmp_path):
        result, fake = _push(
            tmp_path, "Body.", params={"post_type": "page", "author": "admin"}
        )
        assert result["success"] is True, result
        assert fake.created[0]["author"] == 2


class TestAuthorAlreadyAnId:
    def test_an_integer_is_sent_without_a_lookup(self, tmp_path):
        result, fake = _push(tmp_path, "Body.", params={"author": 5})
        assert result["success"] is True, result
        assert fake.created[0]["author"] == 5
        assert _users_gets(fake) == []

    def test_a_string_of_digits_is_an_id(self, tmp_path):
        """A handoff is text: ``WordPress author: 5`` arrives as ``"5"``."""
        result, fake = _push(tmp_path, "Body.", params={"author": " 5 "})
        assert result["success"] is True, result
        assert fake.created[0]["author"] == 5
        assert _users_gets(fake) == []


class TestNoAuthor:
    def test_a_blank_author_sends_none_and_looks_up_none(self, tmp_path):
        for blank in (None, "", "   "):
            result, fake = _push(tmp_path, "Body.", params={"author": blank})
            assert result["success"] is True, result
            assert "author" not in fake.created[0]
            assert _users_gets(fake) == []
            assert "unresolved_author" not in result


class TestUnresolvedAuthor:
    def test_a_draft_is_pushed_without_the_author_and_says_so(self, tmp_path):
        result, fake = _push(tmp_path, "Body.", params={"author": "ghost"})
        assert result["success"] is True, result
        assert "author" not in fake.created[0]
        assert result["unresolved_author"] == "ghost"

    def test_a_refused_lookup_is_unresolved_not_a_crash(self, tmp_path):
        """An account that may not list users gets a 403 from the endpoint."""
        fake = FakeWordPress()
        fake.users_response = Resp(403, {"code": "rest_user_cannot_view"})
        result, fake = _push(tmp_path, "Body.", fake=fake, params={"author": "admin"})
        assert result["success"] is True, result
        assert "author" not in fake.created[0]
        assert result["unresolved_author"] == "admin"

    def test_a_live_publish_refuses_and_creates_nothing(self, tmp_path):
        result, fake = _push(
            tmp_path, "Body.", params={"author": "ghost"}, publish_live=True
        )
        assert result["success"] is False
        assert "ghost" in result["error"]
        assert result["unresolved_author"] == "ghost"
        assert fake.created == [], "the post was created despite the refusal"
        assert fake.post_requests == []

    def test_a_live_publish_can_be_forced(self, tmp_path):
        result, fake = _push(
            tmp_path,
            "Body.",
            params={"author": "ghost"},
            publish_live=True,
            allow_missing_terms=True,
        )
        assert result["success"] is True, result
        assert "author" not in fake.created[0]
        assert result["unresolved_author"] == "ghost"

    def test_a_refusal_uploads_nothing(self, tmp_path):
        """Decided before the first image goes up, as the term refusal is."""
        result, fake = _push(
            tmp_path,
            "![Map](map.png)\n",
            files={"map.png": PNG},
            params={"author": "ghost"},
            publish_live=True,
        )
        assert result["success"] is False
        assert fake.media_requests == []


class TestPrintAuthorResult:
    def test_it_names_the_author_and_the_fix(self, capsys):
        wp.print_author_result({"success": True, "unresolved_author": "ghost"})
        out = capsys.readouterr().out
        assert "'ghost'" in out
        assert "NOT applied" in out
        assert "login" in out

    def test_it_prints_nothing_when_the_author_resolved(self, capsys):
        wp.print_author_result({"success": True})
        assert capsys.readouterr().out == ""
