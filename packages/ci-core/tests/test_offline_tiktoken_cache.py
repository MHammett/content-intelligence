"""The vendored tiktoken cache that lets litellm import offline.

packages/ci-core/conftest.py explains why it exists. CI runs on Linux, where
litellm's own wheel carries a good copy of this file, so nothing else in CI
would notice if the vendored one rotted: the suite would stay green there and go
red again only on the next fresh Windows worktree.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# What tiktoken names cl100k_base in its cache: the SHA-1 of its download URL.
_CL100K_BASE_FILE = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"


def test_tiktoken_loads_cl100k_base_from_the_vendored_cache(tmp_path, monkeypatch):
    """tiktoken's own pinned sha256 is the arbiter here, not a copy of it.

    The load reads from a copy of the cache because tiktoken deletes a cached
    file that fails its check before it tries to download a replacement. The
    download is refused here rather than left to the socket guard, which
    ``--force-enable-socket`` turns off: the README suggests that flag for
    diagnosing guard trips, and under it a rotted file would be replaced from
    the network and this test would pass.
    """
    cache = tmp_path / "tiktoken_cache"
    shutil.copytree(os.environ["CUSTOM_TIKTOKEN_CACHE_DIR"], cache)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(cache))

    import tiktoken.load
    from tiktoken_ext import openai_public

    def _no_download(url):
        raise AssertionError(
            "tiktoken rejected the vendored cl100k_base (missing, or failed its "
            f"sha256 check) and tried to download {url}"
        )

    monkeypatch.setattr(tiktoken.load, "read_file", _no_download)

    assert len(openai_public.cl100k_base()["mergeable_ranks"]) == 100_256


def test_git_leaves_the_vendored_cache_line_endings_alone():
    """Without its ``binary`` attribute, Git for Windows (core.autocrlf=true by
    default) checks the file out with CRLF, the corruption that broke litellm's
    copy. CI checks out on Linux and would not notice, so assert it instead."""
    cache_dir = Path(os.environ["CUSTOM_TIKTOKEN_CACHE_DIR"])
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(cache_dir),
                "check-attr",
                "text",
                "--",
                _CL100K_BASE_FILE,
            ],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("no usable git")

    assert out.strip() == f"{_CL100K_BASE_FILE}: text: unset", out
