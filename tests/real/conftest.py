"""Real-fetch test config: skip network tests by default; opt in via RUN_NETWORK_TESTS=1."""
import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_NETWORK_TESTS"):
        return
    skip = pytest.mark.skip(reason="network test; set RUN_NETWORK_TESTS=1 to run")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def is_finite():
    import math

    def _f(v):
        try:
            return math.isfinite(float(v))
        except (TypeError, ValueError):
            return False

    return _f
