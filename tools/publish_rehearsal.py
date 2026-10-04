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

Uploaded file bytes are written to ``--out-dir`` so they can be inspected --
``tools/image_metadata.py report`` is the companion for that. Headers are never
printed; the body of each POST is.
"""

import argparse
import json
import pathlib
import socket
import sys
from unittest.mock import patch

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO / "packages" / "ci-article-review" / "tests"


def _block_sockets():
    def refuse(*_a, **_k):
        raise AssertionError(
            "a socket was opened: this rehearsal must send nothing. Either a new "
            "code path reaches the network, or a fake is missing."
        )

    socket.socket.connect = refuse
    socket.create_connection = refuse


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
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
    parser.add_argument(
        "forward",
        nargs="*",
        help="flags to forward to ci-review, after a bare --",
    )
    args = parser.parse_args(argv)

    handoff = pathlib.Path(args.handoff).resolve()
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else handoff.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    _block_sockets()
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
    with (
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
    for created in fake.created:
        print("\npost body:")
        print(json.dumps(created, indent=2)[:2000])
    return 0 if code in (None, 0) else code


if __name__ == "__main__":
    sys.exit(main())
