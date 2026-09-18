"""Test setup for ci-style-profile: litellm must import without the network.

This suite blocks the network, and test_callers.py imports litellm during
collection, before pytest-socket's guard is even installed. The whole story,
including why this file is not in tests/, is in packages/ci-core/conftest.py,
which sets the same two variables.
"""

import os
from pathlib import Path

os.environ["CUSTOM_TIKTOKEN_CACHE_DIR"] = str(
    Path(__file__).resolve().parents[1] / "ci-core/tests/fixtures/tiktoken_cache"
)
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
