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
import re
from datetime import datetime  # noqa: F401 — type context for Activity entries
from pathlib import Path

from ..model import MissionRef
from .feed import is_devcake_comment, unquoted
from .markers import (COMMENT_SENTINEL, PART_LINE, PLAN_FILE, STEP_MARKER,
                      discovery_in_keys)

log = logging.getLogger("devcake.missions")


def _part_coords(body: str) -> tuple[int, int] | None:
    for line in unquoted(body).splitlines():
        hit = PART_LINE.match(line)
        if hit:
            return int(hit.group(1)), int(hit.group(2))
    return None


def _strip_part_and_sentinel(body: str) -> str:
    text = body or ""
    if COMMENT_SENTINEL in text:
        text = text[: text.rfind(COMMENT_SENTINEL)].rstrip()
    lines = text.splitlines()
    drop: set[int] = set()
    for i, line in enumerate(lines):
        if PART_LINE.match(line):
            drop.add(i)
            if i + 1 < len(lines) and lines[i + 1] == "":
                drop.add(i + 1)
            break
    return "\n".join(ln for i, ln in enumerate(lines) if i not in drop)


def _join_part_payloads(chunks: list[str]) -> str:
    if not chunks:
        return ""
    out = chunks[0]
    for nxt in chunks[1:]:
        if out and nxt and out[-1].isalnum() and nxt[0].isalnum():
            out += nxt
        elif out.endswith("\n") or not nxt:
            out += nxt
        else:
            out += "\n" + nxt
    return out


def _step_filename(reconstructed: str) -> str | None:
    scan = unquoted(reconstructed)
    hit = STEP_MARKER.search(scan)
    if hit:
        return f"{hit.group(1)}_{hit.group(2)}.md"
    hit = PLAN_FILE.search(scan)
    if hit:
        return hit.group(1)
    return None


def _substance(filename: str, reconstructed: str) -> str:
    """What the Dev reads in N_TYPE.md / PLAN_N.md (Linear attachment bytes)."""
    if filename.startswith("PLAN_"):
        parts = reconstructed.split("\n\n", 1)
        return parts[1] if len(parts) > 1 else reconstructed
    lines = reconstructed.splitlines()
    i = 0
    while i < len(lines) and not (lines[i].startswith(">") or lines[i] == ">"):
        i += 1
    if i >= len(lines):
        return reconstructed
    dump: list[str] = []
    for line in lines[i:]:
        if line.startswith("> "):
            dump.append(line[2:])
        elif line == ">":
            dump.append("")
        else:
            dump.append(line)
    text = "\n".join(dump)
    if text.startswith("---\n\n"):
        text = text[5:]
    return text


def coalesced_step_files(entries) -> list[tuple[str, str, object]]:
    """(filename, content, first_entry) from paginated or single inline steps."""
    out: list[tuple[str, str, object]] = []
    i = 0
    n = len(entries)
    while i < n:
        e = entries[i]
        body = e.body or ""
        if not is_devcake_comment(body):
            i += 1
            continue
        coords = _part_coords(body)
        if coords and coords[0] == 1 and coords[1] >= 2:
            total = coords[1]
            group = [e]
            j = i + 1
            while len(group) < total and j < n:
                nxt = entries[j]
                if not is_devcake_comment(nxt.body or ""):
                    break
                if _part_coords(nxt.body or "") != (len(group) + 1, total):
                    break
                group.append(nxt)
                j += 1
            if len(group) == total:
                reconstructed = _join_part_payloads(
                    [_strip_part_and_sentinel(g.body or "") for g in group])
                name = _step_filename(reconstructed)
                if name:
                    out.append((name, _substance(name, reconstructed), e))
                i = j
                continue
        name = _step_filename(_strip_part_and_sentinel(body))
        if name and coords is None:
            reconstructed = _strip_part_and_sentinel(body)
            out.append((name, _substance(name, reconstructed), e))
        i += 1
    return out


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


