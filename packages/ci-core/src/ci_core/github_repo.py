"""A thin GitHub REST client: read files at a ref, list a directory, commit files.

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

What is never done here: a path is never deleted, and an existing file is only
changed through an explicit ``replace={path: expected_blob_sha}``. Everything
else that would overwrite raises ``kind="exists"`` and writes nothing.

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
from urllib.parse import quote

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
    default_branch: str
    private: bool
    can_push: bool


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
        except ValueError:
            text = None
        if not text:
            text = getattr(resp, "text", "") or ""
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
        body = self._request("GET", f"/repos/{self.repo}").json()
        return RepoInfo(
            default_branch=body["default_branch"],
            private=bool(body.get("private")),
            can_push=bool((body.get("permissions") or {}).get("push")),
        )

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
    ):
        """Add ``files`` (path -> bytes) to ``branch`` in one fast-forward commit.

        A path that is absent is written. A path that exists with identical bytes
        is ``unchanged``. A path that exists with different bytes raises
        ``kind="exists"`` and nothing is written, unless ``replace`` names it
        with the blob SHA it is expected to have: then it is replaced only if
        the file is still that. Nothing is ever deleted.

        If another writer moves the branch between this call reading it and
        updating it, GitHub refuses the update as not a fast forward and the
        whole call starts again from the new head, up to ``retries`` more times,
        before ``kind="conflict"``.
        """
        replace = dict(replace or {})
        if not files:
            raise ValueError("no files to commit")
        for path in files:
            _check_path(path)
        self._refuse_secrets(files, message, refuse_if_contains)

        blob_shas = {}  # local blob sha -> sha GitHub holds, valid across retries
        for attempt in range(retries + 1):
            head, tree, head_url = self._head(branch)
            to_write, unchanged = self._classify(files, replace, head)
            if not to_write:
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
            new_tree = self._request(
                "POST",
                f"/repos/{self.repo}/git/trees",
                body={"base_tree": tree, "tree": entries},
            ).json()["sha"]
            commit = self._request(
                "POST",
                f"/repos/{self.repo}/git/commits",
                body={"message": message, "tree": new_tree, "parents": [head]},
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
