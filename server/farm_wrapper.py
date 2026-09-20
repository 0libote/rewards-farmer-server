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

from balance_probe import extract_account_balance

try:
    import rewards_tasks
    import main
except ImportError as e:
    print(
        f"[FARM_WRAPPER] Could not import upstream modules from {UPSTREAM_SRC}: {e}\n"
        "[FARM_WRAPPER] The upstream repository layout may have changed; "
        "try 'Pull Latest' or restart the container.",
        file=sys.stderr,
        flush=True,
    )
    raise


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


def _report_balance(label: str, value: int | None) -> None:
    if value is not None:
        print(f"[POINTS] Balance {label}: {value} pts", flush=True)
    else:
        print(f"[POINTS] Balance {label}: unavailable (not signed in or header not hydrated)", flush=True)


def instrumented_complete_all(self):
    balance_before = None
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
                try:
                    self.switch_to_earn_page()
                except Exception:
                    # Earn tab click can go stale after the last task; reload
                    # is an equivalent clean state for reading the header.
                    try:
                        self.driver.get("https://rewards.bing.com/")
                        time.sleep(2)
                    except Exception:
                        pass
                time.sleep(3)
                balance_after = extract_account_balance(self.driver)
                if balance_after is not None:
                    print(f"[POINTS] Balance after run: {balance_after} pts", flush=True)
                    if balance_before is not None:
                        gained = max(0, balance_after - balance_before)
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
