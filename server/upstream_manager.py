import os
import shutil
import subprocess
import time
from functools import lru_cache
from typing import Dict, Any, Optional, Tuple
from server.config import UPSTREAM_DIR, DATA_DIR, PROFILES_DIR, VISUAL_SEARCH_IMAGE, NOUNS_FILE

UPSTREAM_REPO_URL = os.getenv(
    "UPSTREAM_REPO_URL", "https://github.com/User0332/rewards-farmer.git"
)

VISUAL_SEARCH_FILENAME = "visual_search.jpg"

# get_upstream_info shells out to git three times and is called on every
# /api/status poll. Cache briefly; clone/pull invalidate it.
_UPSTREAM_INFO_TTL_SECONDS = 10.0
_upstream_info_cache: Optional[Tuple[float, Dict[str, Any]]] = None


def invalidate_upstream_info() -> None:
    global _upstream_info_cache
    _upstream_info_cache = None


@lru_cache(maxsize=1)
def get_edge_version() -> str:
    """Returns the installed Microsoft Edge browser version (cached: it can't
    change without a container rebuild)."""
    for bin_name in ["microsoft-edge", "microsoft-edge-stable"]:
        try:
            out = subprocess.check_output([bin_name, "--version"], stderr=subprocess.DEVNULL, text=True)
            return out.strip()
        except Exception:
            continue
    return "Microsoft Edge (not detected)"


