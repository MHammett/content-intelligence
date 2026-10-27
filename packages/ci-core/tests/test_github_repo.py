"""``ci_core.github_repo``: a GitHub client, tested against an in-process fake.

``FakeGitHub`` is modelled on ``FakeWordPress`` (packages/ci-article-review/tests/
test_wordpress_images.py): it records every request and is attached through
``session=``, so nothing here touches the network. It keeps a real object model
-- blobs, trees, commits and branch heads -- because the behaviours worth testing
are the ones that depend on it: a commit that is not a fast forward is refused, a
tree built on a base tree keeps what the base held, and a file over 1 MB comes
back from the Contents API only when asked for raw.

Where the fake is deliberately strict, it is because the real API is. Each such
choice names its source in a comment.
"""

import base64
import hashlib
import json
import logging
import subprocess

import pytest
import requests

from ci_core import github_repo, http
from ci_core.github_repo import GitHubRepo, GitHubRepoError, git_blob_sha, resolve_token

REPO = "owner/name"
TOKEN = "ghp_exampleSECRETtoken0123456789"
RESET = 1_800_000_000


class Resp:
    """Enough of ``requests.Response`` for the client."""

    def __init__(
        self, status=200, body=None, *, content=None, headers=None, ctype=None
    ):
        self.status_code = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._body = body
        if content is None:
            content = json.dumps(body).encode() if body is not None else b""
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers.setdefault(
            "content-type",
            ctype
            or (
                "application/json; charset=utf-8" if body is not None else "text/plain"
            ),
        )

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


def _sha(*parts):
    return hashlib.sha1("\0".join(str(p) for p in parts).encode()).hexdigest()


