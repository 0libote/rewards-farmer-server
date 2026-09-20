"""Shared pytest setup: keep module-level caches from leaking between tests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.account_checker as checker
import server.upstream_manager as um


@pytest.fixture(autouse=True)
def _clear_caches():
    checker.invalidate_account_cache()
    um.invalidate_upstream_info()
    yield
    checker.invalidate_account_cache()
    um.invalidate_upstream_info()
