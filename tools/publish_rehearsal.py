"""Run the real ``ci-review --publish`` with every socket blocked.

``--replay`` verifies the review pipeline for free, but ``--publish`` POSTs to a
live site, so there was no safe way to exercise it before a commit: every
WordPress call is a real one, a media upload is public the moment it lands, and a
test post has to be cleaned up by hand afterwards. That made the one part of the
pipeline with irreversible side effects the one part nobody rehearsed.

This runs the actual CLI -- argument parsing, config loading, the handoff parser,
``run_publish_pipeline``, ``wp.push``, the block conversion, the lot -- against a
fake WordPress, with ``socket.connect`` and ``socket.create_connection`` replaced
by something that raises. The fake is the suite's own ``FakeWordPress``, imported
from ``packages/ci-article-review/tests/``, deliberately: a second fake of the
same API would be a second thing to keep true, and the point of this tool is that
the rehearsal and the tests agree about what WordPress does.

It sends nothing. The socket replacements are a tripwire, not a courtesy: if a
code path reaches the network, this fails loudly instead of quietly publishing.

    uv run python tools/publish_rehearsal.py HANDOFF --publication NAME \\
        --config-dir ../../configs [-- --publish-live ...]

Credentials are never read: ``--wp-user``/``--wp-password`` are forced to dummy
values, which beat the config file in this project's credential precedence. Any
flag after a bare ``--`` is forwarded to the CLI, so this is also how to rehearse
``--keep-image-metadata`` or ``--publish-live``.

Config loading resolves every ``${VAR}`` in ``--config-dir`` and stops at the
first one with no value, though ``--publish`` makes no model call. So each
placeholder found there that has no value gets a dummy before the CLI starts: a
worktree with a stub ``.env`` rehearses without hand-building a list of variables.
A variable that already has a value is left alone, and is not used for anything.

Two more things it does so that a rehearsal stays a rehearsal. The run's log goes
to a temporary directory (``CI_HISTORY_ROOT`` is pointed there for the run), not
to ``pipeline_history/`` or whatever ``CI_HISTORY_ROOT`` named, so a rehearsal
adds nothing to the real daily log. And no ``.env`` is loaded: ``find_dotenv`` and
``load_dotenv`` are replaced before the project is imported, because a worktree
inside the main checkout otherwise finds the real ``.env`` and puts every provider
key in the process's environment. Nothing would use one, but "never read" should
be true of all the credentials and not only the WordPress ones.

Uploaded file bytes are written to ``--out-dir`` so they can be inspected --
``tools/image_metadata.py report`` is the companion for that, and so is the body of
each post: the markup WordPress would have been sent goes to
``rehearsal-post-N.html`` in the same place, whole, because the terminal copy of a
long article was cut mid-paragraph. Headers are never printed; the other fields of
each POST are.
"""

import argparse
import json
import logging
import os
import pathlib
import socket
import sys
import tempfile
from unittest.mock import patch

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO / "packages" / "ci-article-review" / "tests"

DUMMY_ENV_VALUE = "rehearsal-dummy"


def _placeholder_names(node):
    """Every ``${NAME}`` in a loaded YAML tree, as the config loader reads them.

    Only a string that is wholly ``${NAME}`` is resolved (``resolve_env``), so a
    placeholder inside a longer string or a comment is not one and is not found.
    """
    if isinstance(node, dict):
        for value in node.values():
            yield from _placeholder_names(value)
    elif isinstance(node, list):
        for value in node:
            yield from _placeholder_names(value)
    elif isinstance(node, str) and node.startswith("${") and node.endswith("}"):
        yield node[2:-1]


def config_placeholders(config_dir):
    """The sorted names of every ``${VAR}`` in the YAML files under ``config_dir``.

    A file that does not parse is skipped: the CLI reports it, with its path, when
    it loads the same file.
    """
    names = set()
    for path in sorted(pathlib.Path(config_dir).rglob("*")):
        if path.suffix not in (".yaml", ".yml") or not path.is_file():
            continue
        try:
            tree = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        names.update(_placeholder_names(tree))
    return sorted(names)


def set_dummy_env(config_dir):
    """Give every placeholder under ``config_dir`` that has no value a dummy.

    Returns the names it set. Done before ``ci_article_review`` is imported, which
    takes its snapshot of the environment at import. A value already in the
    environment is left as it is, and nothing in a rehearsal sends a credential
    anywhere. No ``.env`` is loaded (``disable_dotenv``), so a variable a ``.env``
    would have supplied gets a dummy like any other.
    """
    names = [n for n in config_placeholders(config_dir) if n not in os.environ]
    for name in names:
        os.environ[name] = DUMMY_ENV_VALUE
    return names