class FakeGitHub:
    """One repository, ``owner/name``, behind the REST routes the client uses."""

    def __init__(self, token=TOKEN, *, empty=False):
        self.token = token
        self.requests = []  # (method, path, params, body), in order
        self.headers_seen = []  # the headers of each request
        self.urls = []
        self.blobs = {}  # sha -> bytes
        self.trees = {}  # sha -> {path: blob sha}, flattened
        self.commits = {}  # sha -> {"tree", "parents", "message"}
        self.branches = {}
        self.before_ref_update = None  # called with the fake, on every ref update
        self.injected = []  # [(method, path part, Resp, times left)]
        self.closed = False
        self._n = 0
        if not empty:
            self.external_commit("main", {"README.md": b"# readme\n"}, "initial")

    # -- test helpers --------------------------------------------------------

    def inject(self, method, part, resp, times=1):
        self.injected.append([method, part, resp, times])

    def external_commit(self, branch, files, message="by someone else"):
        """Another writer's commit on top of ``branch``, outside the client."""
        parent = self.branches.get(branch)
        flat = dict(self.trees[self.commits[parent]["tree"]]) if parent else {}
        for path, data in files.items():
            self.blobs[git_blob_sha(data)] = data
            flat[path] = git_blob_sha(data)
        sha = self._new_commit(
            self._new_tree(flat), [parent] if parent else [], message
        )
        self.branches[branch] = sha
        return sha

    def tree_at(self, ref):
        return self.trees[self.commits[self._resolve(ref)]["tree"]]

    @property
    def empty(self):
        return not self.commits

    def count(self, method, part):
        return sum(1 for m, p, _, _ in self.requests if m == method and part in p)

    # -- the object model ----------------------------------------------------

    def _new_tree(self, flat):
        sha = _sha("tree", json.dumps(sorted(flat.items())))
        self.trees[sha] = dict(flat)
        return sha

    def _new_commit(self, tree, parents, message):
        self._n += 1
        sha = _sha("commit", tree, parents, message, self._n)
        self.commits[sha] = {"tree": tree, "parents": list(parents), "message": message}
        return sha

    def _resolve(self, ref):
        if ref in self.commits:
            return ref
        return self.branches.get(ref)

    def _is_ancestor(self, old, new):
        seen, todo = set(), [new]
        while todo:
            sha = todo.pop()
            if sha == old:
                return True
            if sha not in seen:
                seen.add(sha)
                todo.extend(self.commits[sha]["parents"])
        return False

    # -- the session interface -----------------------------------------------

    def close(self):
        self.closed = True

    def request(
        self, method, url, headers=None, params=None, json=None, timeout=None, **kw
    ):
        headers = headers or {}
        prefix = "https://api.github.com"
        assert url.startswith(prefix), url
        path = url[len(prefix) :]
        self.urls.append(url)
        self.requests.append((method, path, params, json))
        self.headers_seen.append(headers)
        for item in self.injected:
            if item[0] == method and item[1] in path and item[3]:
                item[3] -= 1
                return item[2]
        if (
            self.token is not None
            and headers.get("Authorization") != f"Bearer {self.token}"
        ):
            return Resp(401, {"message": "Bad credentials"})
        base = f"/repos/{REPO}"
        if not (path == base or path.startswith(base + "/")):
            return Resp(404, {"message": "Not Found"})
        return self._route(method, path[len(base) :], headers, params or {}, json)

    def _route(self, method, path, headers, params, body):
        accept = headers.get("Accept", "")
        if (method, path) == ("GET", ""):
            return Resp(
                200,
                {
                    "default_branch": "main",
                    "private": True,
                    "permissions": {"push": True},
                },
            )
        git_routes = path.startswith(("/git/", "/commits"))
        if self.empty and git_routes:
            # https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-your-git-database
            return Resp(409, {"message": "Git Repository is empty."})
        if method == "GET" and path == "/commits":
            return Resp(200, [{"sha": s} for s in list(self.commits)[:1]])
        if method == "GET" and path.startswith("/commits/"):
            sha = self._resolve(path[len("/commits/") :])
            if sha is None:
                # Seen live: an unknown ref is a 422 here, not a 404.
                return Resp(422, {"message": "No commit found for SHA: " + path[9:]})
            assert accept == "application/vnd.github.sha"
            return Resp(200, content=sha.encode(), ctype="text/plain")
        if method == "GET" and path.startswith("/git/ref/heads/"):
            sha = self.branches.get(path[len("/git/ref/heads/") :])
            if sha is None:
                return Resp(404, {"message": "Not Found"})
            return Resp(200, {"object": {"sha": sha, "type": "commit"}})
        if method == "GET" and path.startswith("/git/commits/"):
            sha = path[len("/git/commits/") :]
            return Resp(
                200,
                {
                    "sha": sha,
                    "tree": {"sha": self.commits[sha]["tree"]},
                    "html_url": f"https://github.com/{REPO}/commit/{sha}",
                },
            )
        if (method, path) == ("POST", "/git/blobs"):
            assert body["encoding"] == "base64"
            data = base64.b64decode(body["content"])
            self.blobs[git_blob_sha(data)] = data
            return Resp(201, {"sha": git_blob_sha(data)})
        if (method, path) == ("POST", "/git/trees"):
            flat = dict(self.trees[body["base_tree"]]) if body.get("base_tree") else {}
            for e in body["tree"]:
                assert e["mode"] == "100644" and e["type"] == "blob"
                assert e["sha"] in self.blobs
                flat[e["path"]] = e["sha"]
            return Resp(201, {"sha": self._new_tree(flat)})
        if (method, path) == ("POST", "/git/commits"):
            sha = self._new_commit(body["tree"], body["parents"], body["message"])
            return Resp(
                201, {"sha": sha, "html_url": f"https://github.com/{REPO}/commit/{sha}"}
            )
        if method == "PATCH" and path.startswith("/git/refs/heads/"):
            return self._update_ref(path[len("/git/refs/heads/") :], body)
        if path.startswith("/contents"):
            return self._contents(
                method, path[len("/contents") :].lstrip("/"), accept, params, body
            )
        return Resp(404, {"message": "Not Found"})

    def _update_ref(self, branch, body):
        if self.before_ref_update:
            self.before_ref_update(self)
        current = self.branches.get(branch)
        if current is None:
            return Resp(422, {"message": "Reference does not exist"})
        if not body.get("force") and not self._is_ancestor(current, body["sha"]):
            # The words the real API uses for a non-fast-forward update.
            return Resp(422, {"message": "Update is not a fast forward"})
        self.branches[branch] = body["sha"]
        return Resp(
            200, {"ref": f"refs/heads/{branch}", "object": {"sha": body["sha"]}}
        )

    def _contents(self, method, path, accept, params, body):
        if method == "PUT":
            return self._put_contents(path, body)
        sha = self._resolve(params.get("ref", "main"))
        if sha is None:
            return Resp(404, {"message": "No commit found for the ref"})
        flat = self.trees[self.commits[sha]["tree"]]
        if path in flat:
            data = self.blobs[flat[path]]
            if "raw" in accept:
                return Resp(200, content=data, ctype="application/vnd.github.raw+json")
            # Over 1 MB the JSON form carries the metadata and no content.
            # https://docs.github.com/en/rest/repos/contents
            inline = len(data) <= 1_000_000
            return Resp(
                200,
                {
                    "type": "file",
                    "name": path.rsplit("/", 1)[-1],
                    "path": path,
                    "sha": flat[path],
                    "size": len(data),
                    "encoding": "base64" if inline else "none",
                    "content": base64.b64encode(data).decode() if inline else "",
                },
            )
        prefix = path + "/" if path else ""
        children = {}
        for p, blob in flat.items():
            if p.startswith(prefix):
                head, _, rest = p[len(prefix) :].partition("/")
                children[head] = (
                    ("dir", _sha("dir", prefix + head)) if rest else ("file", blob)
                )
        if children:
            return Resp(
                200,
                [
                    {"name": n, "path": prefix + n, "type": t, "sha": s}
                    for n, (t, s) in sorted(children.items())
                ],
            )
        return Resp(404, {"message": "Not Found"})

    def _put_contents(self, path, body):
        data = base64.b64decode(body["content"])
        if self.empty:
            sha = self.external_commit(body["branch"], {path: data}, body["message"])
        else:
            if path in self.tree_at(body["branch"]):
                return Resp(
                    422, {"message": 'Invalid request.\n\n"sha" wasn\'t supplied.'}
                )
            sha = self.external_commit(body["branch"], {path: data}, body["message"])
        return Resp(201, {"commit": {"sha": sha}, "content": {"path": path}})


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def gh(fake):
    return GitHubRepo(REPO, TOKEN, session=fake)


