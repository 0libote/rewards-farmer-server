"""Farm execution wrapper with raw telemetry balance extraction.

Runs upstream rewards-farmer without modifying any upstream files,
measuring total Microsoft Rewards points balance before and after the run.
"""
import os
import re
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


def extract_account_balance(driver) -> int | None:
    """Extracts total Microsoft Rewards point balance from dashboard."""
    try:
        # 1. Execute JS to extract points from modern React dashboard
        js_code = """
        try {
            // Check points in global state or header
            for (let el of document.querySelectorAll('header *, [class*="points"], [id*="points"], [class*="balance"], [class*="status"]')) {
                let txt = (el.innerText || el.textContent || '').trim();
                // Match numbers like 14,250 or 520
                if (/^[0-9]{1,3}(,[0-9]{3})*$/.test(txt)) {
                    let val = parseInt(txt.replace(/,/g, ''), 10);
                    if (val >= 0 && val < 5000000) return val;
                }
            }
        } catch(e) {}
        return null;
        """
        val = driver.execute_script(js_code)
        if val is not None and isinstance(val, int):
            return val
    except Exception:
        pass

    try:
        # 2. Text regex on page source / body
        body_text = driver.find_element("tag name", "body").text or ""
        # Look for "Available points" or "Total points"
        m = re.search(r"(?:available\s*points|total\s*points|your\s*points)[:\s]*([0-9,]+)", body_text, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception:
        pass

    return None


# Wrap RewardsTaskUtils.complete_all_tasks to record raw balance
original_complete_all = rewards_tasks.RewardsTaskUtils.complete_all_tasks


def instrumented_complete_all(self):
    balance_before = None
    try:
        # Give page a second to hydrate points header
        time.sleep(1.5)
        balance_before = extract_account_balance(self.driver)
        if balance_before is not None:
            print(f"[POINTS] Balance before run: {balance_before} pts", flush=True)
    except Exception:
        pass

    try:
        original_complete_all(self)
    finally:
        try:
            if not hasattr(self, "switch_to_earn_page"):
                print("[POINTS] Skipping balance-after check: upstream helper not found.", flush=True)
            else:
                self.switch_to_earn_page()
                time.sleep(2)
                balance_after = extract_account_balance(self.driver)
                if balance_after is not None:
                    print(f"[POINTS] Balance after run: {balance_after} pts", flush=True)
                    if balance_before is not None:
                        gained = max(0, balance_after - balance_before)
                        print(f"[POINTS] Raw points earned this run: +{gained} pts", flush=True)
        except Exception:
            pass


rewards_tasks.RewardsTaskUtils.complete_all_tasks = instrumented_complete_all

if __name__ == "__main__":
    sys.exit(main.main())
