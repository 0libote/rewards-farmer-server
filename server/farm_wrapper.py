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


def extract_account_balance(driver, attempts: int = 3) -> int | None:
    """Extract total Microsoft Rewards point balance from the dashboard.

    Retries with small backoff: the React header hydrates progressively and a
    single 1.5s snapshot regularly reads the pre-hydration DOM (empty) and
    reports None, leaving history with null balances.
    """
    js_candidates = [
        # 1. Header / points / balance elements with a bare number.
        """
        try {
            for (let el of document.querySelectorAll('header *, [class*="points"], [id*="points"], [class*="balance"], [class*="status"]')) {
                let txt = (el.innerText || el.textContent || '').trim();
                if (/^[0-9]{1,3}(,[0-9]{3})*$/.test(txt)) {
                    let val = parseInt(txt.replace(/,/g, ''), 10);
                    if (val >= 0 && val < 5000000) return val;
                }
            }
        } catch(e) {}
        return null;
        """,
        # 2. aria-labels like "Available points 4,491" on the rewards flyout.
        """
        try {
            let els = document.querySelectorAll('[aria-label*="point" i], [title*="point" i]');
            for (let el of els) {
                let txt = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '') + ' ' + (el.innerText || '')).trim();
                let m = txt.match(/([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)/);
                if (m) {
                    let val = parseInt(m[1].replace(/,/g, ''), 10);
                    if (val >= 0 && val < 5000000) return val;
                }
            }
        } catch(e) {}
        return null;
        """,
        # 3. Any "4,491 points" occurrence in rendered text.
        """
        try {
            let txt = document.body ? (document.body.innerText || '') : '';
            let m = txt.match(/([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{2,})\\s*points/i);
            if (m) {
                let val = parseInt(m[1].replace(/,/g, ''), 10);
                if (val >= 0 && val < 5000000) return val;
            }
        } catch(e) {}
        return null;
        """,
    ]

    for attempt in range(attempts):
        for js_code in js_candidates:
            try:
                val = driver.execute_script(js_code)
                if isinstance(val, int) and 0 <= val < 5000000:
                    return val
                # Selenium may return floats/longs for JS numbers.
                if isinstance(val, float) and val.is_integer() and 0 <= val < 5000000:
                    return int(val)
            except Exception:
                continue

        try:
            # 4. Text regex on body as last resort.
            body_text = driver.find_element("tag name", "body").text or ""
            for pat in (
                r"(?:available\s*points|total\s*points|your\s*points|rewards\s*points)[:\s]*([0-9,]+)",
                r"([0-9]{1,3}(?:,[0-9]{3})+)\s*points",
            ):
                m = re.search(pat, body_text, re.IGNORECASE)
                if m:
                    val = int(m.group(1).replace(",", ""))
                    if 0 <= val < 5000000:
                        return val
        except Exception:
            pass

        if attempt < attempts - 1:
            time.sleep(2)

    return None


# Wrap RewardsTaskUtils.complete_all_tasks to record raw balance
original_complete_all = rewards_tasks.RewardsTaskUtils.complete_all_tasks


def instrumented_complete_all(self):
    balance_before = None
    try:
        # Points header hydrates progressively; give it time and retry inside
        # the extractor rather than a single snapshot.
        time.sleep(3)
        balance_before = extract_account_balance(self.driver)
        if balance_before is not None:
            print(f"[POINTS] Balance before run: {balance_before} pts", flush=True)
        else:
            print("[POINTS] Balance before run: unavailable (header not hydrated)", flush=True)
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
                    print("[POINTS] Balance after run: unavailable (header not hydrated)", flush=True)
        except Exception as exc:
            print(f"[POINTS] Balance-after check failed: {exc}", flush=True)


rewards_tasks.RewardsTaskUtils.complete_all_tasks = instrumented_complete_all

if __name__ == "__main__":
    sys.exit(main.main())