def assert_no_token(*texts):
    for text in texts:
        assert TOKEN not in str(text)


def raises(kind, fn, *args, **kw):
    """Call ``fn``, expect a ``GitHubRepoError`` of ``kind``, and prove it holds no token."""
    with pytest.raises(GitHubRepoError) as info:
        fn(*args, **kw)
    err = info.value
    assert err.kind == kind, (err.kind, str(err))
    assert_no_token(str(err), repr(err), err.args)
    return err


# -- git_blob_sha ------------------------------------------------------------


@pytest.mark.parametrize(
    "data, expected",
    [
        # Each is what `git hash-object --stdin` printed for the same bytes.
        (b"", "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"),
        (b"hello\n", "ce013625030ba8dba906f756967f9e9ca394464a"),
        (
            "café\r\nline2\r\n".encode("utf-8"),
            "1de205e5db9994d6d4fd5259e3bd73518a74d7a2",
        ),
    ],
)
def test_git_blob_sha_matches_git_hash_object(data, expected):
    assert git_blob_sha(data) == expected


# -- reads -------------------------------------------------------------------


def test_info_reports_default_branch_privacy_and_push_access(gh):
    info = gh.info()
    assert (info.default_branch, info.private, info.can_push) == ("main", True, True)


def test_requests_carry_the_token_only_in_the_authorization_header(gh, fake):
    gh.info()
    gh.read("README.md", "main")
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for h in fake.headers_seen)
    assert all(h["User-Agent"] == http.USER_AGENT for h in fake.headers_seen)
    assert_no_token(
        *fake.urls, *[r[2] for r in fake.requests], *[r[3] for r in fake.requests]
    )


def test_reading_a_file_over_1mb_returns_the_exact_bytes(gh, fake):
    big = ("héllo ✓ line\r\n" * 150_000).encode("utf-8")
    assert len(big) > 2_000_000
    fake.external_commit("main", {"docs/report.json": big})
    got = gh.read("docs/report.json", "main")
    assert got.data == big and b"\r\n" in got.data
    assert got.blob_sha == git_blob_sha(big)
    assert got.commit_sha == fake.branches["main"]
    # The reason it works: the read asked for raw, and the fake only inlines
    # content over 1 MB for a raw request.
    raw_reads = [
        h
        for (m, p, _, _), h in zip(fake.requests, fake.headers_seen)
        if "/contents/" in p
    ]
    assert raw_reads and all("raw" in h["Accept"] for h in raw_reads)


