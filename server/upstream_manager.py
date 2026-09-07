import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Dict, Any
from server.config import UPSTREAM_DIR, DATA_DIR, PROFILES_DIR, VISUAL_SEARCH_IMAGE, NOUNS_FILE

UPSTREAM_REPO_URL = os.getenv(
    "UPSTREAM_REPO_URL", "https://github.com/User0332/rewards-farmer.git"
)


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
        res = subprocess.run(
            ["git", "clone", "--depth", "1", UPSTREAM_REPO_URL, str(UPSTREAM_DIR)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            print(f"Failed to clone upstream repo: {res.stderr}")
            return {"success": False, "error": res.stderr}
        print("Upstream repo successfully cloned.")

    # Setup symlinks in upstream dir so upstream's relative paths find persistent data
    _setup_symlinks()

    return get_upstream_info()


def _setup_symlinks():
    """Ensure data-dir, visual_search.jpg, nouns.txt point to persistent data directory."""
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
    if not NOUNS_FILE.exists() and upstream_nouns.exists() and not upstream_nouns.is_symlink():
        # Copy initial default nouns.txt to persistent storage
        try:
            with open(upstream_nouns, "r", encoding="utf-8") as src, open(NOUNS_FILE, "w", encoding="utf-8") as dst:
                dst.write(src.read())
        except Exception as e:
            print(f"Error copying initial nouns.txt: {e}")

    if NOUNS_FILE.exists() and not upstream_nouns.is_symlink():
        try:
            if upstream_nouns.exists():
                upstream_nouns.unlink()
            upstream_nouns.symlink_to(NOUNS_FILE.resolve())
        except Exception as e:
            print(f"Symlink error for nouns.txt: {e}")

    # visual_search.jpg
    upstream_visual = UPSTREAM_DIR / "visual_search.jpg"
    if VISUAL_SEARCH_IMAGE.exists() and not upstream_visual.is_symlink():
        try:
            if upstream_visual.exists():
                upstream_visual.unlink()
            upstream_visual.symlink_to(VISUAL_SEARCH_IMAGE.resolve())
        except Exception as e:
            print(f"Symlink error for visual_search.jpg: {e}")


def update_upstream() -> Dict[str, Any]:
    """Pulls the latest commits from upstream repository."""
    if not UPSTREAM_DIR.exists() or not (UPSTREAM_DIR / ".git").exists():
        return ensure_upstream()

    try:
        res = subprocess.run(
            ["git", "-C", str(UPSTREAM_DIR), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        _setup_symlinks()
        return {
            "success": res.returncode == 0,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "info": get_upstream_info(),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_upstream_info() -> Dict[str, Any]:
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
            UPSTREAM_DIR / "visual_search.jpg",
            UPSTREAM_DIR / "src" / "visual_search.jpg",
        ]
        generated = next((p for p in candidates if p.exists() and p.is_file()), None)
        if generated is None:
            return {"success": False, "error": "Generator ran but no visual_search.jpg was produced."}
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
