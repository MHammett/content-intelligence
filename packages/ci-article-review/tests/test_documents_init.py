"""``ci-docs init``: the document repository's bootstrap, against an in-process GitHub.

``FakeGitHub`` is the one in ``packages/ci-core/tests/test_github_repo.py``, cut
down to what the bootstrap touches and extended with what that file does not
have: a repository that does not exist (404 on every route), a public one, one
the token cannot push to, and a ``gh repo create`` that makes the missing one
appear, empty. An empty repository answers the Git Data API with 409 until it has
a commit, as the real one does
(https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-your-git-database).

Nothing here reaches the network or runs the real ``gh``: the session and the
subprocess runner are both passed in.
"""

import base64
import hashlib
import json
from pathlib import Path

import pytest

from ci_article_review import documents_cli, documents_init as di
from ci_core.github_repo import git_blob_sha

REPO = "example-owner/example-articles"
TOKEN = "ghp_exampleSECRETtoken0123456789"
HEAD = "a" * 40
SLUG = "example-article"

ROOT = Path(di.__file__).parent


class Resp:
    def __init__(self, status=200, body=None, *, content=None, ctype=None):
        self.status_code = status
        self._body = body
        self.content = (
            content
            if content is not None
            else (json.dumps(body).encode() if body is not None else b"")
        )
        self.text = self.content.decode("utf-8", "replace")
        self.headers = {
            "content-type": ctype
            or ("application/json" if body is not None else "text/plain")
        }

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


def _sha(*parts):
    return hashlib.sha1("\0".join(str(p) for p in parts).encode()).hexdigest()