def test_read_of_a_missing_file_is_not_found(gh):
    raises("not_found", gh.read, "nope.md", "main")


def test_read_of_a_directory_is_an_error_not_a_listing(gh, fake):
    fake.external_commit("main", {"docs/a.md": b"a"})
    err = raises("other", gh.read, "docs", "main")
    assert "directory" in str(err)


def test_list_dir_returns_entries_and_a_missing_directory_is_empty(gh, fake):
    fake.external_commit("main", {"drafts/a.md": b"a", "drafts/sub/b.md": b"b"})
    entries = gh.list_dir("drafts", "main")
    assert [(e.name, e.path, e.type) for e in entries] == [
        ("a.md", "drafts/a.md", "file"),
        ("sub", "drafts/sub", "dir"),
    ]
    assert entries[0].sha == git_blob_sha(b"a")
    assert gh.list_dir("nothing/here", "main") == []
    assert {e.name for e in gh.list_dir("", "main")} == {"README.md", "drafts"}


def test_list_dir_on_a_missing_ref_is_an_error_not_an_empty_listing(gh):
    raises("not_found", gh.list_dir, "drafts", "no-such-branch")


def test_resolve_pins_a_read(gh, fake):
    pinned = gh.resolve("main")
    fake.external_commit("main", {"README.md": b"# changed\n"})
    assert gh.read("README.md", pinned).data == b"# readme\n"
    assert gh.read("README.md", "main").data == b"# changed\n"
    assert gh.resolve("main") != pinned


def test_a_full_sha_is_not_looked_up_again(gh, fake):
    sha = fake.branches["main"]
    before = len(fake.requests)
    assert gh.resolve(sha) == sha
    assert len(fake.requests) == before


# -- commit_files ------------------------------------------------------------


def test_two_new_paths_make_one_commit(gh, fake):
    before = len(fake.commits)
    result = gh.commit_files(
        "main", {"a/one.md": b"one\r\n", "a/two.md": "dué".encode()}, "add two"
    )
    assert len(fake.commits) == before + 1
    assert fake.branches["main"] == result.commit_sha
    assert result.written == ("a/one.md", "a/two.md") and result.unchanged == ()
    assert result.html_url.endswith(result.commit_sha)
    tree = fake.tree_at("main")
    assert fake.blobs[tree["a/one.md"]] == b"one\r\n"
    assert "README.md" in tree  # the base tree survived
    assert fake.commits[result.commit_sha]["message"] == "add two"
    assert fake.count("PATCH", "/git/refs/") == 1
    assert fake.requests[-1][3]["force"] is False


def test_an_identical_path_is_unchanged_and_all_unchanged_makes_no_commit(gh, fake):
    files = {"a.md": b"one", "b.md": b"two"}
    first = gh.commit_files("main", files, "add")
    commits, requests_before = len(fake.commits), len(fake.requests)
    again = gh.commit_files("main", files, "add again")
    assert again.written == () and set(again.unchanged) == set(files)
    assert again.commit_sha == first.commit_sha
    assert len(fake.commits) == commits
    assert not [r for r in fake.requests[requests_before:] if r[0] != "GET"]


def test_a_mix_writes_the_new_and_reports_the_identical(gh, fake):
    gh.commit_files("main", {"a.md": b"one"}, "add")
    result = gh.commit_files("main", {"a.md": b"one", "b.md": b"two"}, "more")
    assert result.written == ("b.md",) and result.unchanged == ("a.md",)


def test_a_different_existing_path_raises_exists_and_writes_nothing(gh, fake):
    gh.commit_files("main", {"a.md": b"one"}, "add")
    head, blobs = fake.branches["main"], len(fake.blobs)
    posts = fake.count("POST", "")
    err = raises(
        "exists", gh.commit_files, "main", {"new.md": b"x", "a.md": b"CHANGED"}, "m"
    )
    assert "a.md" in str(err)
    assert fake.branches["main"] == head
    assert len(fake.blobs) == blobs and fake.count("POST", "") == posts


def test_a_path_that_is_a_directory_is_refused(gh, fake):
    fake.external_commit("main", {"docs/a.md": b"a"})
    raises("exists", gh.commit_files, "main", {"docs": b"x"}, "m")


