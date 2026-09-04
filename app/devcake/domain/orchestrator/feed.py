"""Feed choke-point, audit log, and provenance helpers (docs/03 §8a, docs/05 §4)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ...security import redact
from ..model import Mission, MissionRef, STAGE_LABELS
from ..run import utcnow
from . import markers
from .markers import (COMMENT_SENTINEL, DELIVERABLE_MARKER, FEED_INLINE_MAX,
                      PART_LINE, PLAN_FILE, REPLY_MARKER, SENTINEL_RE,
                      STEP_MARKER)

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


def _attachments_supported(mgr) -> bool:
    """Official file-upload API. Missing/broken caps must not drop the feed."""
    try:
        return bool(mgr.pmo.capabilities().attachments_supported)
    except Exception:  # noqa: BLE001 — missing/broken caps must not drop feed
        return True


def _comment_max_chars(mgr) -> int | None:
    """Vendor issue-comment cap, or None when the adapter does not declare one."""
    try:
        n = mgr.pmo.capabilities().comment_max_chars
    except Exception:  # noqa: BLE001 — missing caps must not drop the feed
        return None
    return int(n) if n else None


_PART_LABEL_BUDGET = len("Part 999 of 999") + 2  # label + blank line
# GitHub secondary write limits (~80 content creations / minute). A 50 MB
# dump at comment_max_chars=65536 is ~800 comments; refuse before we try.
MAX_VENDOR_COMMENT_PARTS = 40


def _part_label(i: int, n: int) -> str:
    return f"Part {i} of {n}"


def _chunk_text(text: str, room: int) -> list[str]:
    """Split `text` into pieces of at most `room` characters.

    `"".join(chunks) == text`. A newline cut keeps the newline on the
    left chunk so join is concatenation, not a guess.
    """
    if room < 1:
        raise ValueError("vendor comment room must be positive")
    out: list[str] = []
    rest = text
    while rest:
        if len(rest) <= room:
            out.append(rest)
            break
        cut = rest.rfind("\n", 0, room)
        if cut >= room // 4:
            out.append(rest[: cut + 1])
            rest = rest[cut + 1:]
        else:
            out.append(rest[:room])
            rest = rest[room:]
    return out


def _attach_part_label(chunk: str, i: int, n: int) -> str:
    """`Part i of n` near the top. startswith-markers stay the first line."""
    label = _part_label(i, n)
    raw = chunk
    for marker in (REPLY_MARKER, DELIVERABLE_MARKER):
        if raw.startswith(marker):
            rest = raw[len(marker):]
            if rest.startswith("\n"):
                rest = rest[1:]
            if rest.startswith("\n"):
                rest = rest[1:]
            return f"{marker}\n\n{label}\n\n{rest}"
    return f"{label}\n\n{raw}"


def split_vendor_comments(markdown: str, limit: int) -> list[str]:
    """Full body as one or more comments, each fitting under `limit`.

    Single-comment posts are unlabeled. A split is labeled `Part i of n`
    on every page. The sentinel `_feed` appends is reserved in `limit`.
    """
    text = markdown.rstrip()
    sentinel_over = len("\n\n") + len(COMMENT_SENTINEL)
    if len(text) + sentinel_over <= limit:
        return [text]
    room = limit - sentinel_over - _PART_LABEL_BUDGET
    if room < 64:
        room = 64
    chunks = _chunk_text(text, room)
    n = len(chunks)
    if n > MAX_VENDOR_COMMENT_PARTS:
        raise ValueError(
            f"paginated comment would post more than "
            f"{MAX_VENDOR_COMMENT_PARTS} parts ({n}) — refusing")
    parts = [_attach_part_label(c, i, n) for i, c in enumerate(chunks, 1)]
    for part in parts:
        if len(part) + sentinel_over > limit:
            raise ValueError(
                f"paginated comment still exceeds vendor cap "
                f"({len(part) + sentinel_over} > {limit})")
    return parts


def strip_vendor_page(body: str) -> str:
    """Remove sentinel and one `Part i of n` label. Inverse of seal+label."""
    text = body or ""
    idx = text.rfind(COMMENT_SENTINEL)
    if idx >= 0:
        text = text[:idx]
        if text.endswith("\n\n"):
            text = text[:-2]
        elif text.endswith("\n"):
            text = text[:-1]
    for marker in (REPLY_MARKER, DELIVERABLE_MARKER):
        prefix = marker + "\n\n"
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix):]
        first, sep, after = rest.partition("\n")
        if PART_LINE.match(first):
            if after.startswith("\n"):
                after = after[1:]
            return marker + "\n\n" + after
        return text
    first, sep, after = text.partition("\n")
    if PART_LINE.match(first):
        if after.startswith("\n"):
            after = after[1:]
        return after
    return text


def join_vendor_comments(bodies: list[str]) -> str:
    """Inverse of split_vendor_comments + the `_feed` sentinel suffix."""
    return "".join(strip_vendor_page(b) for b in bodies)


def _part_coords(body: str) -> tuple[int, int] | None:
    for line in unquoted(body).splitlines():
        hit = PART_LINE.match(line)
        if hit:
            return int(hit.group(1)), int(hit.group(2))
    return None


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
    """Linear-shaped attachment bytes from a reconstructed feed comment."""
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
    """(filename, content, first_entry) from paginated or single inline steps.

    The inverse of `_feed`'s vendor-cap split. activity_payload only calls
    this — it must not re-parse Part labels.
    """
    out: list[tuple[str, str, object]] = []
    i = 0
    n = len(entries)
    while i < n:
        e = entries[i]
        body = e.body or ""
        if not is_devcake_comment(body):
            i += 1
            continue
        page = strip_vendor_page(body)
        coords = _part_coords(body)
        if coords and coords[0] == 1 and coords[1] >= 2:
            total = coords[1]
            group = [e]
            j = i + 1
            while len(group) < total and j < n:
                nxt = entries[j]
                nb = nxt.body or ""
                if not is_devcake_comment(nb):
                    break
                if _part_coords(nb) != (len(group) + 1, total):
                    break
                group.append(nxt)
                j += 1
            if len(group) == total:
                reconstructed = join_vendor_comments(
                    [g.body or "" for g in group])
                name = _step_filename(reconstructed)
                if name:
                    out.append((name, _substance(name, reconstructed), e))
                i = j
                continue
        name = _step_filename(page)
        if name and coords is None:
            out.append((name, _substance(name, page), e))
        i += 1
    return out


def _audit(mgr, pmo_id: str, action: str, detail: str = "") -> None:
    # Belt-and-braces: detail should be names/counts only, but exception
    # fragments (e.g. activity_repo_push_failed) can embed secret shapes —
    # match settings_bundle.audit_event so on-disk JSONL is scrubbed too.
    detail = redact(detail)
    markers.AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # redact(detail) already scrubbed; JSONL write is post-barrier (scanner cannot see MaD yet).
    with open(markers.AUDIT_PATH, "a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                            "instance": getattr(mgr, "instance_name", ""),
                            "pmo_id": pmo_id, "action": action, "detail": detail}) + "\n")
    mgr._grace_next.add(pmo_id)
    # mirror every audit action as a span so OO alerts can fire on them
    # (`devcake_needs_human` was a file-only record no alert could ever see).
    # One span name, action as attribute — the alert set queries
    # devcake_audit_action.
    with tracer.start_as_current_span("audit.event") as span:
        span.set_attribute("devcake.audit.action", action)
        span.set_attribute("devcake.pmo.id", pmo_id)
        span.set_attribute("devcake.audit.detail", detail[:500])


def _trip_breaker(mgr, name: str, reason: str) -> None:
    """Single choke point for tripping a breaker: sets the in-memory dict
    AND emits a span — breakers had no telemetry at all, so the documented
    DEV_AUTH alert could never fire."""
    mgr.breakers[name] = reason
    with tracer.start_as_current_span("breaker.trip") as span:
        span.set_attribute("devcake.breaker", name)
        span.set_attribute("devcake.reason", redact(reason)[:500])
        span.set_status(Status(StatusCode.ERROR, f"breaker {name} tripped"))


async def _feed(mgr, pmo_id: str, kind: str, markdown: str, *,
                externalize: bool = True) -> None:
    """The single choke-point for PMO comments: redaction + the provenance
    sentinel. Bodies over FEED_INLINE_MAX are uploaded as .md attachments
    and replaced by a short referencing comment (docs/05 §4) unless the
    caller opts out (externalize=False — the ADR-0014 finalize post, whose
    long text already lives in its own attachment). When the vendor
    declares `comment_max_chars` and the body will not fit, `_feed` posts
    the FULL text as sequential `Part i of n` comments (markers stay on
    part 1; each page carries the sentinel) up to MAX_VENDOR_COMMENT_PARTS.
    Never a truncated dump, never a 422; over the part cap is ValueError.
    The sentinel
    goes on the comment, never inside the attachment, so provenance
    classification keeps working. Upload failures fall back to posting
    inline — an upload outage must never lose feed content. Projects have
    no issue-style comments API (verified live): their run artifacts
    live in the audit log + OpenObserve; the substance lands on the child
    issues anyway (ADR-0006)."""
    markdown = redact(markdown)
    if kind == "project":
        # via the MANAGER method, not _audit(mgr, ...) directly (audit D5 #1):
        # tests override mgr._audit as an instance attribute (fakes noop_audit,
        # the activity-repo audit collector) — the direct module call would
        # bypass that seam, leaking to the global events.jsonl and adding a
        # grace-cycle skip the pre-refactor code did not.
        mgr._audit(pmo_id, "project_feed_suppressed", markdown[:120])
        return
    if externalize and len(markdown) > FEED_INLINE_MAX \
            and _attachments_supported(mgr):
        try:
            name = f"comment-{utcnow():%Y%m%dT%H%M%S}.md"
            url = await mgr.pmo.upload_attachment(pmo_id, name,
                                                   markdown.encode())
            # preview from UNQUOTED lines only: flattening newlines would
            # otherwise land "> "-quarantined text mid-line, back in scan
            # scope (ADR-0014 D2)
            preview = unquoted(markdown) or markdown[:300]
            markdown = (preview[:300].replace("\n", " ")
                        + f"… — full text attached: [{name}]({url})")
        except Exception:
            log.exception("feed attachment upload failed — posting inline")
    cap = _comment_max_chars(mgr)
    parts = (split_vendor_comments(markdown, cap) if cap is not None
             else [markdown.rstrip()])
    try:
        for part in parts:
            # Cut-newlines stay: join_vendor_comments is concatenation.
            await mgr.pmo.post_feed(
                MissionRef(pmo_id, "issue"),
                part + "\n\n" + COMMENT_SENTINEL)
    finally:
        # even a post the client saw fail may have landed on the vendor:
        # invalidating after a failure is always safe, keeping is not
        feed_written(mgr, pmo_id)


def feed_written(mgr, pmo_id: str) -> None:
    """DevCake wrote to this feed: memoized scans of it are stale (ADR-0033
    addendum). Every DevCake-authored issue comment passes through `_feed`,
    so this is the one invalidation site."""
    memo = getattr(mgr, "feed_memo", None)
    if memo is not None:
        memo.forget(pmo_id)


async def post_attachment_comment(mgr, pmo_id: str, kind: str, *,
                                  filename: str, content: str,
                                  comment_of) -> str | None:
    """The ONE attachment+comment pipe (ADR-0033 chokepoint ruling): upload
    `content` as `filename`, then post the comment `comment_of(url)` builds —
    url is None when the upload failed, so the builder picks its inline
    fallback (an upload outage must never lose feed content). comment_of
    returns (body, externalize): callers keep their exact externalization
    semantics — a counted marker must ride the comment (False), a plain
    pointer comment may keep the size second-chance (True). Callers redact
    `content`; the comment body still passes through _feed's redact."""
    url = None
    if _attachments_supported(mgr):
        try:
            url = await mgr.pmo.upload_attachment(pmo_id, filename,
                                                  content.encode())
        except Exception:  # noqa: BLE001 — fall back to the builder's inline form
            log.exception("attachment upload failed for %s — inline fallback",
                          filename)
    body, externalize = comment_of(url)
    await mgr._feed(pmo_id, kind, body, externalize=externalize)
    return url


def blockquote(text: str) -> str:
    """Inverse of unquoted: prefix EVERY line with '> ' (bare '>' for blank
    lines, so lazy-continuation can't leak) — the ADR-0014 D2 quarantine for
    model-authored text posted inline. Applied app-side at the finalize
    choke-point, never in the entrypoint: old images stay quarantined too."""
    return "\n".join("> " + line if line.strip() else ">"
                     for line in (text or "").splitlines())


def unquoted(body: str | None) -> str:
    """Strip `>`-quoted lines: markers/sentinels inside a human's quote of
    a DevCake comment must never count as DevCake's own."""
    return "\n".join(line for line in (body or "").splitlines()
                     if not line.lstrip().startswith(">"))


def is_devcake_comment(body: str | None) -> bool:
    """Provenance classification (docs/03 §8a): sentinel-signed ⇒ DevCake.
    `>`-quoted lines are ignored, so a human reply that ENDS by quoting a
    DevCake comment still classifies as human — misreading a human's
    instruction as DevCake's own record is the unsafe direction."""
    return bool(SENTINEL_RE.search(
        unquoted(body).rstrip()))


def stage_of(mission: Mission) -> str | None:
    stage = mission.labels & STAGE_LABELS
    return next(iter(stage)) if stage else None