class FakeGitHub:
    """``REPO`` behind the REST routes the client uses."""

    def __init__(self, *, exists=True, empty=False, private=True, push=True):
        self.exists = exists
        self.private = private
        self.push = push
        self.requests = []
        self.blobs, self.trees, self.commits, self.branches = {}, {}, {}, {}
        self._n = 0
        if exists and not empty:
            self.external_commit("main", {"README.md": b"# readme\n"}, "initial")

    # -- test helpers --------------------------------------------------------

    def external_commit(self, branch, files, message="by someone else"):
        parent = self.branches.get(branch)
        flat = dict(self.trees[self.commits[parent]["tree"]]) if parent else {}
        for path, data in files.items():
            self.blobs[git_blob_sha(data)] = data
            flat[path] = git_blob_sha(data)
        self._n += 1
        tree = _sha("tree", sorted(flat.items()))
        self.trees[tree] = dict(flat)
        sha = _sha("commit", tree, parent, message, self._n)
        self.commits[sha] = {
            "tree": tree,
            "parents": [parent] if parent else [],
            "message": message,
        }
        self.branches[branch] = sha
        return sha

    def tree(self, branch="main"):
        return self.trees[self.commits[self.branches[branch]]["tree"]]

    def file(self, path, branch="main"):
        return self.blobs[self.tree(branch)[path]]

    def messages(self):
        return [c["message"] for c in self.commits.values()]

    @property
    def empty(self):
        return not self.commits

    @property
    def writes(self):
        return [r for r in self.requests if r[0] in ("POST", "PUT", "PATCH")]

    def head(self, branch="main"):
        return self.branches.get(branch)

    # -- the session interface -----------------------------------------------

    def close(self):
        pass

    def request(self, method, url, headers=None, params=None, json=None, **kw):
        assert url.startswith("https://api.github.com"), url
        path = url[len("https://api.github.com") :]
        self.requests.append((method, path, params, json))
        if (headers or {}).get("Authorization") != f"Bearer {TOKEN}":
            return Resp(401, {"message": "Bad credentials"})
        base = f"/repos/{REPO}"
        if not self.exists or not (path == base or path.startswith(base + "/")):
            return Resp(404, {"message": "Not Found"})
        return self._route(method, path[len(base) :], headers or {}, params or {}, json)

    def _route(self, method, path, headers, params, body):
        accept = headers.get("Accept", "")
        if (method, path) == ("GET", ""):
            return Resp(
                200,
                {
                    "default_branch": "main",
                    "private": self.private,
                    "permissions": {"push": self.push},
                },
            )
        if self.empty and path.startswith(("/git/", "/commits")):
            return Resp(409, {"message": "Git Repository is empty."})
        if method == "GET" and path == "/commits":
            return Resp(200, [{"sha": s} for s in list(self.commits)[:1]])
        if method == "GET" and path.startswith("/commits/"):
            ref = path[len("/commits/") :]
            sha = ref if ref in self.commits else self.branches.get(ref)
            if sha is None:
                return Resp(422, {"message": "No commit found for SHA: " + ref})
            return Resp(200, content=sha.encode(), ctype="text/plain")
        if method == "GET" and path.startswith("/git/ref/heads/"):
            sha = self.branches.get(path[len("/git/ref/heads/") :])
            if sha is None:
                return Resp(404, {"message": "Not Found"})
            return Resp(200, {"object": {"sha": sha}})
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
            data = base64.b64decode(body["content"])
            self.blobs[git_blob_sha(data)] = data
            return Resp(201, {"sha": git_blob_sha(data)})
        if (method, path) == ("POST", "/git/trees"):
            flat = dict(self.trees[body["base_tree"]])
            for e in body["tree"]:
                flat[e["path"]] = e["sha"]
            self._n += 1
            sha = _sha("tree", sorted(flat.items()))
            self.trees[sha] = flat
            return Resp(201, {"sha": sha})
        if (method, path) == ("POST", "/git/commits"):
            self._n += 1
            sha = _sha("commit", body["tree"], body["parents"], self._n)
            self.commits[sha] = {
                "tree": body["tree"],
                "parents": body["parents"],
                "message": body["message"],
            }
            return Resp(
                201, {"sha": sha, "html_url": f"https://github.com/{REPO}/commit/{sha}"}
            )
        if method == "PATCH" and path.startswith("/git/refs/heads/"):
            self.branches[path[len("/git/refs/heads/") :]] = body["sha"]
            return Resp(200, {"object": {"sha": body["sha"]}})
        if path.startswith("/contents"):
            return self._contents(
                method, path[len("/contents") :].lstrip("/"), accept, params, body
            )
        return Resp(404, {"message": "Not Found"})

    def _contents(self, method, path, accept, params, body):
        if method == "PUT":
            assert self.empty, "the bootstrap seeds only an empty repository"
            data = base64.b64decode(body["content"])
            sha = self.external_commit(body["branch"], {path: data}, body["message"])
            return Resp(201, {"commit": {"sha": sha}})
        ref = params.get("ref", "main")
        sha = ref if ref in self.commits else self.branches.get(ref)
        flat = self.trees[self.commits[sha]["tree"]]
        if path in flat:
            data = self.blobs[flat[path]]
            if "raw" in accept:
                return Resp(200, content=data, ctype="application/vnd.github.raw+json")
            return Resp(200, {"type": "file", "path": path, "sha": flat[path]})
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


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeRun:
    """``subprocess.run`` for ``git`` and ``gh``; records every argv."""

    def __init__(self, fake, *, logged_in=True, head=HEAD, dirty=""):
        self.fake, self.logged_in, self.head, self.dirty = fake, logged_in, head, dirty
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:2] == ["git", "rev-parse"]:
            return Done(0, self.head + "\n") if self.head else Done(128)
        if argv[:2] == ["git", "status"]:
            return Done(0, self.dirty)
        if argv[:3] == ["gh", "auth", "status"]:
            return Done(0 if self.logged_in else 1)
        if argv[:3] == ["gh", "repo", "create"]:
            self.fake.exists = True  # created, and empty: no commit yet
            return Done(0, f"https://github.com/{REPO}\n")
        raise AssertionError(f"unexpected command {argv}")

    def gh_calls(self):
        return [c for c in self.calls if c[0] == "gh"]


@pytest.fixture
def seed_file(tmp_path):
    path = tmp_path / "publication-instructions.md"
    path.write_bytes(b"# Example publication\r\n\r\nVoice: plain.\r\n")
    return path


def run_init(fake, *args, run=None, environ=None):
    run = run or FakeRun(fake)
    lines = []
    code = documents_cli.main(
        ["init", "--repo", REPO, "--config-dir", "no-such-dir", *args],
        environ={"GITHUB_TOKEN": TOKEN} if environ is None else environ,
        run=run,
        session=fake,
        out=lines.append,
        sleep=lambda s: None,
    )
    return code, "\n".join(lines), run