def disable_dotenv():
    """Stop the project loading a ``.env``. Call before importing ``ci_article_review``.

    ``config_loader`` runs ``load_dotenv(find_dotenv())`` when it is imported, and
    ``find_dotenv`` walks up from the package file, so a worktree inside the main
    checkout reaches the real one. With both replaced it finds no file and loads
    nothing, which is what a checkout without a ``.env`` already does.
    """
    import dotenv

    dotenv.find_dotenv = lambda *_a, **_k: ""
    dotenv.load_dotenv = lambda *_a, **_k: False


def _block_sockets():
    def refuse(*_a, **_k):
        raise AssertionError(
            "a socket was opened: this rehearsal must send nothing. Either a new "
            "code path reaches the network, or a fake is missing."
        )

    socket.socket.connect = refuse
    socket.create_connection = refuse


def split_forward(argv):
    """Split ``argv`` at its first bare ``--``: ``(this tool's own args, forwarded)``.

    argparse is not asked to do it. With ``nargs="*"`` for the forwarded flags,
    CPython 3.12.3 (what CI runs) answers "unrecognized arguments: -- --flag" when
    the options come before the ``--``, and 3.13 does not; splitting first gives
    the same answer on every version.
    """
    argv = list(argv)
    if "--" not in argv:
        return argv, []
    at = argv.index("--")
    return argv[:at], argv[at + 1 :]


def main(argv=None):
    own, forward = split_forward(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        usage="%(prog)s HANDOFF --publication NAME [options] [-- ci-review flags ...]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("handoff", help="path to a publication handoff")
    parser.add_argument("--publication", required=True)
    parser.add_argument(
        "--config-dir",
        default="configs",
        help="a worktree has no configs/ of its own (it is git-ignored), so point "
        "this at the main checkout's",
    )
    parser.add_argument(
        "--out-dir",
        help="where to write the bytes of each upload (default: alongside the handoff)",
    )
    args = parser.parse_args(own)
    args.forward = forward

    handoff = pathlib.Path(args.handoff).resolve()
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else handoff.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    _block_sockets()
    disable_dotenv()
    dummied = set_dummy_env(args.config_dir)
    if dummied:
        print(f"dummy values set for unset config variables: {', '.join(dummied)}\n")
    sys.path.insert(0, str(TESTS))
    from test_wordpress_images import FakeWordPress  # noqa: E402

    from ci_article_review import pipeline  # noqa: E402

    fake = FakeWordPress()
    argv_cli = [
        "ci-review",
        "--publish",
        str(handoff),
        "--publication",
        args.publication,
        "--config-dir",
        args.config_dir,
        # Dummy credentials, highest precedence, so the real ones are never loaded.
        "--wp-user",
        "rehearsal-user",
        "--wp-password",
        "rehearsal-password",
        *args.forward,
    ]
    wp = "ci_article_review.adapters.cms.wordpress"
    code = None
    print(f"$ {' '.join(argv_cli)}\n")
    # The daily log is written under the history root, which is the real shared
    # one unless this says otherwise; a rehearsal's lines do not belong in it.
    with (
        tempfile.TemporaryDirectory(prefix="rehearsal-history-") as history,
        patch.dict(os.environ, {"CI_HISTORY_ROOT": history}),
        patch.object(sys, "argv", argv_cli),
        patch(f"{wp}.requests.post", fake.post),
        patch(f"{wp}.requests.get", fake.get),
        # The checklist asks on stdin; a rehearsal answers it.
        patch("builtins.input", lambda *_a: "yes"),
    ):
        try:
            pipeline.main()
        except SystemExit as e:
            code = e.code
        finally:
            # The file handler holds the log open, and Windows will not delete
            # a directory that has an open file in it.
            for handler in list(logging.getLogger().handlers):
                if isinstance(handler, logging.FileHandler):
                    handler.close()
                    logging.getLogger().removeHandler(handler)

    print(f"\n{'=' * 70}\nWHAT LEFT THE PROCESS (nothing did; this is what would have)")
    print("=" * 70)
    print(f"exit code:       {code}")
    print(f"requests:        {len(fake.requests)}")
    for method, url in fake.requests:
        print(f"  {method} {url}")
    for up in fake.uploads:
        target = out_dir / f"rehearsal-upload-{up['name']}"
        target.write_bytes(up["bytes"])
        print(f"\nupload {up['name']} ({up['mime']}, {len(up['bytes']):,} bytes)")
        print(f"  alt_text: {up['data'].get('alt_text')!r}")
        print(f"  bytes written to {target}")
    for n, created in enumerate(fake.created, 1):
        target = out_dir / f"rehearsal-post-{n}.html"
        body = created.get("content", "")
        target.write_text(body, encoding="utf-8")
        print(f"\npost {n}:")
        print(
            json.dumps({**created, "content": f"<{len(body):,} characters>"}, indent=2)
        )
        print(f"  content written to {target}")
    return 0 if code in (None, 0) else code


if __name__ == "__main__":
    sys.exit(main())
