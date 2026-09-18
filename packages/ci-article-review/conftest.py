"""Test setup for ci-article-review: litellm must import without the network.

This suite blocks the network. The whole story, including why this file is not
in tests/, is in packages/ci-core/conftest.py, which sets the same two
variables. The suite's fixtures are in tests/conftest.py as usual; pytest loads
this file first, so these are set before anything there can import litellm.
"""

import os
from pathlib import Path

os.environ["CUSTOM_TIKTOKEN_CACHE_DIR"] = str(
    Path(__file__).resolve().parents[1] / "ci-core/tests/fixtures/tiktoken_cache"
)
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