def manifest(fake):
    return json.loads(fake.file(di.MANIFEST_PATH))


def changed_paths(fake, before_head):
    before = fake.trees[fake.commits[before_head]["tree"]]
    after = fake.tree()
    return {p for p in after if before.get(p) != after[p]}


# -- the render ----------------------------------------------------------------


def test_every_template_is_its_source_apart_from_the_marker_line():
    """The single-source guarantee: a template is edited in handoff_templates/ only."""
    rendered = di.render_managed(REPO, "main")
    for name in di.TEMPLATE_FILES:
        source = (di.TEMPLATE_DIR / name).read_bytes().replace(b"\r\n", b"\n")
        marker, _, body = rendered[f"templates/{name}"].partition(b"\n")
        assert body == source, name
        assert marker.startswith(b"<!-- Managed by content-intelligence")
        assert f"handoff_templates/{name}".encode() in marker


def test_the_templates_are_the_ones_ci_setup_copies_plus_the_two_the_drafting_side_needs():
    from ci_article_review.setup import _WORKING_TEMPLATES

    assert set(_WORKING_TEMPLATES) <= set(di.TEMPLATE_FILES)
    assert set(di.TEMPLATE_FILES) - set(_WORKING_TEMPLATES) == {
        "close_out.md",
        "revise_after_review_prompt.md",
    }
    for name in di.TEMPLATE_FILES:
        assert (di.TEMPLATE_DIR / name).is_file(), name


def test_claude_md_and_gitattributes_keep_their_exact_content():
    rendered = di.render_managed(REPO, "main")
    assert rendered["CLAUDE.md"] == b"@AGENTS.md\n"
    first, _, rest = rendered[".gitattributes"].partition(b"\n")
    assert first == b"* text=auto eol=lf"
    assert rest.startswith(b"# Managed by content-intelligence")


def test_markdown_files_start_with_a_marker_naming_their_source():
    rendered = di.render_managed(REPO, "main")
    for path in ("AGENTS.md", "README.md", "instructions/claude-project.md"):
        first = rendered[path].split(b"\n")[0].decode()
        assert first.startswith("<!-- Managed by content-intelligence"), path
        assert "document_repo/" in first, path


def test_nothing_rendered_has_a_carriage_return():
    for path, data in di.render_managed(REPO, "main").items():
        assert b"\r" not in data, path


def test_the_repository_name_and_branch_reach_the_files_that_name_them():
    rendered = di.render_managed("someone/else", "trunk")
    assert b"someone/else" in rendered["instructions/claude-project.md"]
    assert b"trunk" in rendered["instructions/claude-project.md"]
    ours = [d for p, d in rendered.items() if not p.startswith("templates/")]
    assert b"{{" not in b"".join(ours)


def test_agents_md_states_every_name_and_commit_message_the_pipeline_depends_on():
    """The doc and the code are two statements of one convention; keep them in step."""
    text = di.render_managed(REPO, "main")["AGENTS.md"].decode()
    for needle in (
        "<slug>-draft-rN.md",
        "<slug>-metadata-rN.md",
        "<slug>-handoff-rN.md",
        "<slug>-publication-rN.md",
        "<slug>-close-out-rN-to-(code|chat)[-vM].md",
        "<slug>-review-rN.md",
        "draft: <slug> rN",
        "close-out: <slug> rN to-code",
        "publication: <slug> rN",
        "review: <slug> rN",
        "ci-review --github <slug>",
        "pipeline/",
        "Reviewed:",
        "Sync now",
        "templates/revise_after_review_prompt.md",
        "main",
        "force",
        # Committing the pair in one commit needs the connector's push_files.
        "`push_files`",
        "`create_or_update_file` makes one commit per file",
        # Under --github a missing History key is the folder's slug, not the title.
        "uses the article's folder name, `<slug>`, as its history key",
        "does not apply to a round read from this repository",
        # Close-out notes: list first, the naming rules, never overwrite, ci-docs put.
        "List the folder first",
        "`ci-docs ls <slug>`",
        "`-v2` needs the plain note",
        "Never overwrite one",
        "`ci-docs put <slug> <file>`",
        "`handoff: <slug> rN`",
    ):
        assert needle in text, needle
    # The "never" list.
    for needle in (
        "Rename or move an article folder",
        "Modify or delete a numbered file",
        "Rewrite history",
        "secret, a token or a credential",
        "Read `pipeline/`",
        "Edit a managed file",
    ):
        assert needle in text, needle


