"""Tests for persistent-data linking and the upstream info cache."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.upstream_manager as um


def test_sync_nouns_replaces_legacy_symlink_with_copy(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    data_nouns = tmp_path / "nouns.txt"
    data_nouns.write_text("persistent\n", encoding="utf-8")
    upstream_nouns = upstream / "nouns.txt"

    # Simulate an install created by an older version: nouns.txt is a symlink.
    upstream_nouns.symlink_to(data_nouns)

    monkeypatch.setattr(um, "UPSTREAM_DIR", upstream)
    monkeypatch.setattr(um, "NOUNS_FILE", data_nouns)

    um.sync_nouns_to_upstream()

    assert not upstream_nouns.is_symlink()
    assert upstream_nouns.read_text(encoding="utf-8") == "persistent\n"


def test_setup_symlinks_seeds_persistent_nouns(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "nouns.txt").write_text("seed\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setattr(um, "UPSTREAM_DIR", upstream)
    monkeypatch.setattr(um, "NOUNS_FILE", data_dir / "nouns.txt")
    monkeypatch.setattr(um, "PROFILES_DIR", data_dir / "data-dir")
    monkeypatch.setattr(um, "VISUAL_SEARCH_IMAGE", data_dir / "visual_search.jpg")

    um._setup_symlinks()

    assert (data_dir / "nouns.txt").read_text(encoding="utf-8") == "seed\n"
    # Tracked file stays a real file, never a symlink.
    assert not (upstream / "nouns.txt").is_symlink()
    # data-dir is gitignored upstream, so a symlink is safe and expected.
    assert (upstream / "data-dir").is_symlink()


def test_upstream_info_cached_until_invalidated(tmp_path, monkeypatch):
    monkeypatch.setattr(um, "UPSTREAM_DIR", tmp_path / "missing")
    um.invalidate_upstream_info()

    first = um.get_upstream_info()
    assert first == {"installed": False}

    # Creating a repo dir would change a fresh read, but the cache holds.
    (tmp_path / "missing").mkdir()
    (tmp_path / "missing" / ".git").mkdir()
    assert um.get_upstream_info() == {"installed": False}

    um.invalidate_upstream_info()
    assert um.get_upstream_info().get("installed") is True