async def push_activity_repo(mgr, mission, mtype, seq: int,
                             blocker_notes: list[dict] | None = None
                             ) -> dict[str, str]:
    """ADR-0014 D4: one snapshot commit per step dispatch. Any failure is
    audited loudly and swallowed — the run proceeds on the Redis fallback;
    Gitea down degrades to pre-ADR behavior, never to a halt.

    Returns the pushed snapshot's feed watermark (ADR-0031) — {} when the
    push failed or never ran: a failed push means the Dev clones the
    PREVIOUS snapshot, so a watermark from this fetch would overclaim what
    the run actually read."""
    if mgr.internal_forge is None:
        return {}
    try:
        payload = await activity_payload(mgr, mission.pmo_id, mission.pmo_kind,
                                         blocker_notes=blocker_notes)
        name = await mgr.internal_forge.ensure_activity_repo(
            mgr.instance_name, mission.key)
        await mgr.internal_forge.push_activity_snapshot(
            name, _activity_snapshot_files(payload),
            f"step {seq} {mtype.value} dispatch")
        log.info("activity repo %s: snapshot for step %d", name, seq)
        return payload.get("feed_watermark") or {}
    except Exception as e:
        log.exception("activity repo push failed for %s", mission.key)
        mgr._audit(mission.pmo_id, "activity_repo_push_failed",
                    f"{mission.key}: {type(e).__name__}: {str(e)[:180]}")
        return {}


def _mission_md(m, attachment_lines=(), document_lines=(),
                blocker_lines=(), discovery_lines=()) -> str:
    """ADR-0014 D3: MISSION.md — the brief. Stable regardless of feed length;
    every step playbook points here."""
    lines = [
        f"# {m.key}: {m.title}",
        f"> Kind: {m.pmo_kind} · Status: {m.status} · Priority: {m.priority} · URL: {m.url}",
        f"> Labels: {', '.join(sorted(m.labels)) or '(none)'}", "",
        "## Description", m.description or "(none)"]
    if blocker_lines:
        # ADR-0032 — done blockers' closing notes; the description above
        # predates them (frozen at decomposition time), so on conflict the
        # handoff is newer
        lines += ["", "## Blocked by (completed — handoffs)", *blocker_lines]
    if document_lines:
        lines += ["", "## Project documents", *document_lines]
    if attachment_lines:
        lines += ["", "## Mission attachments", *attachment_lines]
    if discovery_lines:
        # ADR-0033 D6 — the dedicated CLOSING block, advisory register:
        # routed text is model-authored material that crossed a mission
        # boundary; it is never laundered into spec-register text
        lines += ["", "## Related missions reported the following "
                      "discoveries (leads, not truths — verify against "
                      "each source before relying)", *discovery_lines]
    return "\n".join(lines)


def _discovery_lines(entries) -> list[str]:
    """Routed-discovery deliveries (ADR-0033), collected from delivery
    markers over UNQUOTED bodies — this module's one feed scan, and the
    IRON RULE applies to it like every other. MISSION.md lists the leads
    with provenance + pointers; the full delivery text already rides
    ACTIVITY.md's faithful mirror, so the brief stays stable and small."""
    out: list[str] = []
    seen: set[tuple[str, int]] = set()
    for e in entries:
        for src, step in sorted(discovery_in_keys(unquoted(e.body))):
            if (src, step) in seen:
                continue
            seen.add((src, step))
            ts = getattr(e, "ts", None)
            date = f" · {ts:%Y-%m-%d}" if ts else ""
            out.append(f"{len(out) + 1}. [{src} · step {step}{date}] — the "
                       f"routed delivery is in ACTIVITY.md; full record: "
                       f"`DISCOVERY_{step}.md` on {src}.")
    return out


def _doc_filename(title: str) -> str:
    """One safe path segment from a vendor document title: path separators
    and control chars collapse to spaces; leading/trailing dots stripped so
    `.`/`..` can never form; empty falls back. Length-capped like the URL
    basename fallback in _materialize."""
    seg = re.sub(r"[\\/\x00-\x1f]+", " ", title or "").strip().strip(".").strip()
    return (seg or "untitled")[:80]