def test_the_packaged_sources_exist_and_are_not_named_like_instructions():
    """A file called AGENTS.md or CLAUDE.md in this package would be read as instructions for it."""
    names = {p.name for p in (ROOT / "document_repo").iterdir()}
    for _, src, _ in di.REPO_FILES:
        assert src in names, src
    assert "articles_README.md.tmpl" in names
    assert not names & {"AGENTS.md", "CLAUDE.md", ".gitattributes", "README.md"}


# -- creating the repository ---------------------------------------------------


def test_a_missing_repository_is_created_seeded_and_filled_in_one_further_commit(
    seed_file,
):
    fake = FakeGitHub(exists=False)
    code, out, run = run_init(fake, "--create", "--seed-instructions", str(seed_file))
    assert code == 0, out
    assert run.gh_calls() == [
        ["gh", "auth", "status"],
        [
            "gh",
            "repo",
            "create",
            REPO,
            "--private",
            "--description",
            "Articles for mikehammett.net: drafts, metadata, hand-offs and ci-review reviews",
        ],
    ]
    # One seeding PUT of README.md, then one commit: two commits in all.
    puts = [r for r in fake.requests if r[0] == "PUT"]
    assert [p[1].rsplit("/", 1)[-1] for p in puts] == ["README.md"]
    assert len(fake.commits) == 2
    short = HEAD[:7]
    assert (
        fake.messages()[0]
        == f"onboarding: seed README from content-intelligence {short}"
    )
    last = fake.commits[fake.head()]
    assert (
        last["message"].splitlines()[0]
        == f"onboarding: refresh from content-intelligence {short}"
    )
    expected = set(di.render_managed(REPO, "main")) | {
        di.MANIFEST_PATH,
        di.ARTICLES_README_PATH,
        di.PUBLICATION_PATH,
    }
    assert set(fake.tree()) == expected


def test_the_manifest_records_the_blob_of_every_managed_file(seed_file):
    fake = FakeGitHub(exists=False)
    run_init(fake, "--create", "--seed-instructions", str(seed_file))
    rendered = di.render_managed(REPO, "main")
    body = manifest(fake)
    assert body["source_commit"] == HEAD
    assert body["files"] == {p: git_blob_sha(d) for p, d in sorted(rendered.items())}
    # Written once, never managed: not in the manifest.
    assert di.ARTICLES_README_PATH not in body["files"]
    assert di.PUBLICATION_PATH not in body["files"]
    assert fake.file(di.MANIFEST_PATH).endswith(b"\n")


def test_the_seed_instructions_file_is_committed_as_given_apart_from_line_endings(
    seed_file,
):
    fake = FakeGitHub(exists=False)
    run_init(fake, "--create", "--seed-instructions", str(seed_file))
    assert fake.file(di.PUBLICATION_PATH) == b"# Example publication\n\nVoice: plain.\n"


def test_a_missing_repository_without_create_is_reported_and_nothing_is_made():
    fake = FakeGitHub(exists=False)
    code, out, run = run_init(fake)
    assert code == 1
    assert run.gh_calls() == []
    assert fake.writes == []
    assert (
        f"gh repo create {REPO} --private --description "
        '"Articles for mikehammett.net: drafts, metadata, hand-offs and ci-review reviews"'
    ) in out
    assert "--create" in out


def test_create_stops_before_creating_when_gh_is_not_logged_in():
    fake = FakeGitHub(exists=False)
    run = FakeRun(fake, logged_in=False)
    code, out, _ = run_init(fake, "--create", run=run)
    assert code == 1
    assert run.gh_calls() == [["gh", "auth", "status"]]
    assert fake.exists is False
    assert fake.writes == []


def test_an_existing_empty_repository_is_seeded_without_calling_gh_to_create_it():
    fake = FakeGitHub(exists=True, empty=True)
    code, out, run = run_init(fake)
    assert code == 0, out
    assert run.gh_calls() == []
    assert len(fake.commits) == 2
    assert di.MANIFEST_PATH in fake.tree()


