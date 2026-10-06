"""A byte-exact tiktoken cache, built from litellm's own copy, so litellm imports offline.

``import litellm`` loads tiktoken's ``cl100k_base`` encoding as a side effect
(``litellm/litellm_core_utils/default_encoding.py``). litellm bundles that file
so the load can happen offline, and points tiktoken's cache at its own copy.
But the copy in litellm's *Windows* wheel has CRLF line endings, 1,781,382 bytes
where the real file is 1,681,126, and tiktoken sha256-checks every cached file.
It rejects that one, deletes it, and downloads the original from
openaipublic.blob.core.windows.net, which the suite's socket guard refuses.

:func:`prepare_cache` takes litellm's bundled file, turns CRLF back into LF,
confirms the result against the sha256 tiktoken pins for ``cl100k_base``, and
writes it where ``CUSTOM_TIKTOKEN_CACHE_DIR`` can point. (``TIKTOKEN_CACHE_DIR``
does nothing, because litellm overwrites it on import; ``CUSTOM_TIKTOKEN_CACHE_DIR``
is the override it honours.) The file is never kept in this repository: it is
OpenAI's data with no licence of its own that this project could pass on, and
litellm already ships it. Linux wheels carry a good copy, so this changes nothing
there.

It returns ``None`` when litellm is not installed, ships no such file, or ships
one that does not check out even after the line endings are fixed. The caller
then leaves the variable unset, and
``packages/ci-core/tests/test_offline_tiktoken_cache.py`` says so.

This module is imported by each package's root ``conftest.py`` (the one place
that runs before anything can import litellm); ``pythonpath`` in every inifile
puts this directory on ``sys.path``.
"""

import hashlib
import importlib.util
import os
import tempfile
from pathlib import Path

#: What tiktoken names cl100k_base in its cache: the SHA-1 of its download URL.
CL100K_FILE = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"

#: The sha256 tiktoken pins for that file (``tiktoken_ext/openai_public.py``).
CL100K_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def litellm_tokenizers_dir() -> Path | None:
    """Where litellm keeps its bundled tokenizer files, found without importing it."""
    spec = importlib.util.find_spec("litellm")
    if spec is None or not spec.submodule_search_locations:
        return None
    root = Path(next(iter(spec.submodule_search_locations)))
    return root / "litellm_core_utils" / "tokenizers"


def default_cache_dir() -> Path:
    return Path(tempfile.gettempdir()) / "ci-tiktoken-cache"


def prepare_cache(
    source_dir: Path | None = None, cache_dir: Path | None = None
) -> Path | None:
    """Return a directory holding a verified ``cl100k_base``, or ``None``."""
    source_dir = source_dir or litellm_tokenizers_dir()
    if source_dir is None:
        return None
    try:
        data = (source_dir / CL100K_FILE).read_bytes()
    except OSError:
        return None
    data = data.replace(b"\r\n", b"\n")
    if _sha256(data) != CL100K_SHA256:
        return None

    cache_dir = cache_dir or default_cache_dir()
    dest = cache_dir / CL100K_FILE

    def _already_good() -> bool:
        try:
            return _sha256(dest.read_bytes()) == CL100K_SHA256
        except OSError:
            return False

    if _already_good():
        return cache_dir
    tmp = None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache_dir)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except OSError:
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)
        # Another session may hold the file open while it writes the same bytes.
        return cache_dir if _already_good() else None
    return cache_dir
