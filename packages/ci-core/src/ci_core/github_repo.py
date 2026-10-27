"""A thin GitHub REST client: read files at a ref, commit files, and the pull
request, review, tag, label and issue calls a round of an article needs.

This is the transport for keeping documents in a GitHub repository. It is
deliberately small and has no GitHub library behind it: ``requests`` through
``ci_core.http``'s OS-trust session, so certificates are verified against the
Windows trust store like every other fetch in this repository.

Why REST and not a clone: a clone is shared mutable state on disk, it gets
CRLF-converted under ``core.autocrlf=true``, and concurrent sessions collide on
it. The API moves exact bytes, and the Git Data API gives a *fast-forward-only*
commit, which is the compare-and-swap the document store depends on: two
writers cannot silently overwrite each other, the loser is told and retries on
top of the winner.

What is never done here: a path is deleted only through an explicit
``deletes={path: expected_blob_sha}``, an existing file is only changed through an
explicit ``replace={path: expected_blob_sha}``, and a ref is never updated or
removed except by ``commit_files`` and ``merge_commit``, which only move a branch
forward (``force: false``). Everything else that would overwrite raises
``kind="exists"`` and writes nothing. ``create_ref`` and ``create_tag`` refuse a
name that is taken; they never move it.

The token travels only in the ``Authorization`` header. Error text from GitHub
is passed through ``redact_value`` before it is kept, and nothing here logs a
header, a body or a file's contents.
"""

import base64
import hashlib
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, quote, urlsplit

import requests

from ci_core.http import USER_AGENT, os_trust_session
from ci_core.redact import redact_value, truncate_excerpt

log = logging.getLogger(__name__)

API_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"

#: Per-request timeout in seconds. Blob uploads carry whole files, so this is
#: generous; a real stall is what it is for.
_TIMEOUT = 60

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
# An owner is letters, digits and hyphens; a repository name also allows "_" and
# ".", but "." and ".." are dots in a URL path and not names.
_REPO_NAME = re.compile(r"^[A-Za-z0-9-]+/(?!\.{1,2}$)[A-Za-z0-9_.-]+$")

#: A GET list is fetched ``_PER_PAGE`` at a time (GitHub's maximum) and at most
#: ``_MAX_PAGES`` pages deep; a list longer than that is an error, not a loop.
_PER_PAGE = 100
_MAX_PAGES = 20

_NEXT_LINK = re.compile(r"<([^>]*)>(.*)")

_JSON = "application/vnd.github+json"
_RAW = "application/vnd.github.raw+json"
_SHA_ONLY = "application/vnd.github.sha"

#: ``GitHubRepoError.kind`` values.
KINDS = (
    "auth",
    "not_found",
    "forbidden",
    "rate_limited",
    "conflict",
    "exists",
    "secret",
    "empty",
    "other",
)