# -- dry run -------------------------------------------------------------------


def test_dry_run_on_a_missing_repository_prints_the_command_and_writes_nothing(
    seed_file,
):
    fake = FakeGitHub(exists=False)
    code, out, run = run_init(fake, "--dry-run", "--seed-instructions", str(seed_file))
    assert code == 0
    assert fake.writes == []
    assert run.gh_calls() == []  # not even `gh auth status`
    assert (
        f'gh repo create {REPO} --private --description "Articles for mikehammett.net'
        in out
    )
    assert "seed the empty repository with README.md" in out
    for path in di.render_managed(REPO, "main"):
        assert path in out
    assert di.PUBLICATION_PATH in out and di.MANIFEST_PATH not in out.split("commit")[0]
    assert "Dry run" in out


def test_dry_run_on_an_existing_repository_writes_nothing():
    fake = FakeGitHub()
    before = dict(fake.branches)
    code, out, run = run_init(fake, "--dry-run")
    assert code == 0
    assert fake.writes == []
    assert fake.branches == before
    assert run.gh_calls() == []
    assert "add     AGENTS.md" in out


# -- re-runs -------------------------------------------------------------------


def test_a_rerun_on_an_unchanged_repository_makes_no_commit(seed_file):
    fake = FakeGitHub(exists=False)
    run_init(fake, "--create", "--seed-instructions", str(seed_file))
    head, commits, writes = fake.head(), len(fake.commits), len(fake.writes)
    code, out, _ = run_init(fake, "--seed-instructions", str(seed_file))
    assert code == 0
    assert (fake.head(), len(fake.commits), len(fake.writes)) == (head, commits, writes)
    assert "nothing to commit" in out


def test_a_rerun_from_a_newer_source_commit_alone_makes_no_commit():
    fake = FakeGitHub()
    run_init(fake)
    head = fake.head()
    run = FakeRun(fake, head="b" * 40)
    code, out, _ = run_init(fake, run=run)
    assert code == 0 and fake.head() == head, out
    assert (
        manifest(fake)["source_commit"] == HEAD
    )  # the commit that last changed a file


def test_a_changed_template_replaces_exactly_that_file_and_the_manifest(
    tmp_path, monkeypatch
):
    fake = FakeGitHub()
    run_init(fake)
    before = fake.head()

    templates = tmp_path / "templates"
    templates.mkdir()
    for p in di.TEMPLATE_DIR.iterdir():
        if p.is_file():
            (templates / p.name).write_bytes(p.read_bytes())
    changed = templates / "close_out.md"
    changed.write_bytes(changed.read_bytes() + b"\nAn added line.\n")
    monkeypatch.setattr(di, "TEMPLATE_DIR", templates)

    code, out, _ = run_init(fake, run=FakeRun(fake, head="c" * 40))
    assert code == 0, out
    assert changed_paths(fake, before) == {"templates/close_out.md", di.MANIFEST_PATH}
    assert fake.file("templates/close_out.md").endswith(b"An added line.\n")
    body = manifest(fake)
    assert body["source_commit"] == "c" * 40
    assert body["files"]["templates/close_out.md"] == git_blob_sha(
        fake.file("templates/close_out.md")
    )
    assert fake.commits[fake.head()]["message"].startswith(
        "onboarding: refresh from content-intelligence ccccccc"
    )


def test_a_managed_file_edited_in_the_repository_is_left_alone_and_listed():
    fake = FakeGitHub()
    run_init(fake)
    fake.external_commit("main", {"AGENTS.md": b"edited by the author\n"})
    before = fake.head()
    code, out, _ = run_init(fake)
    assert code == 0
    assert fake.head() == before  # nothing else changed, so no commit at all
    assert fake.file("AGENTS.md") == b"edited by the author\n"
    assert "EDITED  AGENTS.md" in out
    assert "--overwrite-edited" in out


