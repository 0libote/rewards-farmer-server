"""Tests for the Rewards balance probe heuristics (no upstream needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.balance_probe import extract_account_balance, on_rewards_page


class _El:
    def __init__(self, text):
        self.text = text


class FakeDriver:
    def __init__(self, url, script_result=None, body=""):
        self.current_url = url
        self._script_result = script_result
        self._body = body
        self.script_calls = 0

    def execute_script(self, code):
        self.script_calls += 1
        return self._script_result

    def find_element(self, by, value):
        return _El(self._body)


class ExplodingUrl:
    @property
    def current_url(self):
        raise RuntimeError("driver gone")


def test_on_rewards_page_detection():
    assert on_rewards_page(FakeDriver("https://rewards.bing.com/")) is True
    assert on_rewards_page(FakeDriver("https://login.live.com/")) is False
    assert on_rewards_page(ExplodingUrl()) is False


def test_off_rewards_page_does_not_probe_dom():
    # The regression this guards: a login page's header numbers being read as
    # the account balance.
    driver = FakeDriver("https://login.live.com/", script_result=40)
    assert extract_account_balance(driver) is None
    assert driver.script_calls == 0


def test_on_rewards_page_reads_balance():
    driver = FakeDriver("https://rewards.bing.com/", script_result=4491)
    assert extract_account_balance(driver) == 4491


def test_float_balance_is_coerced_to_int():
    driver = FakeDriver("https://rewards.bing.com/", script_result=1200.0)
    assert extract_account_balance(driver) == 1200


def test_body_text_fallback_used_when_dom_probe_fails():
    driver = FakeDriver("https://rewards.bing.com/", script_result=None, body="Available points 4,491")
    assert extract_account_balance(driver) == 4491


def test_out_of_range_value_ignored():
    driver = FakeDriver("https://rewards.bing.com/", script_result=999999999)
    assert extract_account_balance(driver, attempts=1) is None


class ConditionalDriver:
    """Returns different values depending on which candidate ran."""

    def __init__(self, url):
        self.current_url = url
        self.calls = []

    def execute_script(self, code):
        self.calls.append(code)
        # Simulate a dashboard that also renders a promo "15,000 points":
        # only the "available points" candidate should win.
        if "available" in code.lower():
            return 6028
        return 15000

    def find_element(self, by, value):
        return _El("")


def test_available_points_candidate_wins_over_promo():
    driver = ConditionalDriver("https://rewards.bing.com/dashboard")
    assert extract_account_balance(driver) == 6028
