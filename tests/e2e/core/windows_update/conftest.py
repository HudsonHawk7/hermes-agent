"""The Windows install/update E2E suite mutates HKCU Path on purpose and restores it."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items):
    mark = pytest.mark.allow_real_user_path
    for item in items:
        item.add_marker(mark)