def test_an_edited_file_stays_alone_while_the_rest_still_refresh(tmp_path, monkeypatch):
    fake = FakeGitHub()
    run_init(fake)
    fake.external_commit("main", {"AGENTS.md": b"edited by the author\n"})
    before = fake.head()
    templates = tmp_path / "t"
    templates.mkdir()
    for p in di.TEMPLATE_DIR.iterdir():
        if p.is_file():
            (templates / p.name).write_bytes(p.read_bytes())
    (templates / "metadata_only.md").write_bytes(b"new metadata template\n")
    monkeypatch.setattr(di, "TEMPLATE_DIR", templates)
    code, out, _ = run_init(fake)
    assert code == 0, out
    assert changed_paths(fake, before) == {
        "templates/metadata_only.md",
        di.MANIFEST_PATH,
    }
    assert fake.file("AGENTS.md") == b"edited by the author\n"
    # Still recognised as edited next time: the manifest kept what was last written.
    assert manifest(fake)["files"]["AGENTS.md"] == git_blob_sha(
        di.render_managed(REPO, "main")["AGENTS.md"]
    )


def test_overwrite_edited_replaces_it():
    fake = FakeGitHub()
    run_init(fake)
    fake.external_commit("main", {"AGENTS.md": b"edited by the author\n"})
    code, out, _ = run_init(fake, "--overwrite-edited")
    assert code == 0
    assert fake.file("AGENTS.md") == di.render_managed(REPO, "main")["AGENTS.md"]
    assert (
        "Replaced although edited here: AGENTS.md"
        in fake.commits[fake.head()]["message"]
    )


def test_without_a_manifest_every_differing_file_counts_as_edited():
    fake = FakeGitHub()
    fake.external_commit("main", {"AGENTS.md": b"someone's own file\n"})
    code, out, _ = run_init(fake)
    assert code == 0
    assert fake.file("AGENTS.md") == b"someone's own file\n"
    assert "EDITED  AGENTS.md" in out
    assert di.MANIFEST_PATH in fake.tree()


def test_an_unreadable_manifest_is_reported_and_rewritten():
    fake = FakeGitHub()
    run_init(fake)
    fake.external_commit("main", {di.MANIFEST_PATH: b"{not json"})
    code, out, _ = run_init(fake)
    assert code == 0
    assert "could not be read as JSON" in out
    assert "files" in manifest(fake)


# -- what is never touched -----------------------------------------------------


def test_articles_and_the_publication_instructions_are_never_in_a_later_commit(
    seed_file, tmp_path, monkeypatch
):
    fake = FakeGitHub(exists=False)
    run_init(fake, "--create", "--seed-instructions", str(seed_file))
    article = f"articles/{SLUG}/{SLUG}-draft-r1.md"
    fake.external_commit(
        "main", {article: b"A draft.\n", di.PUBLICATION_PATH: b"the author's own\n"}
    )
    fake.external_commit("main", {di.ARTICLES_README_PATH: b"edited\n"})
    before = fake.head()

    templates = tmp_path / "t"
    templates.mkdir()
    for p in di.TEMPLATE_DIR.iterdir():
        if p.is_file():
            (templates / p.name).write_bytes(p.read_bytes())
    (templates / "publication.md").write_bytes(b"changed template\n")
    monkeypatch.setattr(di, "TEMPLATE_DIR", templates)

    other_seed = tmp_path / "other.md"
    other_seed.write_bytes(b"a different seed\n")
    code, out, _ = run_init(
        fake, "--seed-instructions", str(other_seed), "--overwrite-edited"
    )
    assert code == 0, out
    assert changed_paths(fake, before) == {"templates/publication.md", di.MANIFEST_PATH}
    assert fake.file(article) == b"A draft.\n"
    assert fake.file(di.PUBLICATION_PATH) == b"the author's own\n"
    assert fake.file(di.ARTICLES_README_PATH) == b"edited\n"
    assert "already exists" in out


def test_the_articles_readme_is_only_created_while_articles_is_empty():
    fake = FakeGitHub()
    fake.external_commit("main", {f"articles/{SLUG}/{SLUG}-draft-r1.md": b"A draft.\n"})
    code, out, _ = run_init(fake)
    assert code == 0
    assert di.ARTICLES_README_PATH not in fake.tree()


def test_without_seed_instructions_the_file_is_not_created_and_the_run_says_how():
    fake = FakeGitHub()
    code, out, _ = run_init(fake)
    assert code == 0
    assert di.PUBLICATION_PATH not in fake.tree()
    assert "--seed-instructions" in out