class GitHubRepoError(Exception):
    """A GitHub call failed, or a safety rule refused to make it.

    ``kind`` is what a caller branches on; ``status`` is the HTTP status when
    there was one; ``reset_at`` is the epoch second a rate limit lifts, when
    GitHub said. The message never contains the token.
    """

    def __init__(self, kind, message, *, status=None, reset_at=None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.reset_at = reset_at


@dataclass(frozen=True)
class RepoInfo:
    """What ``GET /repos/{owner}/{repo}`` says about the repository."""

    default_branch: str
    private: bool
    can_push: bool
    #: The merge-method switches and branch clean-up as the repository has them.
    #: ``None`` when GitHub did not say (it leaves them out for a token without
    #: push access), which is not the same as ``False``.
    allow_merge_commit: bool | None = None
    allow_squash_merge: bool | None = None
    allow_rebase_merge: bool | None = None
    delete_branch_on_merge: bool | None = None


@dataclass(frozen=True)
class FileAtRef:
    data: bytes
    blob_sha: str
    commit_sha: str


@dataclass(frozen=True)
class Entry:
    name: str
    path: str
    #: "file" or "dir"; GitHub's "symlink" and "submodule" come through as-is.
    type: str
    sha: str


@dataclass(frozen=True)
class CommitResult:
    commit_sha: str
    html_url: str
    #: Paths this call added or replaced.
    written: tuple
    #: Paths already present with identical bytes.
    unchanged: tuple
    #: Paths this call removed (``deletes=``); a path already absent is not listed.
    deleted: tuple = ()


@dataclass(frozen=True)
class Signature:
    """An author or committer for ``POST /git/commits`` and ``POST /git/tags``.

    ``date`` must be timezone-aware. It is sent as ISO 8601 with its offset, to
    the second, so a commit can carry the moment a document really existed.
    """

    name: str
    email: str
    date: datetime

    def __post_init__(self):
        if self.date.tzinfo is None or self.date.utcoffset() is None:
            raise ValueError("a Signature date must be timezone-aware")

    def to_api(self):
        """The ``{name, email, date}`` object GitHub takes."""
        return {
            "name": self.name,
            "email": self.email,
            "date": self.date.replace(microsecond=0).isoformat(),
        }


@dataclass(frozen=True)
class PullInfo:
    """A pull request, from ``GET /pulls/{n}`` or an entry of ``GET /pulls``."""

    number: int
    #: "open" or "closed"; a merged pull request is "closed" with ``merged`` true.
    state: str
    merged: bool
    merged_at: str | None
    #: Set while a pull request is open too (GitHub's test merge), so it proves
    #: a merge only together with ``merged``.
    merge_commit_sha: str | None
    head_ref: str
    head_sha: str
    #: ``owner/name`` of the head repository, so a caller can refuse a fork;
    #: empty when GitHub no longer has it (a deleted fork).
    head_repo: str
    base_ref: str
    base_sha: str
    title: str
    html_url: str


@dataclass(frozen=True)
class ReviewInfo:
    """A pull request review."""

    id: int
    body: str
    #: The commit the review is about, not the pull request's current head.
    commit_id: str
    #: "COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED" or "PENDING".
    state: str
    #: ``None`` for a pending review.
    submitted_at: str | None
    html_url: str


def _pull_info(body):
    head, base = body.get("head") or {}, body.get("base") or {}
    merged_at = body.get("merged_at")
    return PullInfo(
        number=body["number"],
        state=body["state"],
        # ``GET /pulls`` entries carry no ``merged`` flag, only ``merged_at``.
        merged=bool(body.get("merged", merged_at is not None)),
        merged_at=merged_at,
        merge_commit_sha=body.get("merge_commit_sha"),
        head_ref=head.get("ref") or "",
        head_sha=head.get("sha") or "",
        head_repo=(head.get("repo") or {}).get("full_name") or "",
        base_ref=base.get("ref") or "",
        base_sha=base.get("sha") or "",
        title=body.get("title") or "",
        html_url=body.get("html_url") or "",
    )


def _review_info(body):
    return ReviewInfo(
        id=body["id"],
        body=body.get("body") or "",
        commit_id=body.get("commit_id") or "",
        state=body.get("state") or "",
        submitted_at=body.get("submitted_at"),
        html_url=body.get("html_url") or "",
    )


def _repo_info(body):
    def flag(key):
        return bool(body[key]) if body.get(key) is not None else None

    return RepoInfo(
        default_branch=body["default_branch"],
        private=bool(body.get("private")),
        can_push=bool((body.get("permissions") or {}).get("push")),
        allow_merge_commit=flag("allow_merge_commit"),
        allow_squash_merge=flag("allow_squash_merge"),
        allow_rebase_merge=flag("allow_rebase_merge"),
        delete_branch_on_merge=flag("delete_branch_on_merge"),
    )


def _number(value, what="number"):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{what} must be a positive integer, got {value!r}")
    return value


def git_blob_sha(data):
    """The SHA-1 git gives ``data`` as a blob, as ``git hash-object`` prints it."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def resolve_token(environ=os.environ, run=subprocess.run):
    """``GITHUB_TOKEN`` if set and non-empty, else ``gh auth token``, else ``None``.

    A missing, failing or slow ``gh`` is not an error: the caller decides what
    no token means (a public repository reads without one).
    """
    token = (environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        return token
    try:
        proc = run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _quote(path):
    return quote(path, safe="/")


def _check_path(path):
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise ValueError(f"not a repository-relative POSIX path: {path!r}")


def _check_prefix(prefix):
    parts = prefix.rstrip("/").split("/")
    if (
        not prefix
        or prefix.startswith("/")
        or "\\" in prefix
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise ValueError(f"not a ref prefix like 'tags/example-article/': {prefix!r}")


def _as_bytes(value):
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


class GitHubRepo:
    """One repository, reached over the REST API with one token."""

    def __init__(self, repo, token, *, session=None, api_url=API_URL, timeout=_TIMEOUT):
        if not _REPO_NAME.match(repo or ""):
            raise ValueError(f"repository must be 'owner/name', got {repo!r}")
        api_url = api_url.rstrip("/")
        if token and not api_url.startswith("https://"):
            raise ValueError("refusing to send a token over a non-https API URL")
        self.repo = repo
        self.api_url = api_url
        self._token = token
        self._timeout = timeout
        self._session = session
        self._owns_session = session is None

    def __repr__(self):
        return f"GitHubRepo({self.repo!r}, api_url={self.api_url!r})"

    def close(self):
        """Close the session this object created; a session passed in is the caller's."""
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- transport -----------------------------------------------------------

    def _redact(self, text):
        return redact_value(text, self._token)

    def _error(self, kind, message, **kw):
        return GitHubRepoError(kind, self._redact(message), **kw)

    def _request(self, method, path, *, accept=_JSON, params=None, body=None):
        """One API call; returns the response, or raises ``GitHubRepoError``.

        Success is 2xx. Every other status is returned to nobody: callers that
        expect a particular failure (a missing directory, a stale ref) use
        ``_try`` below instead.
        """
        resp = self._send(method, path, accept=accept, params=params, body=body)
        if not 200 <= resp.status_code < 300:
            raise self._http_error(resp, f"{method} {path}")
        return resp

    def _send(self, method, path, *, accept=_JSON, params=None, body=None):
        if self._session is None:
            self._session = os_trust_session()
        headers = {
            "Accept": accept,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": _API_VERSION,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = self._session.request(
                method,
                f"{self.api_url}{path}",
                headers=headers,
                params=params,
                json=body,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise self._error(
                "other", f"{method} {path} failed: {type(exc).__name__}: {exc}"
            ) from exc
        log.debug("github %s %s -> %s", method, path, resp.status_code)
        return resp

    def _message(self, resp):
        """GitHub's own explanation of a failure, redacted and bounded."""
        try:
            body = resp.json()
            text = body.get("message") if isinstance(body, dict) else None
            errors = body.get("errors") if isinstance(body, dict) else None
        except ValueError:
            text, errors = None, None
        if not text:
            text = getattr(resp, "text", "") or ""
        elif isinstance(errors, list):
            # A 422 says only "Validation Failed" in ``message``; the reason is
            # in ``errors`` (strings, or objects with a ``message`` or ``code``).
            more = [
                str(e.get("message") or e.get("code") or "")
                if isinstance(e, dict)
                else str(e)
                for e in errors
            ]
            more = [m for m in more if m]
            if more:
                text = f"{text}: {'; '.join(more)}"
        return truncate_excerpt(self._redact(text), head=400, tail=100)

    def _http_error(self, resp, what):
        status = resp.status_code
        detail = self._message(resp)
        headers = resp.headers or {}
        if status in (403, 429):
            if str(headers.get("x-ratelimit-remaining", "")).strip() == "0":
                reset = headers.get("x-ratelimit-reset")
                reset_at = int(reset) if str(reset or "").isdigit() else None
                return self._error(
                    "rate_limited",
                    f"{what}: GitHub rate limit reached ({detail})",
                    status=status,
                    reset_at=reset_at,
                )
            retry_after = headers.get("retry-after")
            if str(retry_after or "").isdigit():
                # The secondary limit: no remaining count, a wait instead.
                return self._error(
                    "rate_limited",
                    f"{what}: GitHub asked for a pause of {retry_after}s ({detail})",
                    status=status,
                    reset_at=int(time.time()) + int(retry_after),
                )
        if status == 401:
            return self._error(
                "auth",
                f"{what}: GitHub rejected the token ({detail}). It is missing, "
                "expired or revoked.",
                status=status,
            )
        if status == 404:
            return self._error(
                "not_found",
                f"{what}: not found ({detail}). GitHub answers 404 both for "
                "something that does not exist and for a repository this token "
                "cannot see.",
                status=status,
            )
        if status == 403:
            return self._error(
                "forbidden",
                f"{what}: forbidden ({detail}). The token may lack access to "
                "this repository or write permission.",
                status=status,
            )
        return self._error("other", f"{what}: HTTP {status} ({detail})", status=status)

    # -- reads ---------------------------------------------------------------

    def info(self):
        """The repository's default branch, privacy, push access and merge settings."""
        return _repo_info(self._request("GET", f"/repos/{self.repo}").json())

    def resolve(self, ref):
        """A branch, tag or SHA as the commit SHA it names right now."""
        if _FULL_SHA.match(ref):
            return ref
        resp = self._send(
            "GET", f"/repos/{self.repo}/commits/{_quote(ref)}", accept=_SHA_ONLY
        )
        if resp.status_code == 409 and self._is_empty(resp):
            raise self._empty_error()
        # GitHub answers 422 "No commit found for SHA" for an unknown ref here
        # (seen live 2026-10-26), and 404 for a repository it will not show.
        if resp.status_code == 404 or (
            resp.status_code == 422 and "no commit found" in self._message(resp).lower()
        ):
            raise self._error(
                "not_found",
                f"{self.repo}: no commit for {ref!r}. GitHub answers 404 for a "
                "missing ref and for a repository this token cannot see.",
                status=resp.status_code,
            )
        if not 200 <= resp.status_code < 300:
            raise self._http_error(resp, f"resolve {ref!r}")
        sha = (resp.text or "").strip()
        if not _FULL_SHA.match(sha):
            raise self._error(
                "other", f"resolve {ref!r}: unexpected answer {sha[:80]!r}"
            )
        return sha

    def has_branch(self, branch):
        """True if ``branch`` is a branch of the repository (a tag or SHA is not)."""
        resp = self._send("GET", f"/repos/{self.repo}/git/ref/heads/{_quote(branch)}")
        if resp.status_code == 404 or (
            resp.status_code == 409 and self._is_empty(resp)
        ):
            return False
        if not 200 <= resp.status_code < 300:
            raise self._http_error(resp, f"look up branch {branch}")
        return True

    def read(self, path, ref):
        """The bytes of ``path`` as of ``ref``, pinned to the commit ``ref`` names."""
        _check_path(path)
        commit_sha = self.resolve(ref)
        resp = self._send(
            "GET",
            f"/repos/{self.repo}/contents/{_quote(path)}",
            accept=_RAW,
            params={"ref": commit_sha},
        )
        if resp.status_code == 404:
            raise self._error(
                "not_found",
                f"{path} does not exist at {commit_sha[:12]} in {self.repo}",
                status=404,
            )
        if not 200 <= resp.status_code < 300:
            raise self._http_error(resp, f"read {path}")
        content_type = (resp.headers or {}).get("content-type", "")
        if content_type.split(";")[0].strip() == "application/json":
            # A file comes back as raw bytes; a directory comes back as a JSON
            # listing whatever Accept asked for.
            raise self._error("other", f"{path} is a directory, not a file")
        data = resp.content
        return FileAtRef(data=data, blob_sha=git_blob_sha(data), commit_sha=commit_sha)

    def list_dir(self, path, ref):
        """The entries of a directory as of ``ref``; ``[]`` if it does not exist."""
        if path in ("", "/"):
            path = ""
        else:
            _check_path(path)
        # Resolve first so a missing repository or ref is an error, and a 404
        # below can only mean the directory is absent.
        commit_sha = self.resolve(ref)
        resp = self._send(
            "GET",
            f"/repos/{self.repo}/contents/{_quote(path)}".rstrip("/"),
            params={"ref": commit_sha},
        )
        if resp.status_code == 404:
            return []
        if not 200 <= resp.status_code < 300:
            raise self._http_error(resp, f"list {path or '/'}")
        body = resp.json()
        if not isinstance(body, list):
            raise self._error("other", f"{path} is a file, not a directory")
        return [
            Entry(name=e["name"], path=e["path"], type=e["type"], sha=e["sha"])
            for e in body
        ]

    # -- writes --------------------------------------------------------------

    def commit_files(
        self,
        branch,
        files,
        message,
        *,
        replace=None,
        refuse_if_contains=(),
        retries=3,
        author=None,
        committer=None,
        deletes=None,
    ):
        """Add ``files`` (path -> bytes) to ``branch`` in one fast-forward commit.

        A path that is absent is written. A path that exists with identical bytes
        is ``unchanged``. A path that exists with different bytes raises
        ``kind="exists"`` and nothing is written, unless ``replace`` names it
        with the blob SHA it is expected to have: then it is replaced only if
        the file is still that. ``replace`` guards an existing file only: a path
        that is absent at the head is written even when ``replace`` lists it. A
        caller that needs a file to *still be absent* must leave it out of
        ``replace``, so that a file that has appeared since is refused (``exists``).

        ``deletes`` is ``{path: expected_blob_sha}``: a path is removed, in the
        same commit, only when its current blob is the expected one; any other
        blob raises ``kind="exists"`` and nothing is written. A path that is
        already absent is skipped, so ``CommitResult.deleted`` is the record of
        what was removed: read it if the caller needs to know. (GitHub's reads can
        lag a write by a moment; seen once in about 40 live commits, a branch read
        straight after a commit lacked it, so a delete of that very file did
        nothing and ``deleted`` came back empty.) A path may not be in both
        ``files`` and ``deletes``. Nothing is deleted unless it is named here.

        ``author`` and ``committer`` are ``Signature`` objects, sent as given. When
        both are omitted GitHub uses the token's user and the current time, as
        before. To backdate a commit pass both: a lone ``author`` leaves the
        committer date at the present.

        If another writer moves the branch between this call reading it and
        updating it, GitHub refuses the update as not a fast forward and the
        whole call starts again from the new head, up to ``retries`` more times,
        before ``kind="conflict"``.
        """
        replace = dict(replace or {})
        deletes = dict(deletes or {})
        if not files and not deletes:
            raise ValueError("no files to commit")
        for path in files:
            _check_path(path)
        for path, expected in deletes.items():
            _check_path(path)
            if path in files:
                raise ValueError(f"{path} is in both files and deletes")
            if not _FULL_SHA.match(expected or ""):
                raise ValueError(
                    f"deletes[{path!r}] must be the 40-hex blob SHA the file is expected to have"
                )
        self._refuse_secrets(files, message, refuse_if_contains)

        blob_shas = {}  # local blob sha -> sha GitHub holds, valid across retries
        for attempt in range(retries + 1):
            head, tree, head_url = self._head(branch)
            to_write, unchanged = self._classify(files, replace, head)
            to_delete = self._classify_deletes(deletes, head)
            if not to_write and not to_delete:
                return CommitResult(
                    commit_sha=head,
                    html_url=head_url,
                    written=(),
                    unchanged=tuple(unchanged),
                )
            entries = []
            for path in to_write:
                data = files[path]
                local = git_blob_sha(data)
                if local not in blob_shas:
                    blob_shas[local] = self._request(
                        "POST",
                        f"/repos/{self.repo}/git/blobs",
                        body={
                            "content": base64.b64encode(data).decode("ascii"),
                            "encoding": "base64",
                        },
                    ).json()["sha"]
                entries.append(
                    {
                        "path": path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_shas[local],
                    }
                )
            for path in to_delete:
                # A null sha on the base tree removes the path.
                entries.append(
                    {"path": path, "mode": "100644", "type": "blob", "sha": None}
                )
            new_tree = self._request(
                "POST",
                f"/repos/{self.repo}/git/trees",
                body={"base_tree": tree, "tree": entries},
            ).json()["sha"]
            commit = self._request(
                "POST",
                f"/repos/{self.repo}/git/commits",
                body=self._commit_body(
                    message, new_tree, [head], author=author, committer=committer
                ),
            ).json()
            resp = self._send(
                "PATCH",
                f"/repos/{self.repo}/git/refs/heads/{_quote(branch)}",
                body={"sha": commit["sha"], "force": False},
            )
            if 200 <= resp.status_code < 300:
                return CommitResult(
                    commit_sha=commit["sha"],
                    html_url=commit.get("html_url", ""),
                    written=tuple(to_write),
                    unchanged=tuple(unchanged),
                    deleted=tuple(to_delete),
                )
            if self._not_fast_forward(resp):
                log.info(
                    "github %s: %s moved during commit (attempt %d of %d)",
                    self.repo,
                    branch,
                    attempt + 1,
                    retries + 1,
                )
                continue
            raise self._http_error(resp, f"update {branch}")
        raise self._error(
            "conflict",
            f"{self.repo}: {branch} kept moving; gave up after {retries + 1} "
            "attempts. Nothing was lost; run it again.",
            status=422,
        )

    def merge_commit(
        self,
        branch,
        other_sha,
        message,
        *,
        author=None,
        committer=None,
        retries=3,
    ):
        """Merge ``other_sha`` into ``branch`` as a merge commit built by hand.

        For an import that wants a round's history on ``main`` without a merge
        through the pull request button. The new commit's tree is ``other_sha``'s
        tree and its parents are ``[head of branch, other_sha]``, and ``branch``
        is moved to it with ``force: false``. No content merge is done, so it is
        only correct when ``other_sha`` already contains everything on the
        branch, and that is checked: ``GET /compare/{head}...{other_sha}`` must
        say ``ahead``. ``identical`` returns the head without a commit. Anything
        else (``behind``, ``diverged``, no common ancestor) raises
        ``kind="conflict"`` and nothing is written.

        A branch that moves between the check and the update is retried from the
        new head, up to ``retries`` more times, before ``kind="conflict"``.
        ``author`` and ``committer`` are as for ``commit_files``.
        """
        self._refuse_token(commit_message=message)
        other = self.resolve(other_sha)
        for attempt in range(retries + 1):
            head, _, head_url = self._head(branch)
            status = self._compare_status(head, other)
            if status == "identical":
                return CommitResult(
                    commit_sha=head, html_url=head_url, written=(), unchanged=()
                )
            if status != "ahead":
                raise self._error(
                    "conflict",
                    f"{self.repo}: cannot merge {other[:12]} into {branch}: "
                    f"{branch} is {status} it, and merge_commit only merges a "
                    "commit that is ahead of the branch. Nothing was written.",
                    status=409,
                )
            other_tree = self._request(
                "GET", f"/repos/{self.repo}/git/commits/{other}"
            ).json()["tree"]["sha"]
            commit = self._request(
                "POST",
                f"/repos/{self.repo}/git/commits",
                body=self._commit_body(
                    message,
                    other_tree,
                    [head, other],
                    author=author,
                    committer=committer,
                ),
            ).json()
            resp = self._send(
                "PATCH",
                f"/repos/{self.repo}/git/refs/heads/{_quote(branch)}",
                body={"sha": commit["sha"], "force": False},
            )
            if 200 <= resp.status_code < 300:
                return CommitResult(
                    commit_sha=commit["sha"],
                    html_url=commit.get("html_url", ""),
                    written=(),
                    unchanged=(),
                )
            if self._not_fast_forward(resp):
                log.info(
                    "github %s: %s moved during merge (attempt %d of %d)",
                    self.repo,
                    branch,
                    attempt + 1,
                    retries + 1,
                )
                continue
            raise self._http_error(resp, f"update {branch}")
        raise self._error(
            "conflict",
            f"{self.repo}: {branch} kept moving; gave up after {retries + 1} "
            "attempts. Nothing was lost; run it again.",
            status=422,
        )

    # -- pull requests -------------------------------------------------------

    def get_pull(self, number):
        """One pull request as it is now."""
        _number(number, "pull request number")
        body = self._request("GET", f"/repos/{self.repo}/pulls/{number}").json()
        return _pull_info(body)

    def list_pulls(self, state="open"):
        """Every pull request in ``state`` ("open", "closed" or "all"), all pages.

        The REST ``head`` filter wants an exact ``user:ref``, so a caller that
        wants the pull requests of a branch family filters ``head_ref`` itself.
        Entries of the list carry no ``merged`` flag; it is derived from
        ``merged_at``.
        """
        if state not in ("open", "closed", "all"):
            raise ValueError(f"state must be open, closed or all, got {state!r}")
        items = self._get_all(
            f"/repos/{self.repo}/pulls",
            params={"state": state},
            what="list pull requests",
        )
        return [_pull_info(b) for b in items]

    def create_pull(self, head, base, title, body):
        """Open a pull request from branch ``head`` into ``base``."""
        self._refuse_token(title=title, body=body)
        resp = self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            body={"head": head, "base": base, "title": title, "body": body},
        )
        return _pull_info(resp.json())

    def merge_pull(
        self,
        number,
        *,
        sha,
        merge_method="merge",
        commit_title=None,
        commit_message=None,
    ):
        """Merge pull request ``number``, only if its head is still ``sha``.

        Returns the merge commit's SHA. GitHub answers 409 when the head has
        moved past ``sha`` and 405 when the pull request cannot be merged (also
        seen for a stale ``sha`` within a second of the push that moved the head,
        while GitHub still works out mergeability); both raise ``kind="conflict"``
        with GitHub's message, and nothing is merged.
        """
        _number(number, "pull request number")
        if merge_method not in ("merge", "squash", "rebase"):
            raise ValueError(
                f"merge_method must be merge, squash or rebase: {merge_method!r}"
            )
        body = {"sha": sha, "merge_method": merge_method}
        if commit_title is not None:
            body["commit_title"] = commit_title
        if commit_message is not None:
            body["commit_message"] = commit_message
        self._refuse_token(
            commit_title=commit_title or "", commit_message=commit_message or ""
        )
        resp = self._send("PUT", f"/repos/{self.repo}/pulls/{number}/merge", body=body)
        if resp.status_code in (405, 409):
            raise self._error(
                "conflict",
                f"pull request {number} was not merged: {self._message(resp)}",
                status=resp.status_code,
            )
        if not 200 <= resp.status_code < 300:
            raise self._http_error(resp, f"merge pull request {number}")
        data = resp.json()
        if not data.get("merged") or not _FULL_SHA.match(str(data.get("sha", ""))):
            raise self._error(
                "other",
                f"merge pull request {number}: GitHub answered without a merge "
                f"({self._redact(str(data.get('message', '')))[:200]})",
            )
        return data["sha"]

    # -- reviews -------------------------------------------------------------

    def list_reviews(self, number):
        """Every review of a pull request, oldest first, all pages."""
        _number(number, "pull request number")
        items = self._get_all(
            f"/repos/{self.repo}/pulls/{number}/reviews", what="list reviews"
        )
        return [_review_info(b) for b in items]

    def create_review(self, number, *, commit_id, body, event="COMMENT"):
        """Post a review of ``commit_id`` with a ``body`` and no line comments.

        ``event`` is passed through as given ("COMMENT", "APPROVE" or
        "REQUEST_CHANGES"). GitHub refuses some combinations with a 422, for
        example ``APPROVE`` on one's own pull request, and that raises
        ``kind="other"`` with GitHub's message.
        """
        _number(number, "pull request number")
        self._refuse_token(review_body=body)
        resp = self._request(
            "POST",
            f"/repos/{self.repo}/pulls/{number}/reviews",
            body={"commit_id": commit_id, "body": body, "event": event},
        )
        return _review_info(resp.json())

    # -- refs and tags -------------------------------------------------------

    def matching_refs(self, prefix):
        """``[(ref, sha), ...]`` for every ref under ``prefix``, all pages.

        ``prefix`` is without ``refs/``: ``"heads/round/example-article/"`` or
        ``"tags/example-article/"``. For an annotated tag ``sha`` is the tag
        object's, not the commit's.
        """
        _check_prefix(prefix)
        items = self._get_all(
            f"/repos/{self.repo}/git/matching-refs/{_quote(prefix)}",
            what=f"list refs under {prefix}",
        )
        return [(r["ref"], r["object"]["sha"]) for r in items]

    def create_ref(self, ref, sha):
        """Create ``ref`` (fully qualified, ``refs/heads/...``) at ``sha``.

        A name that is taken raises ``kind="exists"`` and is not moved.
        """
        if not ref.startswith("refs/") or ref.endswith("/"):
            raise ValueError(
                f"ref must be fully qualified (refs/heads/...), got {ref!r}"
            )
        _check_path(ref)
        resp = self._send(
            "POST", f"/repos/{self.repo}/git/refs", body={"ref": ref, "sha": sha}
        )
        if resp.status_code == 422 and "already exists" in self._message(resp).lower():
            raise self._error(
                "exists",
                f"{ref} already exists in {self.repo}; it was not moved",
                status=422,
            )
        if not 200 <= resp.status_code < 300:
            raise self._http_error(resp, f"create {ref}")

    def create_tag(self, name, message, object_sha, *, tagger):
        """Create the annotated tag ``name`` on commit ``object_sha``.

        Makes the tag object (``POST /git/tags``, type commit, with ``tagger`` a
        ``Signature`` whose date is the tag's date) and then the ref
        ``refs/tags/<name>``; returns the tag object's SHA. An existing tag
        raises ``kind="exists"``, is checked before anything is created, and is
        never moved.
        """
        _check_path(name)
        self._refuse_token(tag_message=message)
        if self._ref_sha(f"tags/{name}") is not None:
            raise self._error(
                "exists", f"tag {name} already exists in {self.repo}; it was not moved"
            )
        tag = self._request(
            "POST",
            f"/repos/{self.repo}/git/tags",
            body={
                "tag": name,
                "message": message,
                "object": object_sha,
                "type": "commit",
                "tagger": tagger.to_api(),
            },
        ).json()
        self.create_ref(f"refs/tags/{name}", tag["sha"])
        return tag["sha"]

    # -- labels and issues ---------------------------------------------------

    def ensure_label(self, name, color, description=""):
        """Create the label ``name`` if it is missing; True when this call made it.

        An existing label is never edited, whatever its colour or description.
        """
        if not name or not name.strip():
            raise ValueError("a label needs a name")
        found = self._send("GET", f"/repos/{self.repo}/labels/{quote(name, safe='')}")
        if 200 <= found.status_code < 300:
            return False
        if found.status_code != 404:
            raise self._http_error(found, f"look up label {name}")
        made = self._send(
            "POST",
            f"/repos/{self.repo}/labels",
            body={
                "name": name,
                "color": color.lstrip("#"),
                "description": description,
            },
        )
        if 200 <= made.status_code < 300:
            return True
        if made.status_code == 422 and "already_exists" in (made.text or ""):
            return False  # created by someone between the look-up and the create
        raise self._http_error(made, f"create label {name}")

    def create_issue(self, title, body, labels=()):
        """Open an issue; returns ``(number, html_url)``."""
        self._refuse_token(title=title, body=body)
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        data = self._request("POST", f"/repos/{self.repo}/issues", body=payload).json()
        return data["number"], data["html_url"]

    def comment_on_issue(self, number, body):
        """Comment on an issue or a pull request; returns the comment's URL."""
        _number(number, "issue number")
        self._refuse_token(comment=body)
        data = self._request(
            "POST", f"/repos/{self.repo}/issues/{number}/comments", body={"body": body}
        ).json()
        return data["html_url"]

    # -- repository settings -------------------------------------------------

    def update_repo(self, **fields):
        """``PATCH /repos/{owner}/{repo}`` with ``fields``; returns the new ``RepoInfo``.

        For the merge-method switches and ``delete_branch_on_merge``, for example
        ``update_repo(allow_squash_merge=False)``.
        """
        if not fields:
            raise ValueError("no fields to update")
        body = self._request("PATCH", f"/repos/{self.repo}", body=fields).json()
        return _repo_info(body)

    def seed_empty(self, path, data, message, branch="main"):
        """Make the first commit of an empty repository; returns its SHA.

        The Git Data API answers 409 until a repository has a commit, so the
        first one goes through the Contents API, which creates ``branch``.
        """
        _check_path(path)
        self._refuse_secrets({path: data}, message, ())
        probe = self._send("GET", f"/repos/{self.repo}/commits", params={"per_page": 1})
        if probe.status_code == 200:
            raise self._error(
                "exists",
                f"{self.repo} already has commits; use commit_files, not seed_empty",
            )
        if not (probe.status_code == 409 and self._is_empty(probe)):
            raise self._http_error(probe, "check that the repository is empty")
        resp = self._request(
            "PUT",
            f"/repos/{self.repo}/contents/{_quote(path)}",
            body={
                "message": message,
                "content": base64.b64encode(data).decode("ascii"),
                "branch": branch,
            },
        )
        return resp.json()["commit"]["sha"]

    # -- commit_files helpers ------------------------------------------------

    def _refuse_secrets(self, files, message, refuse_if_contains):
        needles = [_as_bytes(v) for v in refuse_if_contains if v]
        if self._token:
            needles.append(self._token.encode("utf-8"))
        if not needles:
            return
        scanned = dict(files)
        scanned["the commit message"] = _as_bytes(message)
        for path, data in scanned.items():
            if any(n in data for n in needles):
                raise self._error(
                    "secret",
                    f"{path} contains a value that must not be committed (the "
                    "token or a refuse_if_contains value); nothing was sent",
                )

    def _head(self, branch):
        """(commit sha, tree sha, commit page URL) of the branch's head."""
        resp = self._send("GET", f"/repos/{self.repo}/git/ref/heads/{_quote(branch)}")
        if resp.status_code == 409 and self._is_empty(resp):
            raise self._empty_error()
        if resp.status_code == 404:
            raise self._error(
                "not_found",
                f"{self.repo} has no branch {branch!r}. GitHub answers 404 for a "
                "missing branch and for a repository this token cannot see.",
                status=404,
            )
        if not 200 <= resp.status_code < 300:
            raise self._http_error(resp, f"read branch {branch}")
        head = resp.json()["object"]["sha"]
        commit = self._request("GET", f"/repos/{self.repo}/git/commits/{head}").json()
        return head, commit["tree"]["sha"], commit.get("html_url", "")

    def _classify(self, files, replace, head):
        """Split ``files`` into what to write and what is already there.

        Raises ``exists`` on the first path that would overwrite something it
        may not, before anything is created.
        """
        to_write, unchanged = [], []
        for path, data in files.items():
            current = self._blob_at(path, head)
            if current is None:
                to_write.append(path)
            elif current == git_blob_sha(data):
                unchanged.append(path)
            elif path in replace and replace[path] == current:
                to_write.append(path)
            elif path in replace:
                raise self._error(
                    "exists",
                    f"{path} was changed by someone else (expected blob "
                    f"{replace[path][:12]}, found {current[:12]}); nothing was written",
                )
            else:
                raise self._error(
                    "exists",
                    f"{path} already exists with different content; nothing was written",
                )
        return to_write, unchanged

    def _classify_deletes(self, deletes, head):
        """The paths of ``deletes`` that exist at ``head`` with the expected blob.

        A path already absent is skipped. One that exists with another blob
        raises ``exists`` before anything is created.
        """
        to_delete = []
        for path, expected in deletes.items():
            current = self._blob_at(path, head)
            if current is None:
                continue
            if current != expected:
                raise self._error(
                    "exists",
                    f"{path} was changed by someone else (expected blob "
                    f"{expected[:12]}, found {current[:12]}); it was not deleted "
                    "and nothing was written",
                )
            to_delete.append(path)
        return to_delete

    @staticmethod
    def _commit_body(message, tree, parents, *, author, committer):
        body = {"message": message, "tree": tree, "parents": list(parents)}
        if author is not None:
            body["author"] = author.to_api()
        if committer is not None:
            body["committer"] = committer.to_api()
        return body

    def _compare_status(self, base, head):
        """GitHub's ``status`` for ``head`` against ``base``: ahead, behind, ..."""
        resp = self._send(
            "GET",
            f"/repos/{self.repo}/compare/{base}...{head}",
            params={"per_page": 1},
        )
        if (
            resp.status_code == 404
            and "no common ancestor" in self._message(resp).lower()
        ):
            return "unrelated to"
        if not 200 <= resp.status_code < 300:
            raise self._http_error(resp, f"compare {base[:12]}...{head[:12]}")
        return str(resp.json().get("status", "unknown"))

    def _ref_sha(self, ref):
        """The SHA ``ref`` ("tags/x", "heads/y") points at, or None if it is absent."""
        resp = self._send("GET", f"/repos/{self.repo}/git/ref/{_quote(ref)}")
        if resp.status_code == 404:
            return None
        if not 200 <= resp.status_code < 300:
            raise self._http_error(resp, f"look up {ref}")
        body = resp.json()
        return body["object"]["sha"] if isinstance(body, dict) else None

    def _refuse_token(self, **texts):
        """Refuse to send the token inside text that is going to GitHub's database."""
        if not self._token:
            return
        needle = self._token.encode("utf-8")
        for what, text in texts.items():
            if needle in _as_bytes(text):
                raise self._error(
                    "secret", f"the {what} contains the token; nothing was sent"
                )

    def _get_all(self, path, *, params=None, what):
        """Every item of a GET list, following ``Link: rel="next"``.

        Fetches ``_PER_PAGE`` at a time and gives up with ``kind="other"`` after
        ``_MAX_PAGES`` pages rather than looping. A next link that leaves the API
        host is not followed: the token would go with it.
        """
        params = dict(params or {})
        params.setdefault("per_page", _PER_PAGE)
        items = []
        for _ in range(_MAX_PAGES):
            resp = self._request("GET", path, params=params)
            page = resp.json()
            if not isinstance(page, list):
                raise self._error(
                    "other", f"{what}: expected a list, got {type(page).__name__}"
                )
            items.extend(page)
            nxt = self._next_link(resp)
            if nxt is None:
                return items
            path, params = nxt
        raise self._error(
            "other",
            f"{what}: more than {_MAX_PAGES} pages of {_PER_PAGE}; stopped rather than loop",
        )

    def _next_link(self, resp):
        """``(path, params)`` of the ``rel="next"`` page, or None on the last."""
        header = (resp.headers or {}).get("link") or ""
        for part in header.split(","):
            m = _NEXT_LINK.match(part.strip())
            if not m or 'rel="next"' not in m.group(2):
                continue
            url = m.group(1)
            if not url.startswith(self.api_url + "/"):
                raise self._error(
                    "other",
                    "GitHub's next-page link leaves the API host; not following it",
                )
            split = urlsplit(url[len(self.api_url) :])
            return split.path, dict(parse_qsl(split.query))
        return None

    def _blob_at(self, path, commit_sha):
        """The blob SHA of ``path`` at ``commit_sha``, or None if it is absent."""
        resp = self._send(
            "GET",
            f"/repos/{self.repo}/contents/{_quote(path)}",
            params={"ref": commit_sha},
        )
        if resp.status_code == 404:
            return None
        if not 200 <= resp.status_code < 300:
            raise self._http_error(resp, f"look up {path}")
        body = resp.json()
        if isinstance(body, list):
            raise self._error("exists", f"{path} is a directory; nothing was written")
        return body["sha"]

    @staticmethod
    def _not_fast_forward(resp):
        if resp.status_code == 409:
            return True
        if resp.status_code != 422:
            return False
        try:
            text = str(resp.json().get("message", ""))
        except (ValueError, AttributeError):
            text = getattr(resp, "text", "") or ""
        return "fast forward" in text.lower() or "fast-forward" in text.lower()

    @staticmethod
    def _is_empty(resp):
        try:
            text = str(resp.json().get("message", ""))
        except (ValueError, AttributeError):
            text = getattr(resp, "text", "") or ""
        return "empty" in text.lower()

    def _empty_error(self):
        return self._error(
            "empty",
            f"{self.repo} is empty, and the Git Data API answers 409 until it "
            "has a first commit. Make that commit with seed_empty().",
            status=409,
        )
