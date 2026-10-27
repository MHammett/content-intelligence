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

It also holds pull requests (a merge makes a two-parent commit and moves the
base), reviews, annotated tag objects, labels, issues, repository settings, the
compare endpoint, and ``Link`` pagination with a configurable page size.
"""

import base64
import hashlib
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlencode

import pytest
import requests

from ci_core import github_repo, http
from ci_core.github_repo import (
    GitHubRepo,
    GitHubRepoError,
    RepoInfo,
    Signature,
    git_blob_sha,
    resolve_token,
)

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
        self.tags = {}  # tag name -> sha it points at (a tag object, or a commit)
        self.tag_objects = {}  # sha -> {"tag", "message", "object", "type", "tagger"}
        self.pulls = {}  # number -> dict, see _create_pull
        self.reviews = {}  # pull number -> [dict]
        self.labels = {}  # lower-case name -> dict
        self.issues = {}  # number -> {"title", "body", "labels", "comments": [..]}
        self.settings = {
            "allow_merge_commit": True,
            "allow_squash_merge": True,
            "allow_rebase_merge": True,
            "delete_branch_on_merge": False,
        }
        self.page_size = None  # cap on items per page, whatever per_page asks for
        # GitHub refuses APPROVE from the pull request's own author, and the
        # token here is always that author.
        self.author_is_reviewer = True
        self._n = 0
        self._numbers = 0  # pull requests and issues share one number space
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

    def _new_commit(self, tree, parents, message, author=None, committer=None):
        self._n += 1
        sha = _sha("commit", tree, parents, message, self._n)
        self.commits[sha] = {
            "tree": tree,
            "parents": list(parents),
            "message": message,
            "author": author,
            "committer": committer,
        }
        return sha

    def _resolve(self, ref):
        if ref in self.commits:
            return ref
        if ref in self.branches:
            return self.branches[ref]
        # GET /commits/{tag} peels an annotated tag to the commit it points at.
        sha = self.tags.get(ref)
        if sha in self.tag_objects:
            return self.tag_objects[sha]["object"]
        return sha

    def _next_number(self):
        self._numbers += 1
        return self._numbers

    def pull_by_head(self, head_ref):
        return next(p for p in self.pulls.values() if p["head"] == head_ref)

    def _page(self, path, items, params):
        """One page of ``items`` and, while there is more, GitHub's ``Link`` header."""
        per = int(params.get("per_page", 30))
        if self.page_size:
            per = min(per, self.page_size)
        page = int(params.get("page", 1))
        chunk = items[(page - 1) * per : page * per]
        headers = {}
        if page * per < len(items):
            base = f"https://api.github.com/repos/{REPO}{path}"
            query = lambda n: urlencode({**params, "per_page": per, "page": n})  # noqa: E731
            last = -(-len(items) // per)
            parts = [f'<{base}?{query(page + 1)}>; rel="next"']
            parts.append(f'<{base}?{query(last)}>; rel="last"')
            if page > 1:
                parts.append(f'<{base}?{query(page - 1)}>; rel="prev"')
            headers["Link"] = ", ".join(parts)
        return Resp(200, chunk, headers=headers)

    def _ancestors(self, sha):
        """``sha`` and every commit reachable from it."""
        seen, todo = set(), [sha]
        while todo:
            cur = todo.pop()
            if cur not in seen:
                seen.add(cur)
                todo.extend(self.commits[cur]["parents"])
        return seen

    def _merge_base(self, a, b):
        """The best common ancestor: one no other common ancestor descends from."""
        common = self._ancestors(a) & self._ancestors(b)
        best = [
            c
            for c in common
            if not any(c != o and c in self._ancestors(o) for o in common)
        ]
        return best[0] if len(best) == 1 else None

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
            return Resp(200, self._repo_json())
        if (method, path) == ("PATCH", ""):
            assert set(body) <= set(self.settings), body
            self.settings.update(body)
            return Resp(200, self._repo_json())
        git_routes = path.startswith(("/git/", "/commits", "/compare/"))
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
        if method == "GET" and path.startswith("/git/ref/tags/"):
            sha = self.tags.get(path[len("/git/ref/tags/") :])
            if sha is None:
                return Resp(404, {"message": "Not Found"})
            kind = "tag" if sha in self.tag_objects else "commit"
            return Resp(200, {"object": {"sha": sha, "type": kind}})
        if method == "GET" and path.startswith("/git/matching-refs/"):
            prefix = "refs/" + unquote(path[len("/git/matching-refs/") :])
            refs = [(f"refs/heads/{b}", s) for b, s in self.branches.items()]
            refs += [(f"refs/tags/{t}", s) for t, s in self.tags.items()]
            found = [
                {
                    "ref": r,
                    "object": {
                        "sha": s,
                        "type": "tag" if s in self.tag_objects else "commit",
                    },
                }
                for r, s in sorted(refs)
                if r.startswith(prefix)
            ]
            return self._page(path, found, params)
        if (method, path) == ("POST", "/git/refs"):
            return self._create_ref(body)
        if (method, path) == ("POST", "/git/tags"):
            assert body["type"] == "commit" and body["object"] in self.commits
            assert set(body["tagger"]) == {"name", "email", "date"}
            sha = _sha("tag", body["tag"], body["object"], body["message"], self._n)
            self._n += 1
            self.tag_objects[sha] = dict(body)
            return Resp(201, {"sha": sha, "tag": body["tag"]})
        if method == "GET" and path.startswith("/compare/"):
            return self._compare(path[len("/compare/") :])
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
                if e["sha"] is None:
                    # https://docs.github.com/en/rest/git/trees: a null sha
                    # deletes the path from the base tree; one the base does
                    # not hold is an error.
                    if e["path"] not in flat:
                        return Resp(422, {"message": "GitRPC::BadObjectState"})
                    del flat[e["path"]]
                    continue
                assert e["sha"] in self.blobs
                flat[e["path"]] = e["sha"]
            return Resp(201, {"sha": self._new_tree(flat)})
        if (method, path) == ("POST", "/git/commits"):
            for parent in body["parents"]:
                assert parent in self.commits, parent
            sha = self._new_commit(
                body["tree"],
                body["parents"],
                body["message"],
                body.get("author"),
                body.get("committer"),
            )
            return Resp(
                201, {"sha": sha, "html_url": f"https://github.com/{REPO}/commit/{sha}"}
            )
        if method == "PATCH" and path.startswith("/git/refs/heads/"):
            return self._update_ref(path[len("/git/refs/heads/") :], body)
        if path.startswith("/contents"):
            return self._contents(
                method, path[len("/contents") :].lstrip("/"), accept, params, body
            )
        if path.startswith(("/pulls", "/labels", "/issues")):
            return self._collab(method, path, params, body)
        return Resp(404, {"message": "Not Found"})

    def _create_ref(self, body):
        ref, sha = body["ref"], body["sha"]
        assert ref.startswith(("refs/heads/", "refs/tags/")), ref
        kind, _, name = ref.split("/", 2)
        table = self.branches if ref.startswith("refs/heads/") else self.tags
        if name in table:
            return Resp(422, {"message": "Reference already exists"})
        if sha not in self.commits and sha not in self.tag_objects:
            return Resp(422, {"message": "Object does not exist"})
        table[name] = sha
        return Resp(201, {"ref": ref, "object": {"sha": sha}})

    def _compare(self, basehead):
        base, _, head = basehead.partition("...")
        base, head = self._resolve(unquote(base)), self._resolve(unquote(head))
        for sha in (base, head):
            if sha not in self.commits:
                return Resp(404, {"message": f"No commit found for SHA: {sha}"})
        merge_base = self._merge_base(base, head)
        if merge_base is None:
            return Resp(404, {"message": "No common ancestor between the two."})
        if base == head:
            status = "identical"
        elif self._is_ancestor(base, head):
            status = "ahead"
        elif self._is_ancestor(head, base):
            status = "behind"
        else:
            status = "diverged"
        return Resp(200, {"status": status, "merge_base_commit": {"sha": merge_base}})

    def _collab(self, method, path, params, body):
        """Pull requests, reviews, labels and issues."""
        if (method, path) == ("GET", "/pulls"):
            state = params.get("state", "open")
            found = [
                self._pull_json(p, full=False)
                for _, p in sorted(self.pulls.items())
                if state == "all" or p["state"] == state
            ]
            return self._page(path, found, params)
        if (method, path) == ("POST", "/pulls"):
            if body["head"] not in self.branches or body["base"] not in self.branches:
                return Resp(
                    422,
                    {
                        "message": "Validation Failed",
                        "errors": [{"resource": "PullRequest", "code": "invalid"}],
                    },
                )
            if any(
                p["state"] == "open" and p["head"] == body["head"]
                for p in self.pulls.values()
            ):
                return Resp(
                    422,
                    {
                        "message": "Validation Failed",
                        "errors": [
                            {
                                "resource": "PullRequest",
                                "code": "custom",
                                "message": f"A pull request already exists for {body['head']}.",
                            }
                        ],
                    },
                )
            number = self._next_number()
            self.pulls[number] = {
                "number": number,
                "state": "open",
                "merged": False,
                "merged_at": None,
                "merge_commit_sha": None,
                "head": body["head"],
                "base": body["base"],
                "head_sha": None,
                "title": body["title"],
                "body": body["body"],
            }
            return Resp(201, self._pull_json(self.pulls[number]))
        parts = path.split("/")  # ["", "pulls", "7", "merge"]
        if parts[1] == "pulls" and len(parts) >= 3:
            pull = self.pulls.get(int(parts[2]))
            if pull is None:
                return Resp(404, {"message": "Not Found"})
            rest = "/".join(parts[3:])
            if (method, rest) == ("GET", ""):
                return Resp(200, self._pull_json(pull))
            if (method, rest) == ("PUT", "merge"):
                return self._merge(pull, body)
            if (method, rest) == ("GET", "reviews"):
                return self._page(path, self.reviews.get(pull["number"], []), params)
            if (method, rest) == ("POST", "reviews"):
                return self._review(pull, body)
        if method == "GET" and path.startswith("/labels/"):
            label = self.labels.get(unquote(path[len("/labels/") :]).lower())
            return Resp(200, label) if label else Resp(404, {"message": "Not Found"})
        if (method, path) == ("POST", "/labels"):
            if body["name"].lower() in self.labels:
                return Resp(
                    422,
                    {
                        "message": "Validation Failed",
                        "errors": [
                            {
                                "resource": "Label",
                                "code": "already_exists",
                                "field": "name",
                            }
                        ],
                    },
                )
            assert not body["color"].startswith("#")
            self.labels[body["name"].lower()] = dict(body)
            return Resp(201, dict(body))
        if (method, path) == ("POST", "/issues"):
            number = self._next_number()
            self.issues[number] = {
                "title": body["title"],
                "body": body["body"],
                "labels": body.get("labels", []),
                "comments": [],
            }
            return Resp(
                201,
                {
                    "number": number,
                    "html_url": f"https://github.com/{REPO}/issues/{number}",
                },
            )
        if (
            method == "POST"
            and path.startswith("/issues/")
            and path.endswith("/comments")
        ):
            number = int(path.split("/")[2])
            item = self.issues.get(number) or self.pulls.get(number)
            if item is None:
                return Resp(404, {"message": "Not Found"})
            comments = item.setdefault("comments", [])
            comments.append(body["body"])
            return Resp(
                201,
                {
                    "html_url": f"https://github.com/{REPO}/issues/{number}#c{len(comments)}"
                },
            )
        return Resp(404, {"message": "Not Found"})

    def _pull_json(self, pull, *, full=True):
        # The list endpoint has no ``merged`` flag; a single pull request does.
        merged_head = pull["head_sha"] or self.branches[pull["head"]]
        out = {
            "number": pull["number"],
            "state": pull["state"],
            "merged_at": pull["merged_at"],
            "merge_commit_sha": pull["merge_commit_sha"],
            "head": {
                "ref": pull["head"],
                "sha": merged_head,
                "repo": {"full_name": REPO},
            },
            "base": {"ref": pull["base"], "sha": self.branches[pull["base"]]},
            "title": pull["title"],
            "html_url": f"https://github.com/{REPO}/pull/{pull['number']}",
        }
        if full:
            out["merged"] = pull["merged"]
        return out

    def _merge(self, pull, body):
        if pull["state"] != "open":
            return Resp(405, {"message": "Pull Request is not mergeable"})
        head = self.branches[pull["head"]]
        if body.get("sha") and body["sha"] != head:
            # https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request
            return Resp(
                409,
                {
                    "message": "Head branch was modified. Review and try the merge again."
                },
            )
        base = self.branches[pull["base"]]
        flat = dict(self.trees[self.commits[head]["tree"]])
        if not self._is_ancestor(base, head):
            flat = {**self.trees[self.commits[base]["tree"]], **flat}
        message = body.get("commit_title") or f"Merge pull request #{pull['number']}"
        if body.get("commit_message"):
            message += "\n\n" + body["commit_message"]
        sha = self._new_commit(self._new_tree(flat), [base, head], message)
        self.branches[pull["base"]] = sha
        pull.update(
            state="closed",
            merged=True,
            merged_at="2026-10-27T12:00:00Z",
            merge_commit_sha=sha,
            head_sha=head,
            merge_method=body["merge_method"],
        )
        return Resp(
            200,
            {"sha": sha, "merged": True, "message": "Pull Request successfully merged"},
        )

    def _review(self, pull, body):
        assert set(body) <= {"commit_id", "body", "event", "comments"}, body
        if body["event"] == "APPROVE" and self.author_is_reviewer:
            return Resp(
                422,
                {
                    "message": "Unprocessable Entity",
                    "errors": ["Can not approve your own pull request"],
                },
            )
        reviews = self.reviews.setdefault(pull["number"], [])
        state = {
            "COMMENT": "COMMENTED",
            "APPROVE": "APPROVED",
            "REQUEST_CHANGES": "CHANGES_REQUESTED",
        }[body["event"]]
        review = {
            "id": 1000 + len(reviews),
            "body": body["body"],
            "commit_id": body["commit_id"],
            "state": state,
            "submitted_at": "2026-10-27T12:00:00Z",
            "html_url": f"https://github.com/{REPO}/pull/{pull['number']}#pullrequestreview-{1000 + len(reviews)}",
        }
        reviews.append(review)
        return Resp(200, review)

    def _repo_json(self):
        return {
            "default_branch": "main",
            "private": True,
            "permissions": {"push": True},
            **self.settings,
        }

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


def test_has_branch_tells_a_branch_from_a_tag_a_sha_and_a_missing_name(gh, fake):
    assert gh.has_branch("main") is True
    assert gh.has_branch("no-such-branch") is False
    assert gh.has_branch(fake.branches["main"]) is False  # a SHA is not a branch


def test_has_branch_on_an_empty_repository_is_false():
    fake = FakeGitHub(empty=True)
    assert GitHubRepo(REPO, TOKEN, session=fake).has_branch("main") is False


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


# -- the round surface: helpers ----------------------------------------------


def _when(day=14, hour=18, tz=timezone.utc):
    return datetime(2026, 6, day, hour, 2, 11, tzinfo=tz)


def _sig(day=14, hour=18, tz=timezone.utc):
    return Signature("Example Author", "author@example.invalid", _when(day, hour, tz))


def _round_branch(fake, name="round/example-article/r1", files=None):
    """A branch cut from ``main`` with one commit of its own; returns its tip."""
    fake.branches[name] = fake.branches["main"]
    return fake.external_commit(
        name, files or {"articles/example-article/draft.md": b"draft\n"}, "draft"
    )


def _open_pull(gh, fake, name="round/example-article/r1", title="example-article r1"):
    _round_branch(fake, name)
    return gh.create_pull(name, "main", title, "body")


def _posts(fake, part):
    return [r for r in fake.requests if r[0] == "POST" and part in r[1]]


# -- pagination ----------------------------------------------------------------


def test_a_list_is_followed_across_three_pages(gh, fake):
    for n in range(1, 6):
        _open_pull(gh, fake, f"round/example-article/r{n}")
    fake.page_size = 2
    before = len(fake.requests)
    pulls = gh.list_pulls()
    assert [p.number for p in pulls] == [1, 2, 3, 4, 5]
    gets = [
        r for r in fake.requests[before:] if r[0] == "GET" and r[1].endswith("/pulls")
    ]
    assert len(gets) == 3
    assert gets[0][2]["per_page"] == 100 and gets[0][2]["state"] == "open"
    assert [r[2]["page"] for r in gets[1:]] == ["2", "3"]


def test_a_list_that_never_ends_stops_at_the_page_cap(gh, fake, monkeypatch):
    for n in range(1, 5):
        _open_pull(gh, fake, f"round/example-article/r{n}")
    fake.page_size = 1
    monkeypatch.setattr(github_repo, "_MAX_PAGES", 3)
    before = len(fake.requests)
    err = raises("other", gh.list_pulls)
    assert "more than 3 pages" in str(err)
    assert len(fake.requests) - before == 3


def test_a_list_exactly_at_the_page_cap_is_complete(gh, fake, monkeypatch):
    for n in range(1, 4):
        _open_pull(gh, fake, f"round/example-article/r{n}")
    fake.page_size = 1
    monkeypatch.setattr(github_repo, "_MAX_PAGES", 3)
    assert len(gh.list_pulls()) == 3


def test_a_next_link_that_leaves_the_api_host_is_not_followed(gh, fake):
    fake.inject(
        "GET",
        "/pulls",
        Resp(200, [], headers={"Link": '<https://evil.example/x?page=2>; rel="next"'}),
    )
    err = raises("other", gh.list_pulls)
    assert "leaves the API host" in str(err)
    assert fake.count("GET", "/pulls") == 1


def test_list_pulls_filters_by_state_and_rejects_a_bad_one(gh, fake):
    _open_pull(gh, fake, "round/example-article/r1")
    _open_pull(gh, fake, "round/example-article/r2")
    gh.merge_pull(1, sha=fake.branches["round/example-article/r1"])
    assert [p.number for p in gh.list_pulls()] == [2]
    assert [p.number for p in gh.list_pulls("closed")] == [1]
    assert [p.number for p in gh.list_pulls("all")] == [1, 2]
    with pytest.raises(ValueError):
        gh.list_pulls("merged")


# -- pull requests -------------------------------------------------------------


def test_create_pull_then_get_pull_report_every_field(gh, fake):
    tip = _round_branch(fake)
    made = gh.create_pull("round/example-article/r1", "main", "example-article r1", "b")
    got = gh.get_pull(made.number)
    assert got == made
    assert (got.number, got.state, got.merged, got.merged_at) == (
        1,
        "open",
        False,
        None,
    )
    assert got.head_ref == "round/example-article/r1" and got.head_sha == tip
    assert got.head_repo == REPO
    assert got.base_ref == "main" and got.base_sha == fake.branches["main"]
    assert got.title == "example-article r1"
    assert got.html_url == f"https://github.com/{REPO}/pull/1"
    sent = _posts(fake, "/pulls")[0][3]
    assert sent == {
        "head": "round/example-article/r1",
        "base": "main",
        "title": "example-article r1",
        "body": "b",
    }


def test_a_second_pull_request_for_one_branch_says_why(gh, fake):
    _open_pull(gh, fake)
    err = raises("other", gh.create_pull, "round/example-article/r1", "main", "t", "b")
    assert err.status == 422 and "already exists" in str(err)


def test_a_pull_request_that_does_not_exist_is_not_found(gh):
    raises("not_found", gh.get_pull, 99)


def test_a_pull_number_must_be_a_positive_int(gh):
    for bad in (0, -1, "1", 1.5, True, None):
        with pytest.raises(ValueError):
            gh.get_pull(bad)


def test_merge_pull_with_the_right_sha_merges(gh, fake):
    tip = _round_branch(fake)
    old_main = fake.branches["main"]
    gh.create_pull("round/example-article/r1", "main", "example-article r1", "b")
    sha = gh.merge_pull(1, sha=tip, commit_title="Merge example-article r1")
    assert fake.branches["main"] == sha
    assert fake.commits[sha]["parents"] == [old_main, tip]
    assert fake.commits[sha]["message"] == "Merge example-article r1"
    assert "articles/example-article/draft.md" in fake.tree_at("main")
    assert fake.requests[-1][3] == {
        "sha": tip,
        "merge_method": "merge",
        "commit_title": "Merge example-article r1",
    }
    pull = gh.get_pull(1)
    assert pull.state == "closed" and pull.merged is True
    assert pull.merged_at and pull.merge_commit_sha == sha
    # The list entry has no ``merged`` flag; it is derived from ``merged_at``.
    assert gh.list_pulls("closed")[0].merged is True


def test_merge_pull_with_a_stale_sha_is_a_conflict_and_merges_nothing(gh, fake):
    tip = _round_branch(fake)
    gh.create_pull("round/example-article/r1", "main", "t", "b")
    fake.external_commit("round/example-article/r1", {"late.md": b"late"})
    main = fake.branches["main"]
    err = raises("conflict", gh.merge_pull, 1, sha=tip)
    assert err.status == 409 and "Head branch was modified" in str(err)
    assert fake.branches["main"] == main
    assert gh.get_pull(1).state == "open" and not gh.get_pull(1).merged


def test_merge_pull_of_a_closed_pull_request_is_a_conflict(gh, fake):
    tip = _round_branch(fake)
    gh.create_pull("round/example-article/r1", "main", "t", "b")
    gh.merge_pull(1, sha=tip)
    err = raises("conflict", gh.merge_pull, 1, sha=tip)
    assert err.status == 405 and "not mergeable" in str(err)


def test_merge_pull_passes_the_method_and_message_and_rejects_a_bad_method(gh, fake):
    tip = _round_branch(fake)
    gh.create_pull("round/example-article/r1", "main", "t", "b")
    with pytest.raises(ValueError):
        gh.merge_pull(1, sha=tip, merge_method="fast-forward")
    gh.merge_pull(1, sha=tip, merge_method="squash", commit_message="why")
    assert fake.requests[-1][3]["merge_method"] == "squash"
    assert fake.requests[-1][3]["commit_message"] == "why"


def test_merge_pull_that_gets_no_merge_back_is_an_error(gh, fake):
    tip = _round_branch(fake)
    gh.create_pull("round/example-article/r1", "main", "t", "b")
    fake.inject("PUT", "/merge", Resp(200, {"merged": False, "message": "later"}))
    raises("other", gh.merge_pull, 1, sha=tip)


# -- reviews -------------------------------------------------------------------


def test_create_review_sends_exactly_the_given_fields_and_no_comments(gh, fake):
    _open_pull(gh, fake)
    tip = fake.branches["round/example-article/r1"]
    review = gh.create_review(1, commit_id=tip, body="Summary\n- [ ] one")
    sent = _posts(fake, "/reviews")[0][3]
    assert sent == {"commit_id": tip, "body": "Summary\n- [ ] one", "event": "COMMENT"}
    assert "comments" not in sent
    assert review.state == "COMMENTED" and review.commit_id == tip
    assert review.body == "Summary\n- [ ] one" and review.submitted_at
    assert review.html_url.endswith(f"pullrequestreview-{review.id}")


def test_create_review_passes_event_through_as_given(gh, fake):
    _open_pull(gh, fake)
    tip = fake.branches["round/example-article/r1"]
    fake.author_is_reviewer = False
    review = gh.create_review(1, commit_id=tip, body="ok", event="REQUEST_CHANGES")
    assert review.state == "CHANGES_REQUESTED"
    assert _posts(fake, "/reviews")[0][3]["event"] == "REQUEST_CHANGES"


def test_approving_ones_own_pull_request_is_refused_with_githubs_words(gh, fake):
    _open_pull(gh, fake)
    tip = fake.branches["round/example-article/r1"]
    err = raises("other", gh.create_review, 1, commit_id=tip, body="x", event="APPROVE")
    assert err.status == 422
    assert "Can not approve your own pull request" in str(err)
    assert gh.list_reviews(1) == []


def test_a_422_that_echoes_the_token_is_redacted(gh, fake):
    _open_pull(gh, fake)
    fake.inject(
        "POST",
        "/reviews",
        Resp(422, {"message": f"Unprocessable, token={TOKEN}", "errors": [TOKEN]}),
    )
    err = raises("other", gh.create_review, 1, commit_id="a" * 40, body="x")
    assert "[REDACTED]" in str(err)


def test_list_reviews_is_paginated_and_in_order(gh, fake):
    _open_pull(gh, fake)
    tip = fake.branches["round/example-article/r1"]
    for n in range(3):
        gh.create_review(1, commit_id=tip, body=f"run {n}")
    fake.page_size = 1
    reviews = gh.list_reviews(1)
    assert [r.body for r in reviews] == ["run 0", "run 1", "run 2"]
    assert fake.count("GET", "/reviews") == 3


def test_a_review_body_holding_the_token_is_refused_before_sending(gh, fake):
    _open_pull(gh, fake)
    before = len(fake.requests)
    raises("secret", gh.create_review, 1, commit_id="a" * 40, body=f"see {TOKEN}")
    assert len(fake.requests) == before


# -- merge base ----------------------------------------------------------------


def test_merge_base_is_the_commit_a_branch_was_cut_from(gh, fake):
    cut = fake.branches["main"]
    tip = _round_branch(fake)
    moved = fake.external_commit("main", {"later.md": b"later\n"}, "main moves on")
    assert gh.merge_base(moved, tip) == cut
    method, path, params, _ = fake.requests[-1]
    assert (method, path.endswith(f"/compare/{moved}...{tip}")) == ("GET", True)
    assert params == {"per_page": 1}


def test_merge_base_of_a_commit_and_itself_or_its_ancestor(gh, fake):
    main = fake.branches["main"]
    tip = _round_branch(fake)
    assert gh.merge_base(tip, tip) == tip
    assert gh.merge_base(main, tip) == main
    assert gh.merge_base(tip, main) == main


def test_merge_base_takes_branch_and_tag_names(gh, fake):
    cut = fake.branches["main"]
    tip = _round_branch(fake)
    gh.create_tag("example-article/r1", "accepted", tip, tagger=_sig())
    assert gh.merge_base("main", "example-article/r1") == cut
    assert gh.merge_base("round/example-article/r1", "main") == cut


def test_merge_base_of_unrelated_histories_is_none(gh, fake):
    root = fake.external_commit("orphan", {"x.md": b"x\n"}, "another root")
    assert gh.merge_base(fake.branches["main"], root) is None


def test_merge_base_of_a_missing_commit_is_an_error(gh, fake):
    err = raises("not_found", gh.merge_base, fake.branches["main"], "f" * 40)
    assert err.status == 404


def test_merge_base_without_the_field_is_an_error(gh, fake):
    tip = _round_branch(fake)
    fake.inject("GET", "/compare/", Resp(200, {"status": "ahead"}))
    raises("other", gh.merge_base, fake.branches["main"], tip)


# -- refs and tags -------------------------------------------------------------


def test_matching_refs_lists_a_prefix_with_shas_across_pages(gh, fake):
    a = _round_branch(fake, "round/example-article/r1")
    b = _round_branch(fake, "round/example-article/r2")
    _round_branch(fake, "round/other-article/r1")
    fake.page_size = 1
    assert gh.matching_refs("heads/round/example-article/") == [
        ("refs/heads/round/example-article/r1", a),
        ("refs/heads/round/example-article/r2", b),
    ]
    assert gh.matching_refs("tags/example-article/") == []
    assert fake.requests[-1][1].endswith("/git/matching-refs/tags/example-article/")


@pytest.mark.parametrize("prefix", ["", "/tags/x", "tags//x", "../x", "tags\\x"])
def test_matching_refs_rejects_a_malformed_prefix(gh, prefix):
    with pytest.raises(ValueError):
        gh.matching_refs(prefix)


def test_create_ref_makes_a_branch_and_never_moves_an_existing_one(gh, fake):
    main = fake.branches["main"]
    gh.create_ref("refs/heads/round/example-article/r1", main)
    assert fake.branches["round/example-article/r1"] == main
    other = fake.external_commit("main", {"x.md": b"x"})
    err = raises("exists", gh.create_ref, "refs/heads/round/example-article/r1", other)
    assert err.status == 422 and "was not moved" in str(err)
    assert fake.branches["round/example-article/r1"] == main


@pytest.mark.parametrize("ref", ["heads/x", "main", "refs/heads/", "refs/heads/../x"])
def test_create_ref_wants_a_fully_qualified_name(gh, ref):
    with pytest.raises(ValueError):
        gh.create_ref(ref, "a" * 40)


def test_create_ref_on_a_missing_object_is_an_error(gh):
    err = raises("other", gh.create_ref, "refs/heads/x", "f" * 40)
    assert "Object does not exist" in str(err)


def test_create_tag_makes_an_annotated_tag_with_the_given_date(gh, fake):
    tip = fake.branches["main"]
    tagger = _sig(day=16, hour=10)
    tag_sha = gh.create_tag("example-article/r1", "accepted r1", tip, tagger=tagger)
    obj = fake.tag_objects[tag_sha]
    assert obj["tag"] == "example-article/r1" and obj["object"] == tip
    assert obj["type"] == "commit" and obj["message"] == "accepted r1"
    assert obj["tagger"] == {
        "name": "Example Author",
        "email": "author@example.invalid",
        "date": "2026-06-16T10:02:11+00:00",
    }
    assert (
        fake.tags["example-article/r1"] == tag_sha
    )  # the ref points at the tag object
    assert gh.matching_refs("tags/example-article/") == [
        ("refs/tags/example-article/r1", tag_sha)
    ]


def test_create_tag_on_an_existing_tag_raises_exists_and_does_not_move_it(gh, fake):
    tip = fake.branches["main"]
    first = gh.create_tag("example-article/r1", "one", tip, tagger=_sig())
    later = fake.external_commit("main", {"x.md": b"x"})
    objects, posts = len(fake.tag_objects), len(_posts(fake, "/git/tags"))
    err = raises(
        "exists", gh.create_tag, "example-article/r1", "two", later, tagger=_sig()
    )
    assert "not moved" in str(err)
    assert fake.tags["example-article/r1"] == first
    assert len(fake.tag_objects) == objects  # no orphan tag object either
    assert len(_posts(fake, "/git/tags")) == posts


def test_create_tag_that_loses_a_race_on_the_ref_is_still_exists(gh, fake):
    fake.inject("POST", "/git/refs", Resp(422, {"message": "Reference already exists"}))
    raises(
        "exists",
        gh.create_tag,
        "example-article/r1",
        "m",
        fake.branches["main"],
        tagger=_sig(),
    )


def test_create_tag_checks_its_name_and_hides_the_token(gh, fake):
    with pytest.raises(ValueError):
        gh.create_tag("../x", "m", fake.branches["main"], tagger=_sig())
    raises(
        "secret",
        gh.create_tag,
        "t/r1",
        f"m {TOKEN}",
        fake.branches["main"],
        tagger=_sig(),
    )


def test_a_signature_must_be_timezone_aware_and_keeps_its_offset():
    with pytest.raises(ValueError):
        Signature("n", "e@example.invalid", datetime(2026, 6, 14, 18, 0, 0))
    plus5 = Signature(
        "n",
        "e@example.invalid",
        datetime(
            2026, 6, 14, 18, 0, 0, 999, tzinfo=timezone(timedelta(hours=5, minutes=30))
        ),
    )
    assert plus5.to_api()["date"] == "2026-06-14T18:00:00+05:30"


# -- labels, issues, settings --------------------------------------------------


def test_ensure_label_creates_once_then_sends_no_write(gh, fake):
    assert gh.ensure_label("github-documents", "#0e8a16", "Document store") is True
    assert fake.labels["github-documents"]["color"] == "0e8a16"
    writes = len(_posts(fake, "/labels"))
    assert gh.ensure_label("github-documents", "ff0000", "different") is False
    assert len(_posts(fake, "/labels")) == writes == 1
    assert not [r for r in fake.requests if r[0] in ("PATCH", "PUT", "DELETE")]
    assert fake.labels["github-documents"]["color"] == "0e8a16"  # never edited


def test_ensure_label_that_loses_a_race_returns_false(gh, fake):
    fake.inject(
        "POST",
        "/labels",
        Resp(
            422,
            {
                "message": "Validation Failed",
                "errors": [{"resource": "Label", "code": "already_exists"}],
            },
        ),
    )
    assert gh.ensure_label("round: open", "ededed") is False


def test_ensure_label_quotes_a_name_with_spaces_and_colons(gh, fake):
    assert gh.ensure_label("from-review: deferred", "ededed") is True
    assert any(
        r[1].endswith("/labels/from-review%3A%20deferred") for r in fake.requests
    )
    assert gh.ensure_label("from-review: deferred", "ededed") is False


def test_label_exists_only_reads(gh, fake):
    assert gh.label_exists("from-review") is False
    assert _posts(fake, "/labels") == []
    gh.ensure_label("from-review", "ededed")
    assert gh.label_exists("from-review") is True
    assert len(_posts(fake, "/labels")) == 1  # the one ensure_label made


def test_a_label_needs_a_name(gh):
    with pytest.raises(ValueError):
        gh.ensure_label(" ", "ededed")


def test_create_issue_and_comment_on_an_issue_and_a_pull_request(gh, fake):
    number, url = gh.create_issue("Open work", "body", labels=("from-review",))
    assert (number, url) == (1, f"https://github.com/{REPO}/issues/1")
    assert fake.issues[1]["labels"] == ["from-review"]
    assert gh.comment_on_issue(1, "later").startswith(
        f"https://github.com/{REPO}/issues/1"
    )
    pull = _open_pull(gh, fake)
    assert pull.number == 2
    assert gh.comment_on_issue(2, "note on the pull request").endswith("#c1")
    assert fake.pulls[2]["comments"] == ["note on the pull request"]
    raises("not_found", gh.comment_on_issue, 99, "x")


def test_create_issue_without_labels_sends_none(gh, fake):
    gh.create_issue("t", "b")
    assert "labels" not in _posts(fake, "/issues")[0][3]


def test_info_reports_the_merge_settings(gh, fake):
    info = gh.info()
    assert (info.allow_merge_commit, info.allow_squash_merge) == (True, True)
    assert (info.allow_rebase_merge, info.delete_branch_on_merge) == (True, False)


def test_info_says_none_when_github_leaves_the_settings_out(gh, fake):
    fake.settings.clear()
    info = gh.info()
    assert info.allow_merge_commit is None and info.delete_branch_on_merge is None
    assert RepoInfo("main", True, False).allow_squash_merge is None


def test_update_repo_patches_and_returns_the_new_settings(gh, fake):
    info = gh.update_repo(allow_squash_merge=False, delete_branch_on_merge=True)
    assert fake.requests[-1][:2] == ("PATCH", f"/repos/{REPO}")
    assert fake.requests[-1][3] == {
        "allow_squash_merge": False,
        "delete_branch_on_merge": True,
    }
    assert info.allow_squash_merge is False and info.delete_branch_on_merge is True
    assert gh.info() == info
    with pytest.raises(ValueError):
        gh.update_repo()


# -- commit_files: signatures and deletes --------------------------------------


def test_commit_files_without_signatures_sends_none(gh, fake):
    gh.commit_files("main", {"a.md": b"a"}, "m")
    assert set(_posts(fake, "/git/commits")[0][3]) == {"message", "tree", "parents"}
    sha = fake.branches["main"]
    assert (
        fake.commits[sha]["author"] is None and fake.commits[sha]["committer"] is None
    )


def test_commit_files_records_the_author_and_committer_dates(gh, fake):
    when = _sig(day=14, hour=18, tz=timezone(timedelta(hours=-5)))
    result = gh.commit_files(
        "main", {"a.md": b"a"}, "m", author=when, committer=_sig(day=15)
    )
    commit = fake.commits[result.commit_sha]
    assert commit["author"] == {
        "name": "Example Author",
        "email": "author@example.invalid",
        "date": "2026-06-14T18:02:11-05:00",
    }
    assert commit["committer"]["date"] == "2026-06-15T18:02:11+00:00"


def test_a_lone_author_sends_no_committer(gh, fake):
    gh.commit_files("main", {"a.md": b"a"}, "m", author=_sig())
    body = _posts(fake, "/git/commits")[0][3]
    assert "author" in body and "committer" not in body


def test_deletes_with_the_right_blob_remove_the_path_in_one_commit(gh, fake):
    gh.commit_files("main", {"old.md": b"old", "keep.md": b"keep"}, "setup")
    before = fake.branches["main"]
    result = gh.commit_files(
        "main",
        {"new.md": b"new"},
        "swap",
        deletes={"old.md": git_blob_sha(b"old")},
    )
    tree = fake.tree_at("main")
    assert "old.md" not in tree and "keep.md" in tree and "new.md" in tree
    assert result.deleted == ("old.md",) and result.written == ("new.md",)
    assert fake.commits[result.commit_sha]["parents"] == [before]
    entry = [
        e for e in _posts(fake, "/git/trees")[-1][3]["tree"] if e["path"] == "old.md"
    ]
    assert entry == [{"path": "old.md", "mode": "100644", "type": "blob", "sha": None}]


def test_deletes_alone_make_a_commit(gh, fake):
    gh.commit_files("main", {"old.md": b"old"}, "setup")
    result = gh.commit_files(
        "main", {}, "drop", deletes={"old.md": git_blob_sha(b"old")}
    )
    assert result.deleted == ("old.md",) and result.written == ()
    assert "old.md" not in fake.tree_at("main")


def test_deletes_with_a_wrong_blob_raise_exists_and_commit_nothing(gh, fake):
    gh.commit_files("main", {"old.md": b"old"}, "setup")
    head, blobs, commits = fake.branches["main"], len(fake.blobs), len(fake.commits)
    posts = len(_posts(fake, ""))
    err = raises(
        "exists",
        gh.commit_files,
        "main",
        {"new.md": b"new"},
        "swap",
        deletes={"old.md": git_blob_sha(b"someone else's")},
    )
    assert "old.md" in str(err) and "not deleted" in str(err)
    assert fake.branches["main"] == head and len(fake.commits) == commits
    assert len(fake.blobs) == blobs and len(_posts(fake, "")) == posts
    assert "old.md" in fake.tree_at("main") and "new.md" not in fake.tree_at("main")


def test_deleting_a_path_that_is_already_absent_is_a_no_op(gh, fake):
    head, commits = fake.branches["main"], len(fake.commits)
    result = gh.commit_files("main", {}, "m", deletes={"gone.md": "0" * 40})
    assert result.commit_sha == head and result.deleted == () and result.written == ()
    assert len(fake.commits) == commits and fake.branches["main"] == head
    assert not [r for r in fake.requests if r[0] != "GET"]


def test_deletes_are_validated_before_anything_is_sent(gh, fake):
    with pytest.raises(ValueError):
        gh.commit_files("main", {"a.md": b"a"}, "m", deletes={"a.md": "0" * 40})
    with pytest.raises(ValueError):
        gh.commit_files("main", {}, "m", deletes={"a.md": "not-a-sha"})
    with pytest.raises(ValueError):
        gh.commit_files("main", {}, "m", deletes={"../a.md": "0" * 40})
    with pytest.raises(ValueError):
        gh.commit_files("main", {}, "m")
    assert fake.requests == []


def test_a_directory_cannot_be_deleted_as_a_file(gh, fake):
    fake.external_commit("main", {"docs/a.md": b"a"})
    raises("exists", gh.commit_files, "main", {}, "m", deletes={"docs": "0" * 40})


def test_replace_of_a_path_absent_at_the_head_is_written_not_refused(gh, fake):
    # What the docstring says, pinned: ``replace`` guards a file that exists.
    result = gh.commit_files(
        "main", {"gone.md": b"back"}, "m", replace={"gone.md": git_blob_sha(b"was")}
    )
    assert result.written == ("gone.md",)


# -- merge_commit --------------------------------------------------------------


def test_merge_commit_onto_a_branch_that_is_behind(gh, fake):
    main = fake.branches["main"]
    _round_branch(fake)
    fake.external_commit("round/example-article/r1", {"b.md": b"b"}, "second")
    tip = fake.branches["round/example-article/r1"]
    result = gh.merge_commit(
        "main",
        tip,
        "Merge example-article r1",
        author=_sig(day=16),
        committer=_sig(day=16),
    )
    commit = fake.commits[result.commit_sha]
    assert commit["parents"] == [main, tip]
    assert commit["tree"] == fake.commits[tip]["tree"]
    assert fake.branches["main"] == result.commit_sha
    assert commit["message"] == "Merge example-article r1"
    assert commit["author"]["date"] == "2026-06-16T18:02:11+00:00"
    assert commit["committer"]["date"] == "2026-06-16T18:02:11+00:00"
    assert fake.requests[-1][3]["force"] is False
    assert gh.resolve("main") == result.commit_sha


def test_merge_commit_when_identical_makes_no_commit(gh, fake):
    tip = _round_branch(fake)
    fake.branches["main"] = tip
    commits = len(fake.commits)
    result = gh.merge_commit("main", tip, "m")
    assert result.commit_sha == tip and len(fake.commits) == commits
    assert not [r for r in fake.requests if r[0] in ("POST", "PATCH")]


def test_merge_commit_when_diverged_is_a_conflict_and_writes_nothing(gh, fake):
    tip = _round_branch(fake)
    fake.external_commit("main", {"theirs.md": b"theirs"})
    main, commits = fake.branches["main"], len(fake.commits)
    err = raises("conflict", gh.merge_commit, "main", tip, "m")
    assert "diverged" in str(err) and "Nothing was written" in str(err)
    assert fake.branches["main"] == main and len(fake.commits) == commits
    assert not _posts(fake, "/git/commits")


def test_merge_commit_of_something_the_branch_already_contains_is_a_conflict(gh, fake):
    old = fake.branches["main"]
    fake.external_commit("main", {"x.md": b"x"})
    err = raises("conflict", gh.merge_commit, "main", old, "m")
    assert "behind" in str(err)


def test_merge_commit_retries_from_the_new_head_and_then_sees_the_divergence(gh, fake):
    tip = _round_branch(fake)
    moves = []

    def once(f):
        if not moves:
            moves.append(f.external_commit("main", {"theirs.md": b"theirs"}))

    fake.before_ref_update = once
    err = raises("conflict", gh.merge_commit, "main", tip, "m")
    assert "diverged" in str(err)
    assert fake.branches["main"] == moves[0]  # their commit is still the head
    assert fake.count("PATCH", "/git/refs/") == 1


def test_merge_commit_gives_up_after_the_retries(gh, fake):
    tip = _round_branch(fake)
    main = fake.branches["main"]
    # The compare stays "ahead" every time and only the update is refused.
    fake.inject(
        "PATCH",
        "/git/refs/",
        Resp(422, {"message": "Update is not a fast forward"}),
        times=10,
    )
    err = raises("conflict", gh.merge_commit, "main", tip, "m", retries=2)
    assert "kept moving" in str(err) and "Nothing was lost" in str(err)
    assert fake.count("PATCH", "/git/refs/") == 3
    assert fake.branches["main"] == main


def test_merge_commit_resolves_a_branch_name_and_hides_the_token(gh, fake):
    _round_branch(fake)
    result = gh.merge_commit("main", "round/example-article/r1", "m")
    assert (
        fake.commits[result.commit_sha]["parents"][1]
        == fake.branches["round/example-article/r1"]
    )
    raises("secret", gh.merge_commit, "main", fake.branches["main"], f"m {TOKEN}")


def test_merge_commit_on_a_missing_branch_is_not_found(gh, fake):
    raises("not_found", gh.merge_commit, "nope", fake.branches["main"], "m")


# -- the token never leaks from the new calls ----------------------------------


def test_no_new_error_log_or_repr_holds_the_token(gh, fake, caplog):
    caplog.set_level(logging.DEBUG)
    _open_pull(gh, fake)
    tip = fake.branches["round/example-article/r1"]
    echo = Resp(500, {"message": f"upstream said token={TOKEN}", "errors": [TOKEN]})
    errors = []
    calls = [
        ("GET", "/pulls", lambda: gh.list_pulls()),
        ("GET", "/pulls/1", lambda: gh.get_pull(1)),
        ("POST", "/pulls", lambda: gh.create_pull("a", "b", "t", "b")),
        ("PUT", "/merge", lambda: gh.merge_pull(1, sha=tip)),
        ("GET", "/reviews", lambda: gh.list_reviews(1)),
        ("POST", "/reviews", lambda: gh.create_review(1, commit_id=tip, body="x")),
        ("GET", "/matching-refs/", lambda: gh.matching_refs("tags/x/")),
        ("POST", "/git/refs", lambda: gh.create_ref("refs/heads/x", tip)),
        ("POST", "/git/tags", lambda: gh.create_tag("t/r1", "m", tip, tagger=_sig())),
        ("GET", "/labels/", lambda: gh.ensure_label("l", "ededed")),
        ("POST", "/issues", lambda: gh.create_issue("t", "b")),
        ("POST", "/comments", lambda: gh.comment_on_issue(1, "b")),
        ("PATCH", f"/repos/{REPO}", lambda: gh.update_repo(allow_merge_commit=True)),
        ("GET", "/compare/", lambda: gh.merge_commit("main", tip, "m")),
        ("GET", "/compare/", lambda: gh.merge_base("main", tip)),
    ]
    for method, part, call in calls:
        fake.inject(method, part, echo)
        with pytest.raises(GitHubRepoError) as info:
            call()
        errors.append(info.value)
    assert len(errors) == len(calls)
    for err in errors:
        assert "[REDACTED]" in str(err), (err.kind, str(err))
        assert_no_token(str(err), repr(err), err.args, err.__dict__)
    assert_no_token(caplog.text, repr(gh), str(gh))
    # And the token is only ever in the Authorization header.
    assert_no_token(
        *fake.urls, *[r[2] for r in fake.requests], *[r[3] for r in fake.requests]
    )


def test_the_token_is_refused_in_every_text_the_new_calls_send(gh, fake):
    tip = _round_branch(fake)
    gh.create_pull("round/example-article/r1", "main", "t", "b")
    before = len(fake.requests)
    leak = f"x {TOKEN} y"
    for call in (
        lambda: gh.create_pull("a", "b", leak, "b"),
        lambda: gh.create_pull("a", "b", "t", leak),
        lambda: gh.merge_pull(1, sha=tip, commit_message=leak),
        lambda: gh.create_issue(leak, "b"),
        lambda: gh.create_issue("t", leak),
        lambda: gh.comment_on_issue(1, leak),
    ):
        raises("secret", call)
    assert len(fake.requests) == before


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