def ensure_upstream() -> Dict[str, Any]:
    """Ensures upstream repo is cloned and symlinks/directories are configured."""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)

    if not UPSTREAM_DIR.exists() or not (UPSTREAM_DIR / ".git").exists():
        print(f"Cloning upstream repository from {UPSTREAM_REPO_URL} into {UPSTREAM_DIR}...")
        UPSTREAM_DIR.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = subprocess.run(
                ["git", "clone", "--depth", "1", UPSTREAM_REPO_URL, str(UPSTREAM_DIR)],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            print("Timed out cloning upstream repository (120s).")
            return {"success": False, "error": "Timed out cloning upstream repository"}
        if res.returncode != 0:
            print(f"Failed to clone upstream repo: {res.stderr}")
            return {"success": False, "error": res.stderr}
        print("Upstream repo successfully cloned.")
        invalidate_upstream_info()

    # Setup symlinks in upstream dir so upstream's relative paths find persistent data
    _setup_symlinks()

    return get_upstream_info()


def _setup_symlinks():
    """Ensure data-dir and visual_search.jpg point to persistent data, and copy
    the wordlist into upstream.

    nouns.txt is deliberately copied rather than symlinked: upstream tracks it
    in git, and replacing a tracked file with a symlink creates a typechange
    that makes `git pull --ff-only` fail. The visual image and data-dir are
    gitignored, so symlinks there are safe.
    """
    if not UPSTREAM_DIR.exists():
        return

    upstream_data_dir = UPSTREAM_DIR / "data-dir"
    if not upstream_data_dir.exists():
        try:
            upstream_data_dir.symlink_to(PROFILES_DIR.resolve(), target_is_directory=True)
        except Exception as e:
            print(f"Symlink error for data-dir: {e}")

    # nouns.txt
    upstream_nouns = UPSTREAM_DIR / "nouns.txt"
    if upstream_nouns.is_symlink():
        # Migrate installs created by older versions that symlinked it.
        try:
            upstream_nouns.unlink()
        except OSError as e:
            print(f"Could not remove legacy nouns.txt symlink: {e}")
    if not NOUNS_FILE.exists() and upstream_nouns.is_file():
        # Seed the persistent copy from upstream's shipped wordlist.
        try:
            shutil.copyfile(upstream_nouns, NOUNS_FILE)
        except OSError as e:
            print(f"Error copying initial nouns.txt: {e}")
    sync_nouns_to_upstream()

    # visual_search.jpg (gitignored upstream, symlink is safe)
    upstream_visual = UPSTREAM_DIR / VISUAL_SEARCH_FILENAME
    if VISUAL_SEARCH_IMAGE.exists() and not upstream_visual.is_symlink():
        try:
            if upstream_visual.exists():
                upstream_visual.unlink()
            upstream_visual.symlink_to(VISUAL_SEARCH_IMAGE.resolve())
        except Exception as e:
            print(f"Symlink error for visual_search.jpg: {e}")


def sync_nouns_to_upstream() -> None:
    """Copy the persistent wordlist over upstream's tracked nouns.txt.

    Upstream reads REPO_ROOT/nouns.txt at runtime, so the dashboard's edits must
    land there. A copy keeps the persistent file authoritative without breaking
    git updates the way a symlink would.
    """
    upstream_nouns = UPSTREAM_DIR / "nouns.txt"
    if not NOUNS_FILE.exists() or not UPSTREAM_DIR.exists():
        return
    try:
        if upstream_nouns.is_symlink():
            upstream_nouns.unlink()
        shutil.copyfile(NOUNS_FILE, upstream_nouns)
    except OSError as e:
        print(f"Error syncing nouns.txt to upstream: {e}")


def link_persistent_data() -> None:
    """Re-apply persistent-data links after the dashboard writes a data file."""
    _setup_symlinks()


def update_upstream() -> Dict[str, Any]:
    """Pulls the latest commits from upstream repository."""
    if not UPSTREAM_DIR.exists() or not (UPSTREAM_DIR / ".git").exists():
        return ensure_upstream()

    try:
        # Our copied nouns.txt shows as a local modification of a tracked file,
        # which makes a fast-forward pull refuse. Discard it first; the
        # persistent copy is re-applied by _setup_symlinks below.
        subprocess.run(
            ["git", "-C", str(UPSTREAM_DIR), "checkout", "--", "nouns.txt"],
            capture_output=True,
            text=True,
        )
        res = subprocess.run(
            ["git", "-C", str(UPSTREAM_DIR), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        invalidate_upstream_info()
        _setup_symlinks()
        return {
            "success": res.returncode == 0,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "info": get_upstream_info(),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Timed out pulling upstream (60s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_upstream_info(use_cache: bool = True) -> Dict[str, Any]:
    """Cached view of the local upstream clone; see _read_upstream_info."""
    global _upstream_info_cache
    now = time.monotonic()
    if (
        use_cache
        and _upstream_info_cache is not None
        and now - _upstream_info_cache[0] < _UPSTREAM_INFO_TTL_SECONDS
    ):
        return _upstream_info_cache[1]
    info = _read_upstream_info()
    _upstream_info_cache = (now, info)
    return info


def _read_upstream_info() -> Dict[str, Any]:
    """Gets commit hash, author, date, and commit message of the local upstream clone."""
    if not UPSTREAM_DIR.exists() or not (UPSTREAM_DIR / ".git").exists():
        return {"installed": False}

    try:
        commit_hash = subprocess.check_output(
            ["git", "-C", str(UPSTREAM_DIR), "rev-parse", "HEAD"], text=True
        ).strip()
        commit_msg = subprocess.check_output(
            ["git", "-C", str(UPSTREAM_DIR), "log", "-1", "--pretty=%B"], text=True
        ).strip()
        commit_date = subprocess.check_output(
            ["git", "-C", str(UPSTREAM_DIR), "log", "-1", "--pretty=%cd", "--date=relative"], text=True
        ).strip()
        return {
            "installed": True,
            "commit_hash": commit_hash[:8],
            "full_hash": commit_hash,
            "commit_msg": commit_msg,
            "commit_date": commit_date,
            "repo_url": UPSTREAM_REPO_URL,
        }
    except Exception as e:
        return {"installed": True, "error": str(e)}


def generate_visual_search_image() -> Dict[str, Any]:
    """Runs upstream's random_image_for_visual_search.py script to fetch a random image."""
    script_path = UPSTREAM_DIR / "src" / "random_image_for_visual_search.py"
    if not script_path.exists():
        return {"success": False, "error": "Upstream visual search generator script not found."}

    try:
        res = subprocess.run(
            ["python3", str(script_path)],
            cwd=str(UPSTREAM_DIR),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "Unknown error").strip()
            return {"success": False, "error": detail}

        # The upstream script writes next to itself or the repo root; persist
        # whatever it produced into the data volume so it survives restarts.
        candidates = [
            UPSTREAM_DIR / VISUAL_SEARCH_FILENAME,
            UPSTREAM_DIR / "src" / VISUAL_SEARCH_FILENAME,
        ]
        generated = next((p for p in candidates if p.exists() and p.is_file()), None)
        if generated is None:
            return {"success": False, "error": f"Generator ran but no {VISUAL_SEARCH_FILENAME} was produced."}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if generated.resolve() != VISUAL_SEARCH_IMAGE.resolve():
            shutil.copy2(str(generated), str(VISUAL_SEARCH_IMAGE))
        _setup_symlinks()
        return {
            "success": True,
            "message": "Visual search image generated successfully.",
            "size_bytes": VISUAL_SEARCH_IMAGE.stat().st_size,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
