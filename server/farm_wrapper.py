"""Farm execution wrapper with raw telemetry balance extraction.

Runs upstream rewards-farmer without modifying any upstream files,
measuring total Microsoft Rewards points balance before and after the run.
"""
import sys
import time
from pathlib import Path

# Add upstream/src to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
UPSTREAM_SRC = SCRIPT_DIR.parent / "upstream" / "src"
if not UPSTREAM_SRC.exists():
    UPSTREAM_SRC = Path("/app/upstream/src")

if UPSTREAM_SRC.exists():
    sys.path.insert(0, str(UPSTREAM_SRC))

# This file is executed as a script with its own directory as sys.path[0], but
# be explicit so the balance probe is importable either way.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from balance_probe import extract_account_balance, MAX_PLAUSIBLE_RUN_GAIN

try:
    import rewards_tasks
    import main
    import accounts as upstream_accounts
except ImportError as e:
    print(
        f"[FARM_WRAPPER] Could not import upstream modules from {UPSTREAM_SRC}: {e}\n"
        "[FARM_WRAPPER] The upstream repository layout may have changed; "
        "try 'Pull Latest' or restart the container.",
        file=sys.stderr,
        flush=True,
    )
    raise


# Upstream maps every name in REWARDS_ACCOUNTS to a subdirectory of the data
# dir, so "default" becomes <data-dir>/default. The dashboard stores the
# default account's profile at the data-dir root instead (that is where
# Interactive Login writes it), so passing "default" through untouched made
# automation open an empty, signed-out profile and every task SKIP. Rewrite
# just that case; named accounts already line up.
_original_configured = upstream_accounts.configured


def _configured_with_root_default():
    fixed = []
    for account in _original_configured():
        if (
            account.name == "default"
            and account.user_data_dir != upstream_accounts.USER_DATA_DIR
        ):
            fixed.append(
                upstream_accounts.Account(
                    name="default",
                    user_data_dir=upstream_accounts.USER_DATA_DIR,
                    profile_name=upstream_accounts.PROFILE_NAME,
                )
            )
        else:
            fixed.append(account)
    return fixed


upstream_accounts.configured = _configured_with_root_default


# Wrap RewardsTaskUtils.complete_all_tasks to record raw balance.
# Fail loudly and specifically if upstream renamed the hook, instead of a bare
# AttributeError traceback that reads like a wrapper bug.
_original_complete_all = getattr(
    getattr(rewards_tasks, "RewardsTaskUtils", None), "complete_all_tasks", None
)
if _original_complete_all is None:
    print(
        "[FARM_WRAPPER] Upstream RewardsTaskUtils.complete_all_tasks was not found; "
        "the upstream layout changed. Try 'Pull Latest' or update the wrapper.",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(3)

original_complete_all = _original_complete_all


# A real daily run earns at most a few hundred points. Anything larger is a
# misread (for example "up to 15,000 points" on a promo), so it is not reported
# as raw earnings. Constant lives in balance_probe so the parser and stats agree.
def _report_balance(label: str, value: int | None) -> None:
    if value is not None:
        print(f"[POINTS] Balance {label}: {value} pts", flush=True)
    else:
        print(f"[POINTS] Balance {label}: unavailable (not signed in or header not hydrated)", flush=True)


def instrumented_complete_all(self):
    balance_before = None

    # Upstream navigates to the Rewards root in __init__. For some sessions and
    # market variants that lands on a non-task page (observed: /about) instead
    # of the dashboard, after which every task reports ElementNeverAppeared and
    # SKIPs. Make sure we begin from the dashboard.
    try:
        time.sleep(2)
        current = self.driver.current_url or ""
        if "rewards.bing.com" in current and not any(
            path in current for path in ("/dashboard", "/earn")
        ):
            print(f"[POINTS] Rewards landed on {current}; opening the dashboard.", flush=True)
            self.driver.get("https://rewards.bing.com/dashboard")
            time.sleep(3)
    except Exception as exc:
        print(f"[POINTS] Dashboard recovery failed: {exc}", flush=True)

    try:
        # Points header hydrates progressively; give it time and retry inside
        # the extractor rather than a single snapshot.
        time.sleep(3)
        balance_before = extract_account_balance(self.driver)
        _report_balance("before run", balance_before)
    except Exception as exc:
        print(f"[POINTS] Balance-before check failed: {exc}", flush=True)

    try:
        original_complete_all(self)
    finally:
        try:
            if not hasattr(self, "switch_to_earn_page"):
                print("[POINTS] Skipping balance-after check: upstream helper not found.", flush=True)
            else:
                # Read the balance from the dashboard, which is where the
                # "Available points" label lives. Other pages carry promo
                # numbers ("up to 15,000 points") that the probe could mistake
                # for the balance.
                try:
                    self.driver.get("https://rewards.bing.com/dashboard")
                    time.sleep(2)
                except Exception:
                    pass
                time.sleep(3)
                balance_after = extract_account_balance(self.driver)
                if balance_after is not None:
                    print(f"[POINTS] Balance after run: {balance_after} pts", flush=True)
                    if balance_before is not None:
                        gained = balance_after - balance_before
                        if gained < 0 or gained > MAX_PLAUSIBLE_RUN_GAIN:
                            print(
                                f"[POINTS] Raw points earned this run: unknown "
                                f"(implausible balance delta {gained})",
                                flush=True,
                            )
                        else:
                            print(f"[POINTS] Raw points earned this run: +{gained} pts", flush=True)
                    else:
                        print("[POINTS] Raw points earned this run: unknown (no before-balance)", flush=True)
                else:
                    print("[POINTS] Balance after run: unavailable (not signed in or header not hydrated)", flush=True)
        except Exception as exc:
            print(f"[POINTS] Balance-after check failed: {exc}", flush=True)


rewards_tasks.RewardsTaskUtils.complete_all_tasks = instrumented_complete_all

if __name__ == "__main__":
    sys.exit(main.main())
