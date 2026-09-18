"""Test setup for ci-core: litellm must import without the network.

``import litellm`` loads tiktoken's ``cl100k_base`` encoding as a side effect
(``litellm/litellm_core_utils/default_encoding.py``). litellm bundles that file
so the load can happen offline, and points tiktoken's cache at its own copy.
But the copy in litellm's *Windows* wheel has CRLF line endings — 1,781,382
bytes where the real file is 1,681,126 — and tiktoken sha256-checks every
cached file. It rejects that one, deletes it, and downloads the original from
openaipublic.blob.core.windows.net. Under ``--disable-socket`` that download
raises ``SocketConnectBlockedError``, the import fails, and so does every test
that touches ``client.litellm``. Linux wheels are unaffected, which is why CI
never saw it; a venv that has downloaded the file once passes too, which is why
only fresh worktrees did.

So this points litellm at a byte-exact copy vendored in
``tests/fixtures/tiktoken_cache/``, named the way tiktoken names cache entries
(the SHA-1 of its download URL). ``CUSTOM_TIKTOKEN_CACHE_DIR`` is the one
override litellm honours: setting ``TIKTOKEN_CACHE_DIR`` does nothing, because
litellm overwrites it on import. ``.gitattributes`` marks the file ``binary`` so
that a Windows checkout cannot give it the CRLF endings that broke litellm's.

``LITELLM_LOCAL_MODEL_COST_MAP`` closes the other network call the same import
makes: without it, every ``import litellm`` fetches the model cost map from
raw.githubusercontent.com, and falls back to the bundled copy only once that
fails. Nothing in this repo reads the map.

Both must be set before anything imports litellm, and that moment is often
collection: a test module that imports litellm at the top (ci-style-profile's
``test_callers.py`` does) is imported then. pytest-socket guards only a test's
setup and call, so collection used to run with the network open, which is how a
repo-wide run from a fresh venv used to pass: collection quietly downloaded the
file for everyone else. pytest_plugins/socket_guard.py now guards collection
too, so without these that import fails the run instead. Setting them in
``os.environ`` at conftest import covers collection, the tests, and the
subprocesses they spawn, which inherit the environment but not the guard.

Each package's root ``conftest.py`` sets the same two variables, for the same
reason each package's ``pyproject.toml`` repeats the socket guard: pytest reads
only the conftest files on the path to the tests it was given. The package root
is on that path in every invocation, and pytest imports a conftest there before
anything under ``tests/``, ``tests/conftest.py`` included. It is outside any
Python package, so pytest names it after its path and the three cannot collide.
"""

import os
from pathlib import Path

os.environ["CUSTOM_TIKTOKEN_CACHE_DIR"] = str(
    Path(__file__).resolve().parents[1] / "ci-core/tests/fixtures/tiktoken_cache"
)
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
