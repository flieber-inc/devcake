"""Activity-mirror payload building (ADR-0014 D3/D4).

Extracted from dispatch.py in the 2026-08 evaluation cleanups: this is pure
content transformation — MISSION.md/ACTIVITY.md rendering, attachment
materialization with the docs/07 §2 dedupe rules, zip-slip-hardened archive
expansion, and the per-step activity-repo snapshot — with no dispatch
coupling at all. Consumers: dispatch (pre-step snapshot push), manager
(the RunFinalizer activity_payload seam), tests.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime  # noqa: F401 — type context for Activity entries
from pathlib import Path

from ..model import MissionRef
from .feed import _is_devcake_comment

log = logging.getLogger("devcake.missions")


def _tree_conflict(cand: str, used: set[str]) -> bool:
    """True when `cand` cannot coexist with `used` as one file tree: exact
    duplicate, `cand` names a file where used paths already form a directory,
    or an ancestor directory of `cand` is already a file. A file and a
    directory sharing a name is unrepresentable in a git tree and crashes
    the entrypoint's mkdir/write."""
    if cand in used:
        return True
    pre = cand + "/"
    if any(u.startswith(pre) for u in used):
        return True
    parts = cand.split("/")
    return any("/".join(parts[:i]) in used for i in range(1, len(parts)))


def _unique_name(name: str, used: set[str]) -> str:
    """docs/07 §2 collision rule: later duplicates get -2, -3, … suffixes.
    A flat name also conflicts with an existing extraction DIRECTORY of the
    same name (file-vs-dir is unrepresentable — `_tree_conflict`); the
    suffix rule resolves that identically. Callers pass flat (basenamed)
    names; extraction paths are reserved directly by the expansion loop."""
    stem, dot, ext = name.rpartition(".")
    cand, i = name, 1
    while cand in used or any(u.startswith(cand + "/") for u in used):
        i += 1
        cand = f"{stem}-{i}.{ext}" if dot else f"{name}-{i}"
    used.add(cand)
    return cand


def safe_activity_relpath(path: str) -> str | None:
    """Normalize a feed-controllable relative path for the activity folder.
    Rejects empty, absolute, `..`, and over-deep/long names (zip-slip)."""
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


def expand_zip_attachment(zip_name: str, data: bytes, *,
                          max_bytes: int,
                          max_files: int = 500) -> list[tuple[str, bytes]]:
    """Extract zip members under `{stem}/…`. Best-effort: corrupt/oversize/
    slip members are skipped; never raises into the payload builder."""
    import io
    import zipfile
    from pathlib import PurePosixPath

    stem = PurePosixPath(zip_name.replace("\\", "/")).name
    if stem.lower().endswith(".zip"):
        stem = stem[:-4]
    stem = stem or "archive"
    # stem itself must be a single safe segment
    if safe_activity_relpath(stem) is None or "/" in stem:
        stem = "archive"
    out: list[tuple[str, bytes]] = []
    emitted: set[str] = set()
    used = 0
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        return []
    try:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if len(out) >= max_files:
                break
            member = safe_activity_relpath(info.filename)
            if member is None:
                continue
            full = safe_activity_relpath(f"{stem}/{member}")
            if full is None:
                continue
            # a crafted zip can hold both `x` and `x/y` (or duplicate
            # names) — later members that conflict with an already-emitted
            # path are dropped, keeping the extraction one valid file tree
            if _tree_conflict(full, emitted):
                continue
            # Pre-check declared uncompressed size before read — a zip bomb
            # must not force a multi-GB decompress into memory first. The
            # header can lie; the post-read check below is the hard stop.
            declared = getattr(info, "file_size", 0) or 0
            if declared < 0:
                continue
            if declared and used + declared > max_bytes:
                break
            try:
                content = zf.read(info)
            except Exception:  # noqa: BLE001 — one bad member must not abort the rest
                continue
            if used + len(content) > max_bytes:
                break
            used += len(content)
            emitted.add(full)
            out.append((full, content))
    finally:
        zf.close()
    return out