def test_a_missing_branch_is_not_found(gh):
    raises("not_found", gh.commit_files, "nope", {"a.md": b"x"}, "m")


def test_the_race_is_retried_on_top_of_the_other_writers_commit(gh, fake):
    moves = []

    def once(f):
        if not moves:
            moves.append(f.external_commit("main", {"theirs.md": b"theirs"}))

    fake.before_ref_update = once
    result = gh.commit_files("main", {"mine.md": b"mine"}, "mine")
    tree = fake.tree_at("main")
    assert fake.blobs[tree["theirs.md"]] == b"theirs"  # their file survived
    assert fake.blobs[tree["mine.md"]] == b"mine"
    assert fake.count("PATCH", "/git/refs/") == 2
    assert fake.commits[result.commit_sha]["parents"] == [moves[0]]


def test_a_409_on_the_ref_update_is_treated_as_a_race_too(gh, fake):
    fake.inject("PATCH", "/git/refs/", Resp(409, {"message": "Conflict"}))
    gh.commit_files("main", {"mine.md": b"mine"}, "mine")
    assert fake.count("PATCH", "/git/refs/") == 2
    assert "mine.md" in fake.tree_at("main")


def test_a_head_that_moves_every_time_raises_conflict_after_the_retries(gh, fake):
    n = []

    def always(f):
        n.append(1)
        f.external_commit("main", {f"theirs{len(n)}.md": b"x"})

    fake.before_ref_update = always
    err = raises(
        "conflict", gh.commit_files, "main", {"mine.md": b"mine"}, "m", retries=2
    )
    assert fake.count("PATCH", "/git/refs/") == 3
    assert "mine.md" not in fake.tree_at("main")
    assert "Nothing was lost" in str(err)


def test_a_422_that_is_not_a_fast_forward_is_not_retried(gh, fake):
    fake.inject("PATCH", "/git/refs/", Resp(422, {"message": "Validation Failed"}))
    err = raises("other", gh.commit_files, "main", {"mine.md": b"mine"}, "m")
    assert err.status == 422
    assert fake.count("PATCH", "/git/refs/") == 1


# -- replace -----------------------------------------------------------------


def test_replace_succeeds_when_the_expected_blob_matches(gh, fake):
    v1, v2 = b"managed v1\n", b"managed v2\n"
    fake.external_commit("main", {"AGENTS.md": v1})
    result = gh.commit_files(
        "main", {"AGENTS.md": v2}, "refresh", replace={"AGENTS.md": git_blob_sha(v1)}
    )
    assert result.written == ("AGENTS.md",)
    assert gh.read("AGENTS.md", "main").data == v2


def test_replace_refuses_a_file_someone_else_changed_and_commits_nothing(gh, fake):
    v1 = b"managed v1\n"
    fake.external_commit("main", {"AGENTS.md": v1})
    expected = git_blob_sha(v1)
    fake.external_commit("main", {"AGENTS.md": b"edited by hand\n"})
    head = fake.branches["main"]
    err = raises(
        "exists",
        gh.commit_files,
        "main",
        {"AGENTS.md": b"managed v2\n", "new.md": b"n"},
        "refresh",
        replace={"AGENTS.md": expected},
    )
    assert "AGENTS.md" in str(err)
    assert fake.branches["main"] == head and "new.md" not in fake.tree_at("main")


def test_replace_of_an_identical_file_is_unchanged(gh, fake):
    fake.external_commit("main", {"AGENTS.md": b"same"})
    result = gh.commit_files(
        "main", {"AGENTS.md": b"same"}, "refresh", replace={"AGENTS.md": "0" * 40}
    )
    assert result.unchanged == ("AGENTS.md",) and result.written == ()


def test_a_path_not_named_in_replace_is_never_overwritten(gh, fake):
    fake.external_commit("main", {"AGENTS.md": b"v1", "other.md": b"o"})
    raises(
        "exists",
        gh.commit_files,
        "main",
        {"other.md": b"new"},
        "m",
        replace={"AGENTS.md": git_blob_sha(b"v1")},
    )


# -- secrets -----------------------------------------------------------------


