"""The tiktoken cache, built from litellm's own file, that lets litellm import offline.

packages/ci-core/conftest.py explains why it exists, and
pytest_plugins/offline_tiktoken.py builds it. CI runs on Linux, where litellm's
own wheel carries a good copy of this file, so nothing else in CI would notice
if the build rotted: the suite would stay green there and go red again only on
the next fresh Windows worktree. The CRLF case is therefore made here, from the
good file, rather than waited for.
"""

import os
import shutil
from pathlib import Path

import offline_tiktoken

_FILE = offline_tiktoken.CL100K_FILE


def _cache_dir():
    configured = os.environ.get("CUSTOM_TIKTOKEN_CACHE_DIR")
    assert configured, (
        "the conftest set no CUSTOM_TIKTOKEN_CACHE_DIR: litellm ships no cl100k_base "
        "that checks out against tiktoken's sha256, even with its line endings "
        "fixed (pytest_plugins/offline_tiktoken.py)"
    )
    return Path(configured)


def test_tiktoken_loads_cl100k_base_from_the_prepared_cache(tmp_path, monkeypatch):
    """tiktoken's own pinned sha256 is the arbiter here, not a copy of it.

    The load reads from a copy of the cache because tiktoken deletes a cached
    file that fails its check before it tries to download a replacement. The
    download is refused here rather than left to the socket guard, which
    ``--force-enable-socket`` turns off: the README suggests that flag for
    diagnosing guard trips, and under it a rotted file would be replaced from
    the network and this test would pass.
    """
    cache = tmp_path / "tiktoken_cache"
    shutil.copytree(_cache_dir(), cache)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(cache))

    import tiktoken.load
    from tiktoken_ext import openai_public

    def _no_download(url):
        raise AssertionError(
            "tiktoken rejected the prepared cl100k_base (missing, or failed its "
            f"sha256 check) and tried to download {url}"
        )

    monkeypatch.setattr(tiktoken.load, "read_file", _no_download)

    assert len(openai_public.cl100k_base()["mergeable_ranks"]) == 100_256


def test_the_pinned_hash_is_the_one_tiktoken_checks():
    """The prepared file passes tiktoken's check, so the constant that gates it
    here agrees with the one tiktoken holds."""
    data = (_cache_dir() / _FILE).read_bytes()
    assert offline_tiktoken._sha256(data) == offline_tiktoken.CL100K_SHA256


def test_a_crlf_copy_is_put_back_the_way_it_was_downloaded(tmp_path):
    """The Windows wheel's file: the same bytes with every line ending doubled."""
    good = (_cache_dir() / _FILE).read_bytes()
    crlf = good.replace(b"\n", b"\r\n")
    assert len(crlf) == 1_781_382  # what the Windows wheel's file measures

    source = tmp_path / "wheel"
    source.mkdir()
    (source / _FILE).write_bytes(crlf)
    cache = tmp_path / "cache"

    assert offline_tiktoken.prepare_cache(source, cache) == cache
    assert (cache / _FILE).read_bytes() == good


def test_a_file_that_does_not_check_out_is_refused(tmp_path):
    """Corrupt, or the wrong file entirely: nothing is written, and the caller
    is told so, which leaves tiktoken to its own check rather than ours."""
    source = tmp_path / "wheel"
    source.mkdir()
    (source / _FILE).write_bytes(b"IQ== 0\nIg== 1\n")
    cache = tmp_path / "cache"

    assert offline_tiktoken.prepare_cache(source, cache) is None
    assert not cache.exists()


def test_a_missing_source_is_refused(tmp_path):
    assert offline_tiktoken.prepare_cache(tmp_path / "nowhere", tmp_path / "c") is None


def test_a_rotted_cache_entry_is_replaced(tmp_path):
    good = (_cache_dir() / _FILE).read_bytes()
    source = tmp_path / "wheel"
    source.mkdir()
    (source / _FILE).write_bytes(good)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / _FILE).write_bytes(b"left behind by something else")

    assert offline_tiktoken.prepare_cache(source, cache) == cache
    assert (cache / _FILE).read_bytes() == good


def test_the_file_is_not_kept_in_the_repository():
    """OpenAI's vocabulary file carries no licence this project can pass on, so
    it is derived from litellm's copy at test time and never committed."""
    vendored = Path(__file__).parent / "fixtures" / "tiktoken_cache"
    assert not vendored.exists(), (
        f"{vendored} is back: build the cache with pytest_plugins/offline_tiktoken.py "
        "instead"
    )