async def activity_payload(mgr, pmo_id: str, kind: str = "issue",
                           blocker_notes: list[dict] | None = None) -> dict:
    """ADR-0014 D3: MISSION.md = the brief; ACTIVITY.md = a faithful MIRROR
    of the feed — full bodies inline (never externalized), attachments by
    name in feed order, reply nesting; every attachment's bytes ride as
    sibling files. Project refs (project-fidelity fix): the feed is the
    project-update mirror, and project Documents materialize under docs/.
    blocker_notes (ADR-0032, dispatch-only — the Redis fallback rebuild has
    no resolved blockers and omits the section) render as a MISSION.md
    handoff block."""
    act = await mgr.pmo.get_activity(MissionRef(pmo_id, kind), full=True)
    m = act.mission
    attachments = []
    used: set[str] = {"ACTIVITY.md", "MISSION.md"}   # docs/07 §2 dedupe seed
    # GitHub pages long posts; Linear/Gitea ship N_TYPE.md as attachments.
    # Rebuild those sibling files from Part i of n (or a single inline post)
    # so /workspace/activity/ looks the same to the Dev.
    step_files = coalesced_step_files(act.entries)
    step_by_first = {id(first): fname for fname, _content, first in step_files}

    # Documents FIRST (before mission/feed attachments): their docs/… paths
    # then live in `used`, so a later flat attachment literally named `docs`
    # suffixes to docs-2 instead of colliding file-vs-dir (unrepresentable
    # in the snapshot's git tree)
    document_lines = []
    try:
        doc_cap = mgr._attachment_cap()
    except Exception:  # noqa: BLE001 — cap is advisory; fixed budget fallback
        doc_cap = 50 * 1024 * 1024
    for doc in act.documents:
        content = (doc.content or "").encode()
        if len(content) > doc_cap:
            document_lines.append(
                f"[document too large to mirror: {doc.title}]({doc.url})")
            continue
        fname = _unique_name(f"docs/{_doc_filename(doc.title)}.md", used)
        attachments.append({"filename": fname,
                            "content_b64": base64.b64encode(content).decode()})
        document_lines.append(f"[document: {fname}]")

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
    if kind == "project" and not act.entries:
        # self-explanatory empty mirror (the pre-fix stub said projects carry
        # no feed at all — no longer true; they carry project updates)
        lines += ["", "(no project updates yet — the project-native feed is "
                      "mirrored here once updates exist)"]
    by_id = {e.entry_id: e for e in act.entries if e.entry_id}
    for e in act.entries:
        body = e.body or ""
        # provenance is sentinel-based, never author-based (docs/03 §8a):
        # DevCake may post with the operator's own PMO credentials
        provenance = "🤖 DevCake" if is_devcake_comment(body) else "🧑 HUMAN"
        lines.append(f"### {e.ts:%Y-%m-%d %H:%M} — {e.author} — {provenance} ({e.kind})")
        parent = by_id.get(e.parent_id) if e.parent_id else None
        if parent is not None:
            lines.append(f"↳ reply to {parent.author} @ {parent.ts:%Y-%m-%d %H:%M}")
        elif e.parent_id:
            lines.append("↳ reply to (deleted comment)")
        lines.append(body)                # full body — the mirror never trims
        for att in e.attachments:
            lines.append(await _materialize(att))
        fname = step_by_first.get(id(e))
        if fname and not any((att.name or "") == fname for att in e.attachments):
            lines.append(f"[attachment: {fname}]")
        lines.append("")
    # ADR-0031 — the newest entry this mirror includes, the consumer run's
    # reading receipt. Entries arrive ascending from both adapters; a fresh
    # mission's EMPTY feed yields no watermark (never an IndexError — this
    # builder also serves the Redis activity.get fallback, where an escape
    # would cost the Dev its activity.result). Wire-safe extra key: the
    # entrypoint and the snapshot builder read known keys via .get.
    for fname, content, _first in step_files:
        if fname in used:
            continue
        fname = _unique_name(fname, used)
        attachments.append({
            "filename": fname,
            "content_b64": base64.b64encode(content.encode()).decode(),
        })
    last = act.entries[-1] if act.entries else None
    watermark = ({"entry_id": last.entry_id or "", "ts": last.ts.isoformat()}
                 if last is not None else {})
    blocker_lines = []
    for n in blocker_notes or []:
        blocker_lines.append(f"- `{n['mission_key']}` — {n['title']}")
        if n.get("handoff"):
            blocker_lines.append(f"  Handoff: {n['handoff']}")
    return {"mission_md": _mission_md(m, mission_lines, document_lines,
                                      blocker_lines,
                                      _discovery_lines(act.entries)),
            "activity_md": "\n".join(lines), "attachments": attachments,
            "feed_watermark": watermark}