def test_refuse_if_contains_refuses_before_anything_is_sent(gh, fake):
    value = "hunter2-do-not-publish"
    err = raises(
        "secret",
        gh.commit_files,
        "main",
        {"ok.md": b"fine", "leak.md": f"key = {value}".encode()},
        "m",
        refuse_if_contains=(value,),
    )
    assert "leak.md" in str(err) and "ok.md" not in str(err)
    assert value not in str(err) and value not in repr(err)
    assert fake.requests == []


def test_the_token_itself_is_always_refused(gh, fake):
    raises(
        "secret", gh.commit_files, "main", {"n.md": f"export T={TOKEN}".encode()}, "m"
    )
    assert fake.requests == []


def test_the_token_is_refused_in_the_commit_message_too(gh, fake):
    raises("secret", gh.commit_files, "main", {"n.md": b"fine"}, f"note {TOKEN}")
    assert fake.requests == []


def test_an_empty_refuse_value_matches_nothing(gh, fake):
    gh.commit_files("main", {"n.md": b"fine"}, "m", refuse_if_contains=("", None))
    assert "n.md" in fake.tree_at("main")


# -- empty repositories ------------------------------------------------------


def test_commit_files_on_an_empty_repository_names_seed_empty():
    fake = FakeGitHub(empty=True)
    gh = GitHubRepo(REPO, TOKEN, session=fake)
    err = raises("empty", gh.commit_files, "main", {"a.md": b"x"}, "m")
    assert "seed_empty" in str(err) and err.status == 409
    assert fake.empty


def test_seed_empty_makes_the_first_commit_and_commit_files_then_works():
    fake = FakeGitHub(empty=True)
    gh = GitHubRepo(REPO, TOKEN, session=fake)
    sha = gh.seed_empty("README.md", b"# hello\n", "first commit")
    assert fake.branches["main"] == sha
    assert fake.commits[sha]["message"] == "first commit"
    assert gh.resolve("main") == sha
    result = gh.commit_files("main", {"a.md": b"x"}, "second")
    assert fake.commits[result.commit_sha]["parents"] == [sha]


def test_seed_empty_honours_the_branch():
    fake = FakeGitHub(empty=True)
    GitHubRepo(REPO, TOKEN, session=fake).seed_empty("a.md", b"x", "m", branch="trunk")
    assert list(fake.branches) == ["trunk"]


def test_seed_empty_refuses_a_repository_that_has_commits(gh, fake):
    raises("exists", gh.seed_empty, "new.md", b"x", "m")
    assert fake.count("PUT", "") == 0


def test_seed_empty_refuses_the_token():
    fake = FakeGitHub(empty=True)
    gh = GitHubRepo(REPO, TOKEN, session=fake)
    raises("secret", gh.seed_empty, "a.md", TOKEN.encode(), "m")
    assert fake.requests == []


# -- errors ------------------------------------------------------------------


def test_401_is_auth():
    gh = GitHubRepo(REPO, "ghp_wrongTOKENvalue", session=FakeGitHub())
    err = raises("auth", gh.info)
    assert err.status == 401
    assert "ghp_wrongTOKENvalue" not in str(err)


def test_404_is_not_found_and_says_why_it_is_ambiguous(fake):
    err = raises("not_found", GitHubRepo("owner/missing", TOKEN, session=fake).info)
    assert err.status == 404
    assert "cannot see" in str(err)


@pytest.mark.parametrize("status", [403, 429])
def test_an_exhausted_rate_limit_is_rate_limited_with_reset_and_no_retry(
    gh, fake, status
):
    fake.inject(
        "GET",
        "/commits/",
        Resp(
            status,
            {"message": "API rate limit exceeded"},
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(RESET)},
        ),
        times=5,
    )
    err = raises("rate_limited", gh.resolve, "main")
    assert err.reset_at == RESET and err.status == status
    assert fake.count("GET", "/commits/") == 1


def test_a_secondary_rate_limit_with_retry_after_is_rate_limited(gh, fake):
    fake.inject(
        "GET",
        "/commits/",
        Resp(403, {"message": "secondary rate limit"}, headers={"Retry-After": "60"}),
    )
    err = raises("rate_limited", gh.resolve, "main")
    assert err.reset_at is not None


def test_any_other_403_is_forbidden(gh, fake):
    fake.inject(
        "GET",
        "/commits/",
        Resp(
            403,
            {"message": "Resource not accessible"},
            headers={"X-RateLimit-Remaining": "4000"},
        ),
    )
    raises("forbidden", gh.resolve, "main")