def test_a_missing_seed_file_stops_the_run_before_any_request(tmp_path):
    fake = FakeGitHub()
    code, out, _ = run_init(fake, "--seed-instructions", str(tmp_path / "nope.md"))
    assert code == 1
    assert fake.requests == []


# -- refusals and the token ----------------------------------------------------


def test_an_existing_public_repository_is_refused():
    fake = FakeGitHub(private=False)
    code, out, _ = run_init(fake)
    assert code == 1
    assert fake.writes == []


def test_a_token_that_cannot_push_is_refused_before_any_write():
    fake = FakeGitHub(push=False)
    code, out, _ = run_init(fake)
    assert code == 1
    assert fake.writes == []


def test_no_token_is_a_plain_message(capsys):
    class GhHasNoToken(FakeRun):
        def __call__(self, argv, **kw):
            if argv[:3] == ["gh", "auth", "token"]:
                return Done(1)
            return super().__call__(argv, **kw)

    fake = FakeGitHub()
    code, out, _ = run_init(fake, run=GhHasNoToken(fake), environ={})
    assert code == 1
    assert fake.requests == []
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_the_token_appears_in_no_output(seed_file, capsys):
    fake = FakeGitHub(exists=False)
    code, out, run = run_init(fake, "--create", "--seed-instructions", str(seed_file))
    streams = capsys.readouterr()
    assert TOKEN not in out + streams.out + streams.err
    assert not any(TOKEN in " ".join(c) for c in run.calls)
    for message in fake.messages():
        assert TOKEN not in message


def test_a_seed_file_holding_the_token_is_refused(tmp_path):
    fake = FakeGitHub()
    leaky = tmp_path / "leaky.md"
    leaky.write_text(f"key: {TOKEN}\n", encoding="utf-8")
    code, out, _ = run_init(fake, "--seed-instructions", str(leaky))
    assert code == 1
    assert fake.writes == []


# -- configuration and the source commit ---------------------------------------


def test_the_repository_defaults_to_the_configured_one_then_the_built_in_name(tmp_path):
    (tmp_path / "user.yaml").write_text(
        "github_documents:\n  repo: configured-owner/configured-articles\n  branch: trunk\n",
        encoding="utf-8",
    )
    assert documents_cli._configured(tmp_path) == {
        "repo": "configured-owner/configured-articles",
        "branch": "trunk",
    }
    for text in ("", "- a list\n", "github_documents: nonsense\n", "key: [unclosed\n"):
        (tmp_path / "user.yaml").write_text(text, encoding="utf-8")
        assert documents_cli._configured(tmp_path) == {}
    assert documents_cli._configured(tmp_path / "missing") == {}
    assert di.DEFAULT_REPO == "MHammett/mikehammett.net-articles"


def test_the_repo_flag_beats_the_configuration(tmp_path):
    (tmp_path / "user.yaml").write_text(
        "github_documents:\n  repo: configured-owner/configured-articles\n",
        encoding="utf-8",
    )
    fake = FakeGitHub()
    lines = []
    code = documents_cli.main(
        ["init", "--dry-run", "--repo", REPO, "--config-dir", str(tmp_path)],
        environ={"GITHUB_TOKEN": TOKEN},
        run=FakeRun(fake),
        session=fake,
        out=lines.append,
    )
    assert code == 0 and f"Repository: {REPO}" in "\n".join(lines)


def test_the_source_commit_is_the_package_version_outside_a_checkout():
    full, short = di.source_commit(run=FakeRun(FakeGitHub(), head=None))
    assert full.startswith("ci-article-review ") and short.startswith("v")


def test_uncommitted_sources_mark_the_commit_dirty():
    full, short = di.source_commit(
        run=FakeRun(FakeGitHub(), dirty=" M handoff_templates/close_out.md\n")
    )
    assert full == HEAD + "+dirty" and short == HEAD[:7] + "+dirty"


def test_the_commit_message_lists_what_changed():
    plan = di.Plan(add=["a"], replace=["b", "c"], overwritten=["c"], edited=["d"])
    assert di.commit_message("abc1234", plan).splitlines() == [
        "onboarding: refresh from content-intelligence abc1234",
        "",
        "Added: a",
        "Replaced: b",
        "Replaced although edited here: c",
        "Left alone, edited here: d",
    ]
