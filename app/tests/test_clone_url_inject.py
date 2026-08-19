"""Dev-side clone_user URL injection (http + https) — CAKE-79.

Public seam: ``devcake_dev.workspace.clone.inject_clone_user``. Primary
work-repo clone, activity clone, and sibling clones must share this
helper so scheme coverage cannot drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOTS = [Path(__file__).parents[2], Path(__file__).parents[1]]
IMAGES_COMMON = next(
    (r / "images" / "common" for r in _ROOTS
     if (r / "images" / "common" / "devcake_dev").is_dir()),
    _ROOTS[0] / "images" / "common")
if str(IMAGES_COMMON) not in sys.path:
    sys.path.insert(0, str(IMAGES_COMMON))

from devcake_dev.workspace.clone import inject_clone_user  # noqa: E402


def test_inject_clone_user_http_and_https():
    assert inject_clone_user("http://gitea:3000/o/r.git", "devcake") == \
        "http://devcake@gitea:3000/o/r.git"
    assert inject_clone_user("https://github.com/o/r.git", "x-access-token") == \
        "https://x-access-token@github.com/o/r.git"


def test_inject_clone_user_empty_user_leaves_url_unchanged():
    assert inject_clone_user("http://gitea:3000/o/r.git", "") == \
        "http://gitea:3000/o/r.git"
    assert inject_clone_user("https://github.com/o/r.git", "") == \
        "https://github.com/o/r.git"


def test_inject_clone_user_skips_non_http_schemes():
    # ssh / bare — leave unchanged (siblings historically only touched https?)
    assert inject_clone_user("git@github.com:o/r.git", "devcake") == \
        "git@github.com:o/r.git"