def test_a_network_failure_is_an_other_error_with_no_token(fake):
    class Down:
        def request(self, *a, **kw):
            raise requests.ConnectionError(
                f"connection refused (while sending {TOKEN})"
            )

    err = raises("other", GitHubRepo(REPO, TOKEN, session=Down()).info)
    assert "ConnectionError" in str(err)


def test_the_token_never_reaches_an_error_log_or_repr(gh, fake, caplog):
    caplog.set_level(logging.DEBUG)
    # A server that echoes the credential back in its error text.
    echo = Resp(500, {"message": f"upstream said token={TOKEN} is bad"})
    fake.inject("GET", "/git/ref/", echo)
    errors = []
    for call in (
        lambda: gh.commit_files("main", {"a.md": b"x"}, "m"),
        lambda: gh.read("nope.md", "main"),
        lambda: GitHubRepo(REPO, "other", session=fake).info(),
        lambda: gh.commit_files("main", {"a.md": TOKEN.encode()}, "m"),
    ):
        with pytest.raises(GitHubRepoError) as info:
            call()
        errors.append(info.value)
    assert "[REDACTED]" in str(errors[0])
    for err in errors:
        assert_no_token(str(err), repr(err), err.args, err.__dict__)
    assert_no_token(caplog.text, repr(gh), str(gh))


# -- construction, tokens and the session ------------------------------------


def test_a_repository_name_must_be_owner_slash_name():
    for bad in (
        "",
        "name",
        "a/b/c",
        "a/ b",
        "../x",
        "owner/..",
        "owner/.",
        "own_er/name",
    ):
        with pytest.raises(ValueError):
            GitHubRepo(bad, TOKEN, session=FakeGitHub())


def test_a_token_is_not_sent_over_plain_http():
    with pytest.raises(ValueError):
        GitHubRepo(REPO, TOKEN, session=FakeGitHub(), api_url="http://api.github.com")


@pytest.mark.parametrize(
    "path", ["", "/abs.md", "a//b.md", "../x.md", "a/../b.md", "a\\b.md", "./a.md"]
)
def test_paths_must_be_clean_relative_posix_paths(gh, path):
    with pytest.raises(ValueError):
        gh.commit_files("main", {path: b"x"}, "m")
    with pytest.raises(ValueError):
        gh.read(path, "main")


def test_with_no_session_it_uses_the_os_trust_session_and_closes_it(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(github_repo, "os_trust_session", lambda: fake)
    with GitHubRepo(REPO, TOKEN) as gh:
        assert gh.info().default_branch == "main"
    assert fake.closed


def test_a_session_passed_in_is_not_closed(fake):
    GitHubRepo(REPO, TOKEN, session=fake).close()
    assert not fake.closed


def test_the_public_os_trust_session_verifies_through_the_os_store():
    with http.os_trust_session() as session:
        assert isinstance(
            session.get_adapter("https://api.github.com/"), http._OSTrustAdapter
        )


def test_no_token_sends_no_authorization_header():
    fake = FakeGitHub(token=None)
    GitHubRepo(REPO, None, session=fake).info()
    assert "Authorization" not in fake.headers_seen[0]


# -- resolve_token -----------------------------------------------------------


def _never(*a, **kw):
    raise AssertionError("gh must not be run when GITHUB_TOKEN is set")


def test_the_env_var_wins_over_gh():
    assert resolve_token({"GITHUB_TOKEN": " tok_env \n"}, _never) == "tok_env"


@pytest.mark.parametrize("env", [{}, {"GITHUB_TOKEN": ""}, {"GITHUB_TOKEN": "  "}])
def test_gh_is_the_fallback(env):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="gho_fromgh\n", stderr="")

    assert resolve_token(env, run) == "gho_fromgh"
    assert calls == [["gh", "auth", "token"]]


def test_a_missing_gh_binary_gives_none():
    def run(cmd, **kw):
        raise FileNotFoundError("gh")

    assert resolve_token({}, run) is None


def test_gh_failing_or_printing_nothing_gives_none():
    def failed(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in")

    def blank(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")

    def hung(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 20)

    assert resolve_token({}, failed) is None
    assert resolve_token({}, blank) is None
    assert resolve_token({}, hung) is None
