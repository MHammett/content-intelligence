"""``ci-docs``: keep the private document repository self-describing.

``ci-docs init`` creates the GitHub repository that holds the articles when it
is missing (only with ``--create``), then commits the onboarding files and
templates rendered from sources packaged here, in one commit. Re-running it
refreshes them: a managed file is replaced only if nobody has edited it in the
document repository since the last run. It also sets the repository to merge
commits only (one ``PATCH``, only when a setting differs) and creates the issue
labels that are missing, never editing one that exists. The design is
docs/GITHUB-DOCUMENTS.md; what each file is for is in ``documents_init``.

``ci-docs push-run`` writes a run that is already saved on disk back to its
article folder, which is the retry for a ``ci-review --github SLUG --write-back``
whose commit failed (``github_writeback``).

``ci-docs ls SLUG`` shows an article's rounds and the files of its folder (reads
only), and ``ci-docs put SLUG FILE`` commits one file to the open round's branch
(``github_put``).

A round is accepted by merging its pull request, and the merge is tagged
``<slug>/rN`` automatically: ``push-run``, ``put`` and ``accept --yes`` tag any
merged, untagged round of the article before their own work, and ``ls`` never
does. ``ci-docs tag SLUG`` runs that step alone, and ``ci-docs accept SLUG``
merges the open round when the author has said to (``github_rounds``).

    ci-docs init --dry-run                  # the plan; writes nothing
    ci-docs init --create                   # make the repository too, if missing
    ci-docs init --seed-instructions PATH   # first run: instructions/publication.md
    ci-docs push-run pipeline_history/example-article/run_4_<ts>_report.json
    ci-docs ls example-article
    ci-docs put example-article close-out-to-chat.md
    ci-docs tag example-article --dry-run
    ci-docs accept example-article          # shows the open round; --yes merges it

``ci-docs import scan SLUG ... --out PLAN.json`` is step 1 of importing an
article's history from local files (``history_import``): it merges the sources
into an editable plan and prints a timeline, and ``ci-docs import check
PLAN.json`` validates the plan after it is edited. Both read local files only.

    ci-docs import scan example-article --files ~/Downloads/example-article-v27 \\
        --out ~/private/example-article-plan.json
    ci-docs import check ~/private/example-article-plan.json

``ci-docs import apply PLAN.json --repo OWNER/NAME`` is step 2
(``history_apply``): it writes the plan as merged pull requests, one per round,
in order, with the original dates on every commit, and resumes after a failure
from the plan's ``applied`` block. ``--repo`` is required, so the real repository
is never written by accident, and ``--dry-run`` prints every write it would make
and makes none.

    ci-docs import apply ~/private/example-article-plan.json --repo OWNER/NAME --dry-run
    ci-docs import apply ~/private/example-article-plan.json --repo OWNER/NAME

The token is ``GITHUB_TOKEN``, else ``gh auth token``. It is never printed.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import yaml

from ci_core.console import force_utf8_stdio
from ci_core.github_repo import GitHubRepo, GitHubRepoError, resolve_token

from . import documents_init as di
from .github_documents import GitHubDocumentsError

# The plan lists paths and the author's repository name, not article text, but a
# repository name or an error body can hold anything; keep the console from
# crashing on it.
force_utf8_stdio()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ci-docs",
        description="Maintain the private GitHub repository that holds the articles.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser(
        "init",
        help="create the document repository if missing and commit its onboarding files",
        description=(
            "Render AGENTS.md, CLAUDE.md, README.md, .gitattributes, templates/, "
            "articles/README.md and instructions/claude-project.md from this "
            "repository's packaged sources and commit them to the document "
            "repository in one commit. A managed file is replaced only if its "
            "current blob is the one the manifest recorded. Also set the "
            "repository to allow merge commits only, and create the issue labels "
            "that are missing (an existing label is never edited). No article and "
            "no existing instructions/publication.md is ever touched."
        ),
    )
    init.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help=(
            "the document repository (default: github_documents.repo in "
            f"user.yaml, else {di.DEFAULT_REPO})"
        ),
    )
    init.add_argument(
        "--branch",
        default=None,
        help=f"branch to commit to (default: github_documents.branch, else {di.DEFAULT_BRANCH})",
    )
    init.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan, including the repository creation command, and write nothing",
    )
    init.add_argument(
        "--create",
        action="store_true",
        help=(
            "if the repository does not exist, create it (private) with `gh repo "
            "create`. Without this a missing repository is reported and nothing is made."
        ),
    )
    init.add_argument(
        "--seed-instructions",
        metavar="PATH",
        help=(
            "a local file to commit once as instructions/publication.md (the "
            "publication's own drafting rules). Ignored if that file exists."
        ),
    )
    init.add_argument(
        "--overwrite-edited",
        action="store_true",
        help="also replace managed files that were edited in the document repository",
    )
    init.add_argument(
        "--config-dir",
        default="configs",
        metavar="DIR",
        help="where user.yaml is read from, for github_documents (default: configs)",
    )
    push = sub.add_parser(
        "push-run",
        help="write a saved run's review back to its article folder (the retry for --write-back)",
        description=(
            "Commit a run already saved under pipeline_history/ to the article "
            "folder its report says it was read from: the review beside the "
            "draft, the worklist and the report under pipeline/. One commit, new "
            "files only: a file already there with the same bytes is left alone "
            "(running it twice is a no-op) and one with different bytes stops "
            "it. A report under _replay/ or one that was not read from --github "
            "is refused."
        ),
    )
    push.add_argument(
        "report",
        metavar="REPORT_JSON",
        help="the run's pipeline_history/<key>/run_N_<timestamp>_report.json",
    )
    push.add_argument(
        "--github-repo",
        metavar="OWNER/NAME",
        help="commit to this repository instead of the one the report names",
    )
    push.add_argument(
        "--config-dir",
        default="configs",
        metavar="DIR",
        help=(
            "where user.yaml is read from, for github_documents and the API "
            "keys the commit must not contain (default: configs)"
        ),
    )

    ls = sub.add_parser(
        "ls",
        help="show an article's rounds and the files of its folder",
        description=(
            "Show the article SLUG: its rounds (tags with their dates, merged and "
            "open pull requests with branch, head and the last review's run), then "
            "the files of articles/SLUG/ at REF with each one's last commit. REF "
            "defaults to the open round's branch, else the base branch. Reads "
            "only: it tags nothing."
        ),
    )
    ls.add_argument("slug", metavar="SLUG", help="the article's folder name")
    ls.add_argument(
        "--ref",
        metavar="REF",
        default=None,
        help="list the files at this branch, tag or commit (default: the open round's branch, else the base branch)",
    )
    _repo_options(ls)

    put = sub.add_parser(
        "put",
        help="commit one file to the open round's branch",
        description=(
            "Commit one local file to the open round's branch of the article "
            "SLUG, never to the base branch. The file's own name is the name it "
            "gets and must be close-out-to-chat.md, close-out-to-code.md, "
            "draft.md, metadata.md or publication.md; review.md and anything "
            "under pipeline/ belong to the pipeline and are refused. An existing "
            "file is replaced only if it is still what put read; the same bytes "
            "make no commit. A close-out note is announced with a comment on the "
            "pull request. The file must not contain a configured API key or the "
            "token. A merged, untagged round of the article is tagged first."
        ),
    )
    put.add_argument("slug", metavar="SLUG", help="the article's folder name")
    put.add_argument("file", metavar="FILE", help="the local file to commit")
    put.add_argument(
        "--pr",
        type=int,
        metavar="N",
        default=None,
        help="the round's pull request, when the article has more than one open",
    )
    _repo_options(put)
    put.add_argument(
        "--publication",
        metavar="NAME",
        help=(
            "also load configs/NAME.yaml, for API keys it sets that the file "
            "must not contain (default: user.yaml's keys)"
        ),
    )
    imp = sub.add_parser(
        "import",
        help="import an article's local history: scan its sources into an editable plan, then check it",
        description=(
            "Step 1 of the history import (docs/GITHUB-DOCUMENTS.md, Decision 7): "
            "`scan` reads one article's local sources, de-duplicates by content, "
            "orders them by the best timestamp, groups them into rounds, prints a "
            "timeline and writes plan.json; `check` validates the plan after the "
            "author has edited it. Neither writes to GitHub."
        ),
    )
    imp_sub = imp.add_subparsers(dest="import_command", required=True)
    scan = imp_sub.add_parser(
        "scan",
        help="read the sources, print a timeline and write an editable plan.json",
        description=(
            "Read one article's local sources and write plan.json. The sources are "
            "private: nothing is printed from inside a file, and --out inside the "
            "content-intelligence checkout is refused. A run is read from "
            "pipeline_history/<key>/run_N_<ts>_report.json (never _experiments/, "
            "_replay/ or _nested/)."
        ),
    )
    scan.add_argument("slug", metavar="SLUG", help="the article's slug")
    scan.add_argument(
        "--history-key",
        action="append",
        metavar="KEY",
        dest="history_keys",
        help=(
            "a pipeline_history/ directory that belongs to the article; repeat for "
            "a title-derived key (default: SLUG)"
        ),
    )
    scan.add_argument(
        "--history-root",
        metavar="DIR",
        help=(
            "where the history keys live (default: $CI_HISTORY_ROOT, else the main "
            "checkout's pipeline_history/, found from this code's location)"
        ),
    )
    scan.add_argument(
        "--files",
        action="append",
        metavar="DIR",
        default=[],
        help=(
            "a directory to scan, non-recursive plus one level (for "
            "~/Downloads/<slug>-vNN/); only files whose names mention the slug or a "
            "history key are read. Repeatable."
        ),
    )
    scan.add_argument(
        "--include",
        action="append",
        metavar="NAME",
        default=[],
        help="also read this file name from --files, whatever it is called. Repeatable.",
    )
    scan.add_argument(
        "--drive-dir",
        metavar="DIR",
        help=(
            "files a Code session downloaded from Drive with download_file_content, "
            "plus drive-manifest.json; each file's MD5 is checked against it"
        ),
    )
    scan.add_argument(
        "--recovered",
        metavar="DIR",
        help=(
            "versions a drafting chat reproduced from its context. The least "
            "trusted source: dated only by a <file>.json sidecar's claimed_at, "
            "and dropped when a run or a file holds the same version"
        ),
    )
    scan.add_argument(
        "--chat-export",
        metavar="PATH",
        help=(
            "a claude.ai data export (the directory, the ZIP, or conversations.json). "
            "Conversations carry no project id, so they are matched to the article by "
            "what they say; versions are rebuilt from tool calls, attachments and "
            "long messages and kept only if they resemble a version another source holds"
        ),
    )
    scan.add_argument(
        "--conversation",
        action="append",
        metavar="UUID",
        default=[],
        dest="conversations",
        help=(
            "a conversation of the export that belongs to the article even though it "
            "does not name it; repeatable"
        ),
    )
    scan.add_argument(
        "--chat-map",
        metavar="FILE",
        help=(
            "a TSV with a header 'conversation<TAB>slug' (other columns ignored) "
            "that says which export conversation belongs to which article. The "
            "conversations linked to SLUG are exactly its rows plus any "
            "--conversation; none is linked by what it says. Their whole documents "
            "are kept even when no other source holds a similar version. Needs "
            "--chat-export"
        ),
    )
    scan.add_argument(
        "--min-chat-words",
        type=int,
        default=300,
        metavar="N",
        help=(
            "the fewest words a whole document from a linked conversation "
            "(--chat-map or --conversation) may have to be kept on its own "
            "(default 300)"
        ),
    )
    scan.add_argument(
        "--out",
        required=True,
        metavar="PLAN.json",
        help="where to write the plan; must be outside the content-intelligence checkout",
    )
    check = imp_sub.add_parser(
        "check",
        help="validate an edited plan and print its timeline again",
        description=(
            "Check that an edited plan.json is consistent (times in order within a "
            "round, rounds in order, every included event's source file present "
            "with the recorded hash) and print the timeline. Exits 1 on a problem."
        ),
    )
    check.add_argument("plan", metavar="PLAN.json", help="the plan to check")
    apply_ = imp_sub.add_parser(
        "apply",
        help="write an edited plan into the document repository as merged pull requests",
        description=(
            "Step 2 of the history import. For each round of the plan, in order: "
            "a branch round/SLUG/rN, one commit per event dated at the event's "
            "time, a pull request with a review per run, a merge commit dated at "
            "accepted_at, and the annotated tag SLUG/rN. Each step is recorded in "
            "the plan's applied block and the plan file is rewritten, so running "
            "the same command again resumes after a failure and repeats nothing. "
            "Refuses a public repository, a repository that is not merge-commit-"
            "only, one that already has the article, or one that has rounds or "
            "tags of it the plan does not account for. Nothing is deleted, moved "
            "or force-pushed."
        ),
    )
    apply_.add_argument("plan", metavar="PLAN.json", help="the checked plan to apply")
    apply_.add_argument(
        "--repo",
        required=True,
        metavar="OWNER/NAME",
        help="the document repository to write (required: there is no default)",
    )
    apply_.add_argument(
        "--dry-run",
        action="store_true",
        help="print every write it would make and make none (it still reads GitHub)",
    )
    apply_.add_argument(
        "--config-dir",
        default="configs",
        metavar="DIR",
        help=(
            "where user.yaml is read from, for path_prefix and the API keys no "
            "file may contain (default: configs)"
        ),
    )

    tag = sub.add_parser(
        "tag",
        help="tag the article's merged rounds that have no tag yet",
        description=(
            "Create the annotated tag SLUG/rN on the merge commit of every merged "
            "round pull request of the article that has none, tagger dated at "
            "the merge. Commands that write to the article do this on their own "
            "first; this runs it alone. Safe to run twice, or at once with "
            "another command: a tag that exists is never moved."
        ),
    )
    tag.add_argument("slug", metavar="SLUG", help="the article's folder name")
    tag.add_argument(
        "--dry-run",
        action="store_true",
        help="say which tags would be made and write nothing",
    )
    _repo_options(tag)

    accept = sub.add_parser(
        "accept",
        help="merge the open round's pull request, then tag it (when the author has said to)",
        description=(
            "Show the article's open round pull request, its head, the last "
            "review's run and whether a commit has landed since that review. "
            "With --yes, merge it with a merge commit, only if its head is still "
            "the one shown, and tag the round. A merge is the author's "
            "acceptance: a Code session passes --yes only when the author has "
            "said to accept the round. The normal path is the drafting chat "
            "merging the pull request on the author's word."
        ),
    )
    accept.add_argument("slug", metavar="SLUG", help="the article's folder name")
    accept.add_argument(
        "--pr",
        type=int,
        metavar="N",
        default=None,
        help="the round's pull request, when the article has more than one open",
    )
    accept.add_argument(
        "--yes",
        action="store_true",
        help="merge the pull request (without it, only show it)",
    )
    _repo_options(accept)
    return parser


def _repo_options(parser):
    parser.add_argument(
        "--github-repo",
        metavar="OWNER/NAME",
        help=(
            "the document repository (default: github_documents.repo in "
            f"user.yaml, else {di.DEFAULT_REPO})"
        ),
    )
    parser.add_argument(
        "--config-dir",
        default="configs",
        metavar="DIR",
        help="where user.yaml is read from (default: configs)",
    )


def _configured(config_dir):
    """``github_documents`` from user.yaml, or ``{}``.

    Read straight from the YAML, defensively: the block is optional, the file may
    not exist, and going through ``load_user_config`` would make an unset
    ``${VAR}`` for some unrelated provider stop a command that needs none.
    """
    try:
        data = yaml.safe_load(
            (Path(config_dir) / "user.yaml").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    block = data.get("github_documents") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


def _string(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _create_argv(repo):
    return [
        "gh",
        "repo",
        "create",
        repo,
        "--private",
        "--description",
        di.REPO_DESCRIPTION,
    ]


def _quoted(argv):
    # shlex.join quotes with single quotes, which cmd.exe and PowerShell do not
    # read the same way; the double-quoted form is the one to paste anywhere.
    return " ".join(
        f'"{a}"' if (" " in a or ":" in a or "," in a) else shlex.quote(a) for a in argv
    )


def _gh(run, *args):
    return run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )


def _fail(message):
    print(f"error: {message}", file=sys.stderr)
    return 1


def _check_access(gh, info):
    if not info.private:
        raise GitHubRepoError(
            "other",
            f"{gh.repo} is public. Drafts committed there would be published, so "
            "ci-docs will not write to it. Make it private first.",
        )
    if not info.can_push:
        raise GitHubRepoError(
            "forbidden", f"this token cannot push to {gh.repo} (no write permission)"
        )


def _is_empty(gh, branch):
    try:
        gh.resolve(branch)
    except GitHubRepoError as exc:
        if exc.kind == "empty":
            return True
        raise
    return False


def _print_plan(
    out, repo, branch, short, existence, seed_needed, plan, settings, labels
):
    """``settings`` is ``di.settings_changes``; ``labels`` the names to create."""
    out(f"Repository: {repo}, branch {branch}")
    out(f"Rendered from content-intelligence {short}")
    if existence is not None:
        out(f"  {existence}")
    if settings:
        out(f"  settings: {di.format_settings(settings)}")
    else:
        out("  settings: already merge commits only; nothing to change")
    if labels:
        out(f"  labels  create {', '.join(labels)}")
    else:
        out("  labels  all present; nothing to create")
    if seed_needed:
        out(
            f"  seed the empty repository with {di.SEED_PATH} (one Contents API commit)"
        )
    for label, paths in (
        ("add", plan.add),
        ("replace", plan.replace),
        ("same", plan.unchanged),
    ):
        for path in paths:
            out(f"  {label:<8}{path}")
    for path in plan.overwritten:
        out(
            f"  note    {path} was edited in the repository; replaced (--overwrite-edited)"
        )
    for path in plan.edited:
        out(f"  EDITED  {path}: edited in the document repository, left alone")
    for note in plan.notes:
        out(f"  note    {note}")
    if plan.edited:
        out(
            "  Edit those in content-intelligence instead, or run again with "
            "--overwrite-edited to replace them."
        )
    if plan.needs_commit:
        out(f"  commit  onboarding: refresh from content-intelligence {short}")
    else:
        out("  nothing to commit")


def main(
    argv=None,
    *,
    environ=os.environ,
    run=subprocess.run,
    session=None,
    out=print,
    sleep=time.sleep,
):
    """Entry point. ``environ``, ``run``, ``session`` and ``sleep`` are for tests."""
    args = build_parser().parse_args(argv)
    if args.command == "import" and args.import_command != "apply":
        # Local files only: no token, no network, nothing to catch from GitHub.
        return _import(args, environ, out)
    try:
        if args.command == "import":
            return _import_apply(args, environ, run, session, out)
        if args.command == "push-run":
            return _push_run(args, environ, run, session, out)
        if args.command == "ls":
            return _ls(args, environ, run, session, out)
        if args.command == "put":
            return _put(args, environ, run, session, out)
        if args.command == "tag":
            return _tag(args, environ, run, session, out)
        if args.command == "accept":
            return _accept(args, environ, run, session, out)
        return _init(args, environ, run, session, out, sleep)
    except GitHubDocumentsError as exc:
        return _fail(str(exc))
    except GitHubRepoError as exc:
        if exc.kind == "rate_limited" and exc.reset_at:
            when = time.strftime("%H:%M:%S", time.localtime(exc.reset_at))
            return _fail(f"{exc} The limit lifts at {when}.")
        return _fail(str(exc))


def _import(args, environ, out):
    from . import history_import as hi

    try:
        if args.import_command == "check":
            return _import_check(args, hi, out)
        return _import_scan(args, environ, hi, out)
    except hi.ImportScanError as exc:
        return _fail(str(exc))


def _import_scan(args, environ, hi, out):
    # Refuse before reading anything: a wrong --out should cost nothing.
    hi.refuse_out_inside_checkout(args.out)
    plan, stats = hi.scan(
        args.slug,
        history_keys=args.history_keys,
        history_root=args.history_root,
        files=args.files,
        include=args.include,
        drive_dir=args.drive_dir,
        recovered=args.recovered,
        chat_export=args.chat_export,
        conversations=args.conversations,
        chat_map=args.chat_map,
        min_chat_words=args.min_chat_words,
        environ=environ,
    )
    hi.write_plan(plan, args.out)
    for line in hi.format_summary(stats):
        out(line)
    out("")
    for line in hi.format_timeline(plan):
        out(line)
    excluded = hi.format_excluded(plan)
    if excluded:
        out("")
        out("Excluded (not events):")
        for line in excluded:
            out(line)
    out("")
    out(f"Wrote {args.out}")
    out(
        "Edit it (include: false, move an event, set accepted_at), then: "
        f"ci-docs import check {args.out}"
    )
    return 0


def _import_apply(args, environ, run, session, out):
    from .config_loader import (
        load_user_config,
        merge_configs,
        normalize_github_documents,
    )
    from .github_rounds import open_client, require_token
    from .history_apply import apply_plan

    # The files must not contain a configured API key, so the keys have to be
    # known: a configuration that cannot be loaded stops the command.
    try:
        config = merge_configs(load_user_config(args.config_dir), {})
    except (OSError, ValueError) as exc:
        return _fail(
            f"cannot read the configuration in {args.config_dir} ({exc}); apply "
            "needs it to check the files for API keys. Pass --config-dir."
        )
    client = open_client(
        args.repo.strip(), require_token(lambda: resolve_token(environ, run)), session
    )
    try:
        apply_plan(
            args.plan,
            repo=args.repo,
            client=client,
            settings=normalize_github_documents(config.get("github_documents")),
            api_keys=config["api_keys"],
            dry_run=args.dry_run,
            out=out,
        )
    finally:
        client.close()
    return 0


def _import_check(args, hi, out):
    try:
        with open(args.plan, encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, ValueError) as exc:
        return _fail(f"cannot read the plan {args.plan}: {exc}")
    problems = hi.check_plan(plan)
    if isinstance(plan, dict) and plan.get("rounds"):
        for line in hi.format_timeline(plan):
            out(line)
    if problems:
        out("")
        for problem in problems:
            out(f"problem: {problem}")
        return _fail(f"{len(problems)} problem(s) in {args.plan}")
    out("")
    out(f"{args.plan}: ok")
    return 0


def _push_run(args, environ, run, session, out):
    # Imported here: only this command needs the whole config machinery.
    from .config_loader import (
        load_publication_config,
        load_user_config,
        merge_configs,
        normalize_github_documents,
    )
    from .github_rounds import tag_for_saved_run
    from .github_writeback import push_saved_run

    # The commit must not contain a configured API key, so the keys have to be
    # known. A config that cannot be loaded stops the command rather than
    # quietly skipping the scan.
    try:
        user = load_user_config(args.config_dir)
    except (OSError, ValueError) as exc:
        return _fail(
            f"cannot read the configuration in {args.config_dir} ({exc}); push-run "
            "needs it to check the commit for API keys. Pass --config-dir."
        )
    publication = _report_publication(args.report)
    try:
        pub = (
            load_publication_config(publication, args.config_dir) if publication else {}
        )
    except (OSError, ValueError):
        pub = {}
    config = merge_configs(user, pub)
    settings = normalize_github_documents(config.get("github_documents"))
    # Tag a merged round first, as every command that writes does. It is
    # silent about anything push_saved_run is about to refuse for itself.
    tag_for_saved_run(
        args.report,
        settings=settings,
        repo=args.github_repo,
        token_lookup=lambda: resolve_token(environ, run),
        session=session,
        out=out,
    )
    push_saved_run(
        args.report,
        settings=settings,
        api_keys=config["api_keys"],
        repo=args.github_repo,
        token_lookup=lambda: resolve_token(environ, run),
        session=session,
        out=out,
    )
    return 0


def _ls(args, environ, run, session, out):
    from .config_loader import normalize_github_documents
    from .github_put import format_listing, list_article

    header, rounds, files = list_article(
        args.slug,
        settings=normalize_github_documents(_configured(args.config_dir)),
        repo=args.github_repo,
        ref=args.ref,
        token_lookup=lambda: resolve_token(environ, run),
        session=session,
    )
    for line in format_listing(header, rounds, files):
        out(line)
    return 0


def _put(args, environ, run, session, out):
    from .config_loader import (
        load_publication_config,
        load_user_config,
        merge_configs,
        normalize_github_documents,
    )
    from .github_put import put_file

    # The file must not contain a configured API key, so the keys have to be
    # known: a configuration that cannot be loaded stops the command rather than
    # quietly skipping the scan (as push-run does).
    try:
        user = load_user_config(args.config_dir)
        pub = (
            load_publication_config(args.publication, args.config_dir)
            if args.publication
            else {}
        )
    except (OSError, ValueError) as exc:
        return _fail(
            f"cannot read the configuration in {args.config_dir} ({exc}); put "
            "needs it to check the file for API keys. Pass --config-dir."
        )
    config = merge_configs(user, pub)
    put_file(
        args.slug,
        args.file,
        settings=normalize_github_documents(config.get("github_documents")),
        api_keys=config["api_keys"],
        repo=args.github_repo,
        pr=args.pr,
        token_lookup=lambda: resolve_token(environ, run),
        session=session,
        out=out,
    )
    return 0


def _tag(args, environ, run, session, out):
    from .config_loader import normalize_github_documents
    from .github_rounds import tag_command

    results = tag_command(
        args.slug,
        settings=normalize_github_documents(_configured(args.config_dir)),
        repo=args.github_repo,
        dry_run=args.dry_run,
        token_lookup=lambda: resolve_token(environ, run),
        session=session,
        out=out,
    )
    # A conflict or a failure is a warning in the output; the exit status says
    # the command did not finish its job.
    return 1 if any(r.problem for r in results) else 0


def _accept(args, environ, run, session, out):
    from .config_loader import normalize_github_documents
    from .github_rounds import accept_round

    accept_round(
        args.slug,
        settings=normalize_github_documents(_configured(args.config_dir)),
        repo=args.github_repo,
        pr=args.pr,
        yes=args.yes,
        token_lookup=lambda: resolve_token(environ, run),
        session=session,
        out=out,
    )
    return 0


def _report_publication(path):
    """The publication a saved report was made for, if it can be read."""
    try:
        with open(path, encoding="utf-8") as f:
            name = json.load(f).get("publication")
    except (OSError, ValueError, AttributeError):
        return None
    return name if isinstance(name, str) and name.strip() else None


def _init(args, environ, run, session, out, sleep):
    configured = _configured(args.config_dir)
    repo = args.repo or _string(configured.get("repo")) or di.DEFAULT_REPO
    branch = args.branch or _string(configured.get("branch")) or di.DEFAULT_BRANCH

    seed = None
    if args.seed_instructions:
        try:
            seed = di.read_seed_instructions(args.seed_instructions)
        except OSError as exc:
            return _fail(
                f"cannot read --seed-instructions {args.seed_instructions}: {exc}"
            )
        if not seed.strip():
            return _fail(f"--seed-instructions {args.seed_instructions} is empty")

    token = resolve_token(environ, run)
    if not token:
        return _fail(
            "no GitHub token. Set GITHUB_TOKEN, or run `gh auth login` so "
            "`gh auth token` can supply one."
        )
    try:
        gh = GitHubRepo(repo, token, session=session)
    except ValueError as exc:
        return _fail(str(exc))

    with gh:
        return _init_with(gh, args, repo, branch, seed, run, out, sleep)


def _init_with(gh, args, repo, branch, seed, run, out, sleep):
    full, short = di.source_commit(run)
    rendered = di.render_managed(repo, branch)

    existence = None
    try:
        info = gh.info()
        exists = True
    except GitHubRepoError as exc:
        if exc.kind != "not_found":
            raise
        exists = False

    if exists:
        _check_access(gh, info)
    else:
        command = _quoted(_create_argv(repo))
        existence = (
            f"create the repository (private): {command}\n"
            "          (GitHub also answers 404 for a repository this token cannot "
            "see; check the name)"
        )
        if args.dry_run:
            seed_needed = True
            plan = di.make_plan(
                rendered,
                di.RepoState(),
                source_full=full,
                seed=seed,
                overwrite_edited=args.overwrite_edited,
            )
            _print_plan(
                out,
                repo,
                branch,
                short,
                existence,
                seed_needed,
                plan,
                di.settings_changes(di.NEW_REPO_INFO),
                [label.name for label in di.LABELS],
            )
            out("Dry run: nothing was created or written.")
            return 0
        if not args.create:
            out(f"{repo} does not exist, or this token cannot see it. To create it:")
            out(f"  {command}")
            out("or run this command again with --create. Nothing was created.")
            return 1
        _create(gh, repo, run, sleep, out)
        info = gh.info()
        _check_access(gh, info)

    changes = di.settings_changes(info)
    missing = [label for label in di.LABELS if not gh.label_exists(label.name)]

    seed_needed = (not exists) or _is_empty(gh, branch)
    if seed_needed and args.dry_run:
        state = di.RepoState()
    elif seed_needed:
        readme_commit = gh.seed_empty(
            di.SEED_PATH,
            rendered[di.SEED_PATH],
            f"onboarding: seed README from content-intelligence {short}",
            branch,
        )
        out(f"Seeded the empty repository ({readme_commit[:7]}).")
        state = di.read_state(gh, branch, rendered)
    else:
        state = di.read_state(gh, branch, rendered)

    plan = di.make_plan(
        rendered,
        state,
        source_full=full,
        seed=seed,
        overwrite_edited=args.overwrite_edited,
    )
    _print_plan(
        out,
        repo,
        branch,
        short,
        None,
        seed_needed and args.dry_run,
        plan,
        changes,
        [label.name for label in missing],
    )

    if args.dry_run:
        out("Dry run: nothing was written.")
        return 0
    # The commit goes first: a file the secret scan refuses, or a path that
    # changed under us, stops the run before the repository's settings are touched.
    if plan.needs_commit:
        result = gh.commit_files(
            branch,
            plan.files,
            di.commit_message(short, plan),
            replace=plan.expect,
        )
        out(
            f"Committed {len(result.written)} file(s): {result.html_url or result.commit_sha}"
        )
    _apply_settings(gh, changes, out)
    for label in missing:
        if gh.ensure_label(label.name, label.color, label.description):
            out(f"Created label {label.name}")
    return 0


def _apply_settings(gh, changes, out):
    """One ``PATCH`` when any setting differs; none when they are all as wanted."""
    if not changes:
        return
    after = gh.update_repo(**di.DOCUMENT_REPO_SETTINGS)
    left = di.settings_changes(after)
    if left:
        raise GitHubRepoError(
            "other",
            f"GitHub accepted the settings change but {gh.repo} still has "
            f"{di.format_settings(left)}. An organisation policy can override a "
            "repository's merge settings; check the repository's Settings page.",
        )
    out(f"Changed settings: {di.format_settings(changes)}")


def _create(gh, repo, run, sleep, out):
    try:
        status = _gh(run, "auth", "status")
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitHubRepoError(
            "other", f"cannot run gh to create the repository: {exc}"
        ) from exc
    if status.returncode != 0:
        raise GitHubRepoError(
            "auth", "gh is not logged in. Run `gh auth login`, then try again."
        )
    argv = _create_argv(repo)
    proc = run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise GitHubRepoError(
            "other",
            f"`gh repo create` failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}",
        )
    out(f"Created {repo} (private).")
    # The repository can take a moment to be readable through the REST API.
    for attempt in range(5):
        try:
            gh.info()
            return
        except GitHubRepoError as exc:
            if exc.kind != "not_found" or attempt == 4:
                raise
            sleep(1.0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
