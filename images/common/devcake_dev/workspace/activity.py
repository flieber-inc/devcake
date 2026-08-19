"""Activity folder materialization (ADR-0014)."""
from __future__ import annotations

import base64
import os
import pathlib
import shutil
import subprocess
import sys

def _safe_activity_relpath(path: str):
    """Pinned mirror of app safe_activity_relpath (activity_payload.py):
    reject zip-slip / absolute / empty paths. Returns a posix-relative
    string or None. Guard: app/tests/test_safe_activity_relpath_pin.py
    (ADR-0034)."""
    if not path or not isinstance(path, str):
        return None
    raw = path.replace("\\", "/").strip()
    if not raw or raw.startswith("/") or raw.startswith("~"):
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    if len(parts) > 20 or any(len(p) > 200 for p in parts):
        return None
    return "/".join(parts)


def write_activity_payload(act: dict, dest: pathlib.Path) -> None:
    """ADR-0014 D3: materialize the activity payload into the folder —
    MISSION.md (when the app sent one; old apps don't), ACTIVITY.md, and
    every attachment. Paths may be nested (zip extracts under `{stem}/`);
    unsafe / escaping paths fall back to a basename or `attachment.bin`."""
    dest.mkdir(parents=True, exist_ok=True)
    dest_res = dest.resolve()
    if act.get("mission_md"):
        (dest / "MISSION.md").write_text(act["mission_md"])
    (dest / "ACTIVITY.md").write_text(act.get("activity_md", ""))
    for a in act.get("attachments", []):
        raw = a.get("filename") or "attachment.bin"
        rel = _safe_activity_relpath(raw)
        if rel is None:
            rel = pathlib.Path(str(raw).replace("\\", "/")).name
            if not rel or rel in (".", ".."):
                rel = "attachment.bin"
        target = (dest / rel).resolve()
        # zip-slip: must stay under dest
        try:
            target.relative_to(dest_res)
        except ValueError:
            target = dest_res / "attachment.bin"
        data = base64.b64decode(a["content_b64"])
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError:
            # file-vs-directory collision (an old app can still send a flat
            # name and a same-named extraction dir) or any other tree
            # conflict: flatten — the mirror is advisory and must never
            # kill the run
            try:
                (dest_res / ("conflict-" + rel.replace("/", "__"))
                 ).write_bytes(data)
            except OSError:
                print(f"activity attachment skipped (unwritable): {rel}",
                      file=sys.stderr)


def clone_activity_repo(activity, dest, runner=None):
    """ADR-0014 D4: clone the mission's activity repo — FULL history (the
    step-by-step evolution IS the payload: `git log -p ACTIVITY.md` works
    in-container) with the shared RO token via the askpass env override.
    Non-fatal on every failure; (ok, note)."""
    import re as _re
    if not activity or not activity.get("url"):
        return False, "activity repo: no clone spec (Redis fallback)"
    runner = runner or subprocess.run
    url = activity["url"]
    user = activity.get("clone_user") or ""
    clone_url = (_re.sub(r"^(https?://)", rf"\g<1>{user}@", url)
                 if user else url)
    env = {**os.environ, "DEVCAKE_FORGE_TOKEN": activity.get("token") or ""}
    r = runner(["git", "clone", clone_url, str(dest)],
               capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return False, ("activity repo: clone failed "
                       f"({(r.stderr or '')[-200:]}) — Redis fallback")
    return True, "activity repo: cloned with history"


def materialize_activity(spec, dest, request_reply, runner=None):
    """Clone-first activity materialization (ADR-0014 D4), Redis fallback:
    activity.get + payload write when the clone failed OR left no
    ACTIVITY.md (empty repo — the first push failed; cloning an empty repo
    succeeds). Never fatal, never exit 13 (that's the primary repo's)."""
    notes = []
    ok, note = clone_activity_repo(spec.get("activity_repo"), dest,
                                   runner=runner)
    notes.append(note)
    if not ok or not (dest / "ACTIVITY.md").exists():
        if ok:
            notes.append("activity repo: empty clone — Redis fallback")
        # drop any zero-commit .git so `git log` inside the folder fails
        # honestly instead of confusingly ("no commits yet" over real files)
        shutil.rmtree(dest / ".git", ignore_errors=True)
        act = request_reply("activity.get", "activity.result")
        write_activity_payload(act, dest)
    return notes


