"""Read the Microsoft Rewards point balance from the live dashboard.

Kept separate from farm_wrapper so it can be unit tested without importing the
upstream project, and so the DOM heuristics have one obvious home.
"""
import re
import time

# A real daily run earns at most a few hundred points. Anything larger is a
# misread (for example "up to 15,000 points" on a promo), so values above this
# are treated as unknown rather than recorded.
MAX_PLAUSIBLE_RUN_GAIN = 1000


def on_rewards_page(driver) -> bool:
    """True only when the browser is actually on the Rewards dashboard.

    An unsigned-in profile is redirected to login.live.com, whose header also
    contains small numbers; without this guard the balance probe happily
    reports one of those as the account balance.
    """
    try:
        return "rewards.bing.com" in (driver.current_url or "").lower()
    except Exception:
        return False


def extract_account_balance(driver, attempts: int = 3) -> int | None:
    """Extract total Microsoft Rewards point balance from the dashboard.

    Retries with small backoff: the React header hydrates progressively and a
    single 1.5s snapshot regularly reads the pre-hydration DOM (empty) and
    reports None, leaving history with null balances.
    """
    if not on_rewards_page(driver):
        return None

    js_candidates = [
        # 1. The number labelled "Available points" on the dashboard. On the
        # Earn/other pages that label is absent, so this correctly finds
        # nothing rather than grabbing a promo like "up to 15,000 points".
        r"""
        try {
            let txt = document.body ? (document.body.innerText || '') : '';
            let m = txt.match(/available\s*points[\s\S]{0,24}?([0-9][0-9,]{1,})/i)
                 || txt.match(/([0-9][0-9,]{1,})[\s\S]{0,24}?available\s*points/i);
            if (m) {
                let val = parseInt(m[1].replace(/,/g, ''), 10);
                if (val >= 0 && val < 5000000) return val;
            }
        } catch(e) {}
        return null;
        """,
        # 2. aria-labels / titles that name the balance specifically.
        r"""
        try {
            let els = document.querySelectorAll('[aria-label], [title]');
            for (let el of els) {
                let txt = ((el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).trim();
                if (!/available|balance|total|your\s*points/i.test(txt)) continue;
                let m = txt.match(/([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)/);
                if (m) {
                    let val = parseInt(m[1].replace(/,/g, ''), 10);
                    if (val >= 0 && val < 5000000) return val;
                }
            }
        } catch(e) {}
        return null;
        """,
        # 3. Plus/streak/bonus widgets show "N points" prominently and sit
        # before the balance in the DOM on some variants. Only accept a
        # comma-grouped number here so small promos are ignored.
        r"""
        try {
            let txt = document.body ? (document.body.innerText || '') : '';
            let m = txt.match(/([0-9]{1,3}(?:,[0-9]{3})+)\s*points/i);
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