def _activity_snapshot_files(payload: dict) -> list[dict]:
    """Activity payload → the file list a snapshot commit mirrors
    (identical layout to the Dev's /workspace/activity). Attachment paths
    may be nested (zip extracts under `{stem}/…`); only safe relative
    paths are kept."""
    files = []
    if payload.get("mission_md"):
        files.append({"path": "MISSION.md", "content_b64": base64.b64encode(
            payload["mission_md"].encode()).decode()})
    files.append({"path": "ACTIVITY.md", "content_b64": base64.b64encode(
        payload.get("activity_md", "").encode()).decode()})
    for a in payload.get("attachments", []):
        raw = a.get("filename") or "attachment.bin"
        rel = safe_activity_relpath(raw)
        if rel is None:
            rel = Path(str(raw).replace("\\", "/")).name or "attachment.bin"
            if rel in (".", ".."):
                rel = "attachment.bin"
        files.append({"path": rel, "content_b64": a["content_b64"]})
    return files


async def push_activity_repo(mgr, mission, mtype, seq: int) -> None:
    """ADR-0014 D4: one snapshot commit per step dispatch. Any failure is
    audited loudly and swallowed — the run proceeds on the Redis fallback;
    Gitea down degrades to pre-ADR behavior, never to a halt."""
    if mgr.internal_forge is None:
        return
    try:
        payload = await activity_payload(mgr, mission.pmo_id, mission.pmo_kind)
        name = await mgr.internal_forge.ensure_activity_repo(
            mgr.instance_name, mission.key)
        await mgr.internal_forge.push_activity_snapshot(
            name, _activity_snapshot_files(payload),
            f"step {seq} {mtype.value} dispatch")
        log.info("activity repo %s: snapshot for step %d", name, seq)
    except Exception as e:
        log.exception("activity repo push failed for %s", mission.key)
        mgr._audit(mission.pmo_id, "activity_repo_push_failed",
                    f"{mission.key}: {type(e).__name__}: {str(e)[:180]}")


def _mission_md(m, attachment_lines=()) -> str:
    """ADR-0014 D3: MISSION.md — the brief. Stable regardless of feed length;
    every step playbook points here."""
    lines = [
        f"# {m.key}: {m.title}",
        f"> Kind: {m.pmo_kind} · Status: {m.status} · Priority: {m.priority} · URL: {m.url}",
        f"> Labels: {', '.join(sorted(m.labels)) or '(none)'}", "",
        "## Description", m.description or "(none)"]
    if attachment_lines:
        lines += ["", "## Mission attachments", *attachment_lines]
    return "\n".join(lines)


