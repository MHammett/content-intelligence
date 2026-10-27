"""``ci-docs``: keep the private document repository self-describing.

``ci-docs init`` creates the GitHub repository that holds the articles when it
is missing (only with ``--create``), then commits the onboarding files and
templates rendered from sources packaged here, in one commit. Re-running it
refreshes them: a managed file is replaced only if nobody has edited it in the
document repository since the last run. The design is docs/GITHUB-DOCUMENTS.md;
what each file is for is in ``documents_init``.

    ci-docs init --dry-run                  # the plan; writes nothing
    ci-docs init --create                   # make the repository too, if missing
    ci-docs init --seed-instructions PATH   # first run: instructions/publication.md

The token is ``GITHUB_TOKEN``, else ``gh auth token``. It is never printed.
"""

import argparse
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
            "Render AGENTS.md, CLAUDE.md, README.md, .gitattributes, templates/ and "
            "instructions/claude-project.md from this repository's packaged sources "
            "and commit them to the document repository in one commit. A managed "
            "file is replaced only if its current blob is the one the manifest "
            "recorded. Nothing under articles/ and no existing "
            "instructions/publication.md is ever touched."
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
    return parser


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


def _print_plan(out, repo, branch, short, existence, seed_needed, plan):
    out(f"Repository: {repo}, branch {branch}")
    out(f"Rendered from content-intelligence {short}")
    if existence is not None:
        out(f"  {existence}")
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
    try:
        return _init(args, environ, run, session, out, sleep)
    except GitHubRepoError as exc:
        if exc.kind == "rate_limited" and exc.reset_at:
            when = time.strftime("%H:%M:%S", time.localtime(exc.reset_at))
            return _fail(f"{exc} The limit lifts at {when}.")
        return _fail(str(exc))


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
    articles_readme = di.render_articles_readme(repo, branch)

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
                articles_readme=articles_readme,
                seed=seed,
                overwrite_edited=args.overwrite_edited,
            )
            _print_plan(out, repo, branch, short, existence, seed_needed, plan)
            out("Dry run: nothing was created or written.")
            return 0
        if not args.create:
            out(f"{repo} does not exist, or this token cannot see it. To create it:")
            out(f"  {command}")
            out("or run this command again with --create. Nothing was created.")
            return 1
        _create(gh, repo, run, sleep, out)
        _check_access(gh, gh.info())

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
        articles_readme=articles_readme,
        seed=seed,
        overwrite_edited=args.overwrite_edited,
    )
    _print_plan(out, repo, branch, short, None, seed_needed and args.dry_run, plan)

    if args.dry_run:
        out("Dry run: nothing was written.")
        return 0
    if not plan.needs_commit:
        return 0

    result = gh.commit_files(
        branch,
        plan.files,
        di.commit_message(short, plan),
        replace=plan.expect,
    )
    out(
        f"Committed {len(result.written)} file(s): {result.html_url or result.commit_sha}"
    )
    return 0


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
