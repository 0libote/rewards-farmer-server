"""Regression test for the default-account profile path.

Upstream maps every REWARDS_ACCOUNTS entry to a subdirectory, so "default"
became <data-dir>/default. The dashboard stores the default profile at the
data-dir root (where Interactive Login writes it), so automation opened an
empty, signed-out profile and every task SKIPped. farm_wrapper rewrites that
one case.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = "/app/upstream/data-dir"


class FakeAccount:
    def __init__(self, name, user_data_dir, profile_name):
        self.name = name
        self.user_data_dir = user_data_dir
        self.profile_name = profile_name

    def __eq__(self, other):
        return (
            self.name,
            self.user_data_dir,
            self.profile_name,
        ) == (other.name, other.user_data_dir, other.profile_name)


@pytest.fixture
def farm_wrapper(monkeypatch):
    accounts = types.ModuleType("accounts")
    accounts.USER_DATA_DIR = ROOT
    accounts.PROFILE_NAME = "Default"
    accounts.Account = FakeAccount
    accounts.configured = lambda: [
        FakeAccount("default", f"{ROOT}/default", "Default"),
        FakeAccount("spare", f"{ROOT}/spare", "Default"),
    ]

    rewards_tasks = types.ModuleType("rewards_tasks")
    rewards_tasks.RewardsTaskUtils = type(
        "RewardsTaskUtils", (), {"complete_all_tasks": lambda self: None}
    )

    main = types.ModuleType("main")
    main.main = lambda: 0

    monkeypatch.setitem(sys.modules, "accounts", accounts)
    monkeypatch.setitem(sys.modules, "rewards_tasks", rewards_tasks)
    monkeypatch.setitem(sys.modules, "main", main)

    import server.farm_wrapper as module

    monkeypatch.delitem(sys.modules, "server.farm_wrapper", raising=False)
    return module


def test_default_account_uses_root_profile(farm_wrapper):
    result = farm_wrapper._configured_with_root_default()
    default = next(a for a in result if a.name == "default")
    assert default.user_data_dir == ROOT


def test_named_accounts_are_unchanged(farm_wrapper):
    result = farm_wrapper._configured_with_root_default()
    spare = next(a for a in result if a.name == "spare")
    assert spare.user_data_dir == f"{ROOT}/spare"