async def activity_payload(mgr, pmo_id: str, kind: str = "issue") -> dict:
    """ADR-0014 D3: MISSION.md = the brief; ACTIVITY.md = a faithful MIRROR
    of the feed — full bodies inline (never externalized), attachments by
    name in feed order, reply nesting; every attachment's bytes ride as
    sibling files."""
    if kind == "project":
        # projects have no comments/attachments: the brief IS the payload
        m = await mgr.pmo.get(MissionRef(pmo_id, "project"))
        md = "\n".join([
            f"# {m.key}: {m.title}",
            "> The mission brief lives in MISSION.md (same folder).", "",
            "## Activity", "(projects carry no comment feed — see child issues)"])
        return {"mission_md": _mission_md(m), "activity_md": md,
                "attachments": []}
    act = await mgr.pmo.get_activity(MissionRef(pmo_id, "issue"), full=True)
    m = act.mission
    attachments = []
    used: set[str] = {"ACTIVITY.md", "MISSION.md"}   # docs/07 §2 dedupe seed

    async def _materialize(att):
        """Download one file attachment into the folder; return its index
        line. The adapter resolves names (AttachmentRef.name) — the domain
        never parses vendor asset URLs. `.zip` attachments are kept whole
        and also extracted under `{stem}/` (zip-slip hardened, size-capped)."""
        try:
            data = await mgr.pmo.download_asset(att.url)
        except Exception:  # noqa: BLE001 — attachment fetch degrades to an inline "unavailable" marker; the mirror build continues
            return f"[attachment unavailable: {att.url}]"
        # basename BEFORE dedupe: a slash-bearing link text ([v1/r.md](…))
        # must yield the same name in the index, the snapshot commit, and
        # the folder — a path-y name would desync them and trip the
        # snapshot dup-path guard forever (full-diff review finding)
        raw = (Path(att.name).name if att.name
               else att.url.rsplit("/", 1)[-1][:80])
        fname = _unique_name(raw or "attachment.bin", used)
        attachments.append({"filename": fname,
                            "content_b64": base64.b64encode(data).decode()})
        if fname.lower().endswith(".zip"):
            try:
                cap = mgr._attachment_cap()
            except Exception:  # noqa: BLE001 — expand is advisory; fall back to a fixed budget
                cap = 50 * 1024 * 1024
            pairs = expand_zip_attachment(fname, data, max_bytes=cap)
            if pairs:
                # the extraction dir must not collide with an existing flat
                # name (file-vs-dir: unrepresentable in the snapshot's git
                # tree, and a crash in the entrypoint's mkdir) — remap the
                # whole extraction to `{stem}-2/…`, `-3/…` when it would
                stem = pairs[0][0].split("/", 1)[0]
                final, i = stem, 1
                while _tree_conflict(final, used):
                    i += 1
                    final = f"{stem}-{i}"
                for rel, content in pairs:
                    rel = final + rel[len(stem):]
                    if rel in used:      # unreachable once stems are unique
                        continue
                    used.add(rel)        # later attachments cannot collide
                    attachments.append({
                        "filename": rel,
                        "content_b64": base64.b64encode(content).decode()})
        return f"[attachment: {fname}]"

    mission_lines = []
    for att in act.mission_attachments:
        if att.kind == "link":
            mission_lines.append(f"[link: {att.name or att.url}]({att.url})")
        else:
            mission_lines.append(await _materialize(att))

    lines = []
    if act.truncated:   # the adapter's hard stop — never silent (ADR-0014)
        lines += ["⚠ FEED TRUNCATED — the feed exceeded the full-history "
                  "hard stop; the OLDEST entries are missing from this "
                  "mirror.", ""]
    lines += [
        f"# {m.key}: {m.title}",
        "> Brief: MISSION.md (same folder) — description, labels, mission attachments.", "",
        "## Activity (chronological mirror of the PMO feed)",
        "Entries marked 🧑 HUMAN are instructions/steering from a person — they",
        "are authoritative. Entries marked 🤖 DevCake are DevCake's own records.",
    ]
    by_id = {e.entry_id: e for e in act.entries if e.entry_id}
    for e in act.entries:
        body = e.body or ""
        # provenance is sentinel-based, never author-based (docs/03 §8a):
        # DevCake may post with the operator's own PMO credentials
        provenance = "🤖 DevCake" if _is_devcake_comment(body) else "🧑 HUMAN"
        lines.append(f"### {e.ts:%Y-%m-%d %H:%M} — {e.author} — {provenance} ({e.kind})")
        parent = by_id.get(e.parent_id) if e.parent_id else None
        if parent is not None:
            lines.append(f"↳ reply to {parent.author} @ {parent.ts:%Y-%m-%d %H:%M}")
        elif e.parent_id:
            lines.append("↳ reply to (deleted comment)")
        lines.append(body)                # full body — the mirror never trims
        for att in e.attachments:
            lines.append(await _materialize(att))
        lines.append("")
    return {"mission_md": _mission_md(m, mission_lines),
            "activity_md": "\n".join(lines), "attachments": attachments}

