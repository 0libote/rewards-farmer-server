"""Run upstream's selector check against one profile (subprocess entry point).

The server runs this as a subprocess so the browser work is isolated and its
stdout can be captured verbatim. `REWARDS_CHECK_PROFILE_DIR` selects the profile
so named accounts work too; upstream's checker otherwise always uses the
data-dir root.
"""
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
UPSTREAM_SRC = SCRIPT_DIR.parent / "upstream" / "src"
if not UPSTREAM_SRC.exists():
    UPSTREAM_SRC = Path("/app/upstream/src")

if str(UPSTREAM_SRC) not in sys.path:
    sys.path.insert(0, str(UPSTREAM_SRC))

# Match upstream's own entrypoints: load an upstream .env if present. Existing
# environment variables win, so the server's settings are not overridden.
try:
    from dotenv import load_dotenv
    from constants import DOTENV_PATH

    if os.path.isfile(DOTENV_PATH):
        load_dotenv(DOTENV_PATH)
except Exception:
    pass

try:
    import check_selectors
except ImportError as e:
    print(
        f"[SELECTOR-CHECK] Could not import upstream check_selectors from {UPSTREAM_SRC}: {e}",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(3)

profile_dir = os.environ.get("REWARDS_CHECK_PROFILE_DIR", "").strip()
if profile_dir:
    # check_selectors builds its Account from these module globals.
    check_selectors.USER_DATA_DIR = profile_dir

if __name__ == "__main__":
    sys.exit(check_selectors.main())
