"""Feed choke-point, audit log, and provenance helpers (docs/03 §8a, docs/05 §4)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Sequence

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ...ports.pmo import FOLD_DETAILS, FOLD_PLUS, PMOTransient
from ...security import redact
from ..model import Mission, MissionRef, STAGE_LABELS
from ..run import utcnow
from . import markers
from .markers import (ANSWER_TOKEN_RE, COMMENT_SENTINEL, FEED_INLINE_MAX,
                      PART_LINE, PLAN_FILE, REPLY_MARKER, SENTINEL_RE,
                      STALL_MARKER_RE, STATUS_MARKER_RE, STEP_MARKER)

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


def _attachments_supported(mgr) -> bool:
    """Official file-upload API. Missing/broken caps must not drop the feed."""
    try:
        return bool(mgr.pmo.capabilities().attachments_supported)
    except Exception:  # noqa: BLE001 — missing/broken caps must not drop feed
        return True


def _threads_supported(mgr) -> bool:
    """Vendor nests comments (`feed_threads`). Missing/broken caps ⇒ top
    level: threading is presentation, so the safe default is flat."""
    try:
        return bool(mgr.pmo.capabilities().feed_threads)
    except Exception:  # noqa: BLE001 — missing/broken caps must not drop the feed
        return False


def _comment_max_chars(mgr) -> int | None:
    """Vendor issue-comment cap, or None when the adapter does not declare one."""
    try:
        n = mgr.pmo.capabilities().comment_max_chars
    except Exception:  # noqa: BLE001 — missing caps must not drop the feed
        return None
    return int(n) if n else None


def collapsible_of(mgr) -> str:
    """The vendor's fold syntax family (`feed_collapsible`). Missing/broken
    caps ⇒ "" — the fold renders flat; every marker still rides inline."""
    try:
        return str(mgr.pmo.capabilities().feed_collapsible or "")
    except Exception:  # noqa: BLE001 — missing/broken caps must not drop the feed
        return ""


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
    """`Part i of n` near the top. The startswith-marker of a pre-card
    answer comment (`_legacy_tail`) stays the first line."""
    label = _part_label(i, n)
    raw = chunk
    if raw.startswith(REPLY_MARKER):
        rest = raw[len(REPLY_MARKER):]
        if rest.startswith("\n"):
            rest = rest[1:]
        if rest.startswith("\n"):
            rest = rest[1:]
        return f"{REPLY_MARKER}\n\n{label}\n\n{rest}"
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
    prefix = REPLY_MARKER + "\n\n"
    if text.startswith(prefix):
        rest = text[len(prefix):]
        first, sep, after = rest.partition("\n")
        if PART_LINE.match(first):
            if after.startswith("\n"):
                after = after[1:]
            return REPLY_MARKER + "\n\n" + after
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
    """Linear-shaped attachment bytes from a reconstructed feed comment.

    A step card (ADR-0042) that had to carry its dump inline keeps it as
    the fold's `Transcript (inline)` section: only that section's quoted
    lines are the dump. A pre-card body (no such section) is read as it
    always was — the quoted region from its first `>` line on."""
    if filename.startswith("PLAN_"):
        parts = reconstructed.split("\n\n", 1)
        return parts[1] if len(parts) > 1 else reconstructed
    lines = reconstructed.splitlines()
    inline = _inline_transcript_lines(lines)
    if inline is not None:
        lines = inline
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


def _page_groups(entries) -> Iterator[tuple[list, str | None]]:
    """Walk a feed grouping DevCake's `Part i of n` pages back into one
    post: yields (entries_of_the_post, reconstructed_body). A non-DevCake
    entry yields ([entry], None). A lone page whose siblings are missing
    yields ([entry], None) too — a partial post is never reconstructed.
    The ONE walk shared by `coalesced_step_files` and the projection
    (`unfold_entries`)."""
    i = 0
    n = len(entries)
    while i < n:
        e = entries[i]
        body = e.body or ""
        if not is_devcake_comment(body):
            yield [e], None
            i += 1
            continue
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
                yield group, join_vendor_comments([g.body or "" for g in group])
                i = j
                continue
        if coords is None:
            yield [e], strip_vendor_page(body)
        else:
            yield [e], None
        i += 1


def coalesced_step_files(entries) -> list[tuple[str, str, object]]:
    """(filename, content, first_entry) from paginated or single inline steps.

    The inverse of `_feed`'s vendor-cap split. activity_payload only calls
    this — it must not re-parse Part labels.
    """
    out: list[tuple[str, str, object]] = []
    for group, reconstructed in _page_groups(entries):
        if reconstructed is None:
            continue
        name = _step_filename(reconstructed)
        if name:
            out.append((name, _substance(name, reconstructed), group[0]))
    return out


def _request_actor() -> str:
    """The control-plane actor label for this audit row ("" outside a
    request). Read lazily: the domain must not import the API package at
    import time."""
    try:
        from ...api.auth import REQUEST_ACTOR
    except Exception:  # noqa: BLE001 — an audit row must never fail on a label
        return ""
    return REQUEST_ACTOR.get()


def _audit(mgr, pmo_id: str, action: str, detail: str = "") -> None:
    # Belt-and-braces: detail should be names/counts only, but exception
    # fragments (e.g. activity_repo_push_failed) can embed secret shapes —
    # match settings_bundle.audit_event so on-disk JSONL is scrubbed too.
    detail = redact(detail)
    markers.AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # redact(detail) already scrubbed; JSONL write is post-barrier (scanner cannot see MaD yet).
    # the actor is an allowlisted label (auth.request_actor: [a-z0-9._-],
    # 32 chars) — redact() is the same belt as for detail, so a caller that
    # misuses the header can never park a secret shape in the audit file
    with open(markers.AUDIT_PATH, "a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                            "instance": getattr(mgr, "instance_name", ""),
                            "pmo_id": pmo_id, "action": action, "detail": detail,
                            "actor": redact(_request_actor())}) + "\n")
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
                externalize: bool = True,
                reply_to: str | None = None) -> str | None:
    """The single choke-point for PMO comments: redaction + the provenance
    sentinel. Returns the vendor entry id of the comment posted (part 1
    when paged) so a step's bookkeeping can thread under it: `reply_to`
    nests the post under that earlier top-level entry on vendors that
    declare `feed_threads` and is silently dropped elsewhere — the body,
    markers and sentinel are identical either way (docs/03 §8). Bodies over FEED_INLINE_MAX are uploaded as .md attachments
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
    inline — an upload outage must never lose feed content. Project-kind
    missions post to the vendor's project-native feed (updates) through
    this same chokepoint (ADR-0043 §2): no threads, otherwise the same
    policy — a project run's record lands where its mirror reads."""
    markdown = redact(markdown)
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
    # the keyword reaches the adapter only when the vendor threads: a
    # flat vendor's post_feed is called exactly as before
    thread = ({"reply_to": reply_to}
              if reply_to and kind == "issue" and _threads_supported(mgr)
              else {})
    first: str | None = None
    try:
        for part in parts:
            # Cut-newlines stay: join_vendor_comments is concatenation.
            # Every page of one post nests under the same anchor.
            body = part + "\n\n" + COMMENT_SENTINEL
            try:
                cid = await mgr.pmo.post_feed(MissionRef(pmo_id, kind),
                                              body, **thread)
            except PMOTransient:
                raise            # budget / network: retried as it always was
            except Exception as e:  # noqa: BLE001 — see below
                if not thread:
                    raise
                # the vendor refused the NESTING (verified live: the anchor
                # was deleted by a person → "entity not found"; a reply to
                # a reply → "incorrect parent"): the post itself must never
                # be lost, so it lands top level — exactly a flat vendor's
                # behaviour — and the audit says so once. The next parts of
                # this post follow it flat.
                log.warning("threaded post refused on %s (%s) — posting top "
                            "level", pmo_id, e)
                mgr._audit(pmo_id, "feed_thread_fallback", str(e)[:200])
                thread = {}
                cid = await mgr.pmo.post_feed(MissionRef(pmo_id, kind), body)
            if first is None:
                first = cid
    finally:
        # even a post the client saw fail may have landed on the vendor:
        # invalidating after a failure is always safe, keeping is not
        feed_written(mgr, pmo_id)
    return first


async def _edit(mgr, pmo_id: str, kind: str, entry_id: str,
                markdown: str) -> None:
    """The second feed chokepoint (ADR-0042 §3, §5): replace the whole body
    of DevCake's OWN entry — a step card gaining a late fold section, the
    status comment refreshed. Same policy as `_feed` — redaction, the
    sentinel appended LAST, project kind suppressed to the audit log —
    with three deliberate differences: no externalization (a fold's
    markers must stay inline, so a long body is the caller's problem), no
    vendor-cap paging (over `comment_max_chars` raises ValueError BEFORE
    any wire call — the caller falls back to a new post), no threading.
    `markdown` may arrive sealed or unsealed; it is never double-sealed.
    Invalidates the feed memo in `finally`, exactly like `_feed`: a write
    the client saw fail may still have landed. PMOTransient and permanent
    errors propagate — the callers decide the fallback (a vanished entry
    is permanent: post instead)."""
    markdown = redact(unseal(markdown))
    body = markdown.rstrip() + "\n\n" + COMMENT_SENTINEL
    cap = _comment_max_chars(mgr)
    if cap is not None and len(body) > cap:
        raise ValueError(
            f"edited body exceeds the vendor cap ({len(body)} > {cap})")
    try:
        await mgr.pmo.edit_feed(MissionRef(pmo_id, kind), entry_id, body)
    finally:
        feed_written(mgr, pmo_id)


def feed_written(mgr, pmo_id: str) -> None:
    """DevCake wrote to this feed: memoized scans of it are stale (ADR-0033
    addendum). Every DevCake-authored issue comment write — a post through
    `_feed`, an edit through `_edit` — passes here: the one invalidation
    site."""
    memo = getattr(mgr, "feed_memo", None)
    if memo is not None:
        memo.forget(pmo_id)


async def post_attachment_comment(mgr, pmo_id: str, kind: str, *,
                                  filename: str, content: str,
                                  comment_of,
                                  reply_to: str | None = None) -> str | None:
    """The ONE attachment+comment pipe (ADR-0033 chokepoint ruling): upload
    `content` as `filename`, then post the comment `comment_of(url)` builds —
    url is None when the upload failed, so the builder picks its inline
    fallback (an upload outage must never lose feed content). comment_of
    returns (body, externalize): callers keep their exact externalization
    semantics — a counted marker must ride the comment (False), a plain
    pointer comment may keep the size second-chance (True). Callers redact
    `content`; the comment body still passes through _feed's redact.
    Returns the posted comment's entry id (None when the vendor returns
    none); `reply_to` threads it as _feed does."""
    url = None
    if _attachments_supported(mgr):
        try:
            url = await mgr.pmo.upload_attachment(pmo_id, filename,
                                                  content.encode())
        except Exception:  # noqa: BLE001 — fall back to the builder's inline form
            log.exception("attachment upload failed for %s — inline fallback",
                          filename)
    body, externalize = comment_of(url)
    return await mgr._feed(pmo_id, kind, body, externalize=externalize,
                           reply_to=reply_to)


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



# ═══════════════════════════════════════════════════════════════════════════
# ADR-0042 — the feed for readers: folds, step cards, notices, and the
# unfolding projection. The shapes live HERE (the chokepoint owns every
# direction of every shape); activity_payload only calls `unfold_entries`.
# Every rendered body is handed to `_feed`/`_edit`, which append the
# sentinel LAST — the fold closes before it (SENTINEL_RE anchors at the end
# of the unquoted body; anything after the sentinel would flip the card to
# 🧑 HUMAN and trip the Freshness Gate on DevCake's own record).
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FoldSection:
    """One record section inside a fold: the LEGACY comment text verbatim
    (sentinel-free) under a fixed title. `at` is None for a section as old
    as its comment; a late append (routing receipts, a deliverable note)
    carries the append time so the projection unfolds it as an entry at
    ITS time, not the comment's."""
    title: str
    body: str
    at: datetime | None = None


# fixed section vocabulary — the projection keys on these titles
SECTION_ANSWER = "Answer"
SECTION_TOKEN_REPORT = "Token report"
SECTION_DISCOVERIES = "Discoveries"
SECTION_TRANSITION = "Transition"
SECTION_TRANSCRIPT_INLINE = "Transcript (inline)"
SECTION_RUN = "Run"
SECTION_ROUTING_RECEIPTS = "Routing receipts"
SECTION_DELIVERABLE = "Deliverable"
SECTION_RECORD = "Record"
# the projection never emits these as entries — they describe the card
_SILENT_SECTIONS = frozenset({SECTION_ANSWER, SECTION_RUN,
                              SECTION_TRANSCRIPT_INLINE})

# A section line: a glyph the head never uses, the bold title, an optional
# append time. The glyph keeps an ordinary bold line (`**Result:**`, a bold
# sentence in a notice head) from ever reading as a section.
SECTION_GLYPH = "▸"
SECTION_RE = re.compile(
    r"^▸ \*\*(?P<title>[A-Z][^*\n]{0,40})\*\*"
    r"(?: · at=(?P<at>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z))?$")
_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# fold wrappers per syntax family (PMOCapabilities.feed_collapsible). The
# `plus` fence and the `details` element are verified live on their
# vendors; "" renders the fold flat under a headed rule.
DETAILS_FOLD_OPEN = "<details>\n<summary>{summary}</summary>"
DETAILS_FOLD_CLOSE = "</details>"
PLUS_FOLD_OPEN = "+++ {summary}"
PLUS_FOLD_CLOSE = "+++"
PLAIN_FOLD_OPEN = "▾ {summary}"
PLAIN_FOLD_CLOSE = ""
DEFAULT_SUMMARY = "Details"


def _section_line(section: FoldSection) -> str:
    line = f"{SECTION_GLYPH} **{section.title}**"
    if section.at is not None:
        line += f" · at={section.at.astimezone(timezone.utc):{_AT_FORMAT}}"
    return line


def fold_summary(sections: Sequence[FoldSection]) -> str:
    """`Details — answer · token report · run`: the collapsed line a reader
    sees, naming what the fold holds."""
    names = [s.title.lower() for s in sections]
    return f"{DEFAULT_SUMMARY} — " + " · ".join(names) if names else DEFAULT_SUMMARY


def render_fold(sections: Sequence[FoldSection], *, collapsible: str,
                summary: str | None = None) -> str:
    """THE vendor-specific rendering (one function, one capability value):
    the fold's wrapper for the syntax family; the inner grammar is the same
    everywhere. Section bodies ride verbatim, so every backticked marker a
    scan reads sits on its own unquoted line inside the fold."""
    summary = summary or fold_summary(sections)
    if collapsible == FOLD_DETAILS:
        open_, close = DETAILS_FOLD_OPEN, DETAILS_FOLD_CLOSE
    elif collapsible == FOLD_PLUS:
        open_, close = PLUS_FOLD_OPEN, PLUS_FOLD_CLOSE
    else:
        open_, close = PLAIN_FOLD_OPEN, PLAIN_FOLD_CLOSE
    parts = [open_.format(summary=summary)]
    for sec in sections:
        parts.append(_section_line(sec))
        body = sec.body.strip("\n")
        if body:
            parts.append(body)
    if close:
        parts.append(close)
    return "\n\n".join(parts)


def _fold_opener(line: str) -> str | None:
    if line == "<details>":
        return FOLD_DETAILS
    if line.startswith("+++ ") or line == "+++":
        return FOLD_PLUS
    if line.startswith("▾ "):
        return ""
    return None


def strip_fold(body: str) -> tuple[str, str | None]:
    """(head, fold_inner) — the text before the fold and the fold's inner
    text (section lines + bodies, wrapper and summary line removed), or
    (body, None) when the body carries no fold. Parses every family by
    CONTENT regardless of the vendor: a wrong wrapper constant could break
    a vendor's rendering, never the projection or a scan."""
    text = unseal(body)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(">"):
            continue
        fam = _fold_opener(line)
        if fam is None:
            continue
        head = "\n".join(lines[:i]).rstrip()
        rest = lines[i + 1:]
        if fam == FOLD_DETAILS:
            # summary line, then everything up to the LAST closing tag
            if rest and rest[0].startswith("<summary>"):
                rest = rest[1:]
            end = _rfind(rest, DETAILS_FOLD_CLOSE)
            inner = rest[:end] if end is not None else rest
        elif fam == FOLD_PLUS:
            end = _rfind(rest, PLUS_FOLD_CLOSE)
            inner = rest[:end] if end is not None else rest
        else:
            inner = rest
        return head, "\n".join(inner).strip("\n")
    return text, None


def _rfind(lines: list[str], needle: str) -> int | None:
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] == needle:
            return i
    return None


def fold_sections(inner: str | None) -> list[FoldSection]:
    """Split a fold's inner text on its section lines. Text before the
    first section line belongs to no section and is dropped (a rendered
    fold never has any)."""
    out: list[FoldSection] = []
    title: str | None = None
    at: datetime | None = None
    buf: list[str] = []

    def flush():
        if title is not None:
            out.append(FoldSection(title, "\n".join(buf).strip("\n"), at))

    for line in (inner or "").splitlines():
        hit = SECTION_RE.match(line)
        if hit:
            flush()
            title = hit.group("title")
            stamp = hit.group("at")
            at = (datetime.strptime(stamp, _AT_FORMAT).replace(tzinfo=timezone.utc)
                  if stamp else None)
            buf = []
        elif title is not None:
            buf.append(line)
    flush()
    return out


def append_fold_section(body: str, section: FoldSection, *,
                        collapsible: str) -> str:
    """The late-bookkeeping append (ADR-0042 §3): `body` is a posted comment
    (sentinel or not), the result is the same comment with `section` added
    to its fold — created when a pre-card anchor has none — and the
    summary line re-rendered. Sentinel-free: `_edit` re-seals. A paged
    body (a vendor-cap split) is refused: its fold spans several entries
    and cannot be edited as one — the caller posts instead."""
    if _part_coords(body) is not None:
        raise ValueError("paged comment — a fold cannot be appended across pages")
    head, inner = strip_fold(body)
    sections = fold_sections(inner) + [section]
    return head.rstrip() + "\n\n" + render_fold(sections, collapsible=collapsible)


def unseal(body: str | None) -> str:
    """The body without its trailing provenance sentinel (and the blank line
    before it) — the inverse of the seal `_feed`/`_edit` apply."""
    text = body or ""
    idx = text.rfind(COMMENT_SENTINEL)
    if idx >= 0 and not text[idx + len(COMMENT_SENTINEL):].strip():
        text = text[:idx]
        if text.endswith("\n\n"):
            text = text[:-2]
        elif text.endswith("\n"):
            text = text[:-1]
    return text


def _inline_transcript_lines(lines: list[str]) -> list[str] | None:
    """The `Transcript (inline)` section's lines of a card body, or None
    when the body has no such section (a pre-card transcript comment)."""
    start = None
    for i, line in enumerate(lines):
        hit = SECTION_RE.match(line)
        if hit and hit.group("title") == SECTION_TRANSCRIPT_INLINE:
            start = i + 1
            break
    if start is None:
        return None
    out: list[str] = []
    for line in lines[start:]:
        if SECTION_RE.match(line) or line in (DETAILS_FOLD_CLOSE, PLUS_FOLD_CLOSE):
            break
        out.append(line)
    while out and not out[-1].strip():
        out.pop()                     # the blank line before the next section
    return out


# ── the step card ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StepCardParts:
    """Everything a step card renders. `answer_md` is the Dev's last
    message, already redacted; the renderer quotes it (ADR-0014 D2) and
    cuts it at a boundary. `transcript_url` None ⇒ the dump rides inline
    as a fold section (no-attachment vendor / upload failed) and
    `transcript_md` must be given. `sections` are the fold's record
    sections in order (token report, discoveries, transition, run …)."""
    seq: int
    mission_type: str
    glyph: str
    outcome_word: str
    result_line: str
    next_line: str
    transcript_name: str
    transcript_url: str | None
    sections: list[FoldSection] = field(default_factory=list)
    answer_md: str | None = None
    transcript_md: str | None = None
    duration_s: float | None = None
    cost_usd: float | None = None


CARD_RE = re.compile(
    r"^(?P<glyph>\S+) Step (?P<seq>\d+) · (?P<type>ONBOARD|PLAN|EXECUTE|REVIEW)"
    r" · (?P<outcome>[^·\n]+?)(?: · (?P<rest>[^\n]*))?$")
TRANSCRIPT_LINE_RE = re.compile(
    r"^Transcript: `(?P<name>\d+_(?:ONBOARD|PLAN|EXECUTE|REVIEW)\.md)`"
    r"(?: — \[download\]\((?P<url>\S+)\)| \(inline, in the details below\))$")
ANSWER_CUT_NOTE = "… (truncated — full text in the attachment)"
_LEGACY_REPLY_CUT_NOTE = "… (truncated — full text in the step transcript on this issue)"
_SENTENCE_END = re.compile(r"[.!?…](?=\s)")


def format_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    if seconds < 60:
        return f"{int(round(seconds))} s"
    minutes = int(round(seconds / 60))
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def card_header(glyph: str, seq: int, mission_type: str, outcome_word: str,
                duration_s: float | None = None,
                cost_usd: float | None = None) -> str:
    parts = [f"{glyph} Step {seq} · {mission_type} · {outcome_word}"]
    dur = format_duration(duration_s)
    if dur:
        parts.append(dur)
    if cost_usd is not None:
        parts.append(f"${cost_usd:.2f}")
    return " · ".join(parts)


def cut_at_boundary(text: str, budget: int = FEED_INLINE_MAX) -> tuple[str, bool]:
    """Cut `text` under `budget` at a paragraph break, else at a sentence
    end, else hard — never mid-sentence when a boundary exists past a
    quarter of the budget (the `_chunk_text` rule). (text, was_cut)."""
    if len(text) <= budget:
        return text, False
    floor = budget // 4
    window = text[:budget]
    cut = window.rfind("\n\n")
    if cut >= floor:
        return text[:cut].rstrip(), True
    last = None
    for m in _SENTENCE_END.finditer(window):
        if m.end() >= floor:
            last = m.end()
    if last is not None:
        return text[:last].rstrip(), True
    return window.rstrip(), True


def render_step_card(parts: StepCardParts, *, collapsible: str) -> str:
    """One top-level comment per step (ADR-0042 §2): header line, the
    Dev's answer quoted and cut at a boundary, Result/Next, the transcript
    line (its backticked file token is the seq-derivation surface — exactly
    one per card), then the fold. Sentinel-free: `_feed` seals it."""
    lines = [card_header(parts.glyph, parts.seq, parts.mission_type,
                         parts.outcome_word, parts.duration_s, parts.cost_usd)]
    answer = (parts.answer_md or "").strip()
    if answer:
        cut, was_cut = cut_at_boundary(answer)
        if was_cut:
            cut = cut + "\n\n" + ANSWER_CUT_NOTE
        lines.append(blockquote(cut))
    lines.append(f"**Result:** {parts.result_line}\n**Next:** {parts.next_line}")
    if parts.transcript_url:
        lines.append(f"Transcript: `{parts.transcript_name}` — "
                     f"[download]({parts.transcript_url})")
    else:
        lines.append(f"Transcript: `{parts.transcript_name}` "
                     "(inline, in the details below)")
    sections = list(parts.sections)
    if not parts.transcript_url:
        dump = blockquote(f"---\n\n{parts.transcript_md or ''}")
        inline = FoldSection(SECTION_TRANSCRIPT_INLINE, dump)
        # before the Run section, after every marker-bearing section, so a
        # paged card keeps its markers on the earliest pages
        run_at = next((i for i, s in enumerate(sections)
                       if s.title == SECTION_RUN), len(sections))
        sections.insert(run_at, inline)
    lines.append(render_fold(sections, collapsible=collapsible))
    return "\n\n".join(lines)


# ── notices ───────────────────────────────────────────────────────────────

NEEDS_YOU = "✋ **Needs you.**"
FOR_THE_RECORD = "⚠️ **For the record.**"
INFO = "ℹ️"


def directive_lead(glyph: str, title: str) -> str:
    return f"{glyph} **{title}.**"


def leads_lead(src_key: str, step: int) -> str:
    return f"📨 **Leads from {src_key}, step {step}.**"


def record_section(legacy_body: str, at: datetime | None = None) -> FoldSection:
    """Today's comment text, verbatim minus the sentinel — the section the
    projection emits as the Dev's entry (ADR-0042 amendment: the fold
    always carries the record; the head is for people)."""
    return FoldSection(SECTION_RECORD, unseal(legacy_body).strip("\n"), at)


def render_notice(lead: str, what: str, *, todo: str = "",
                  sections: Sequence[FoldSection] = (),
                  collapsible: str) -> str:
    """A comment written to a person: lead + one sentence + what to do,
    then the fold with the record. Sentinel-free: `_feed` seals it."""
    head = f"{lead} {what}".strip()
    parts = [head]
    if todo:
        parts.append(todo)
    if sections:
        parts.append(render_fold(sections, collapsible=collapsible))
    return "\n\n".join(parts)


def notice(mgr, lead: str, what: str, record: str, *, todo: str = "",
           sections: Sequence[FoldSection] = ()) -> str:
    """THE notice at every call site (ADR-0042 §4, the one rule): the head
    — `lead`, one short sentence, an optional what-to-do — is new prose for
    a person; the fold's `Record` section is `record`, today's comment
    text exactly as the site built it, markers included on their own
    lines, so every scan reads what it read and the Dev's ACTIVITY.md
    unfolds to the byte-identical legacy entry. The head never carries a
    backticked marker, a step-file token, a `Part i of n` line or a line
    opening with `**` — a second copy of a marker would double-count a
    set-valued scan, and the head is not the record. Sentinel-free:
    `_feed` seals it."""
    return render_notice(lead, what, todo=todo,
                         sections=[record_section(record), *sections],
                         collapsible=collapsible_of(mgr))


# ── the status comment (a view) ───────────────────────────────────────────

def is_stall_notice(body: str | None) -> bool:
    """A stalls.py notice (DevCake cannot start the mission): a person's
    notification, never Dev context — the projection drops it."""
    return bool(body) and bool(STALL_MARKER_RE.search(unquoted(body)))


def is_status_comment(body: str | None) -> bool:
    return is_devcake_comment(body) and bool(STATUS_MARKER_RE.search(unquoted(body)))


def find_status_entry(entries) -> str | None:
    """The OLDEST DevCake entry carrying the status marker with an id —
    oldest so two instances that both created one converge on the same."""
    for e in entries:
        if e.entry_id and is_status_comment(e.body):
            return e.entry_id
    return None


# ── the projection: the Dev's folder is unfolded from the record ─────────

@dataclass
class Projected:
    """An ACTIVITY.md entry as the mirror renders it — an ActivityEntry's
    fields plus `source`, the raw first entry it came from (the identity
    `coalesced_step_files` keys its step files on)."""
    ts: datetime
    author: str
    kind: str
    body: str
    attachments: list
    entry_id: str | None
    parent_id: str | None
    source: object


def _seal(body: str) -> str:
    return body.rstrip("\n") + "\n\n" + COMMENT_SENTINEL


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _projected(entry, body: str, *, ts: datetime | None = None,
               attachments: list | None = None) -> Projected:
    return Projected(ts=ts or entry.ts, author=entry.author, kind=entry.kind,
                     body=body,
                     attachments=list(entry.attachments) if attachments is None
                     else attachments,
                     entry_id=entry.entry_id, parent_id=entry.parent_id,
                     source=entry)


def legacy_transcript_body(name: str, run_id: str, url: str | None,
                           answer_bq: str, inline_bq: str) -> str:
    """The pre-card transcript comment, byte for byte (finalize's
    `_post_transcript`): pointer + quoted answer with an attachment, header
    + quoted dump without one."""
    if url:
        body = (f"🧾 DevCake transcript `{name}` (run `{run_id}`) — "
                f"attached: [{name}]({url})")
        return body + ("\n\n" + answer_bq if answer_bq else "")
    return f"🧾 DevCake transcript `{name}` (run `{run_id}`)\n\n" + inline_bq


def legacy_answer_body(answer_bq: str) -> str:
    """The pre-card answer comment (`_post_reply`): the reply marker and the
    quoted answer, its cut note pointing at the transcript on the issue."""
    return REPLY_MARKER + "\n\n" + answer_bq.replace(
        ANSWER_CUT_NOTE, _LEGACY_REPLY_CUT_NOTE)


def _split_attachments(entry, transcript_name: str):
    """(transcript refs, discovery refs, others) by attachment name."""
    tr, disc, other = [], [], []
    for att in entry.attachments:
        name = (att.name or "").rsplit("/", 1)[-1]
        if name == transcript_name:
            tr.append(att)
        elif name.startswith("DISCOVERY_") and name.endswith(".md"):
            disc.append(att)
        else:
            other.append(att)
    return tr, disc, other


def unfold_card(group: list, reconstructed: str) -> list[Projected] | None:
    """A step card → the entries the folder carried before ADR-0042:
    transcript (with the quoted answer and the step file), answer (when
    the card carries the answer token), then one entry per record section
    in fold order — each at the card's time unless the section is a late
    append. None when the body is not a card."""
    first = group[0]
    lines = reconstructed.splitlines()
    if not lines or not CARD_RE.match(lines[0]):
        return None
    head, inner = strip_fold(reconstructed)
    head_lines = head.splitlines()
    tline = next((TRANSCRIPT_LINE_RE.match(l) for l in head_lines
                  if TRANSCRIPT_LINE_RE.match(l)), None)
    if tline is None:
        return None
    name, url = tline.group("name"), tline.group("url")
    # the quoted answer: the maximal run of `>` lines before **Result:**
    quoted = []
    for line in head_lines[1:]:
        if line.startswith("**Result:**"):
            break
        if line.lstrip().startswith(">"):
            quoted.append(line)
    answer_bq = "\n".join(quoted)
    sections = fold_sections(inner)
    by_title = {s.title: s for s in sections}
    run_id = ""
    if SECTION_RUN in by_title:
        run_id = by_title[SECTION_RUN].body.strip().strip("`")
    inline_bq = (by_title[SECTION_TRANSCRIPT_INLINE].body
                 if SECTION_TRANSCRIPT_INLINE in by_title else "")
    tr_refs, disc_refs, other_refs = _split_attachments(first, name)
    out = [_projected(first, _seal(legacy_transcript_body(
        name, run_id, url, answer_bq, inline_bq)),
        attachments=tr_refs + other_refs)]
    if SECTION_ANSWER in by_title and answer_bq:
        out.append(_projected(first, _seal(legacy_answer_body(answer_bq)),
                              attachments=[]))
    for sec in sections:
        if sec.title in _SILENT_SECTIONS:
            continue
        refs = disc_refs if sec.title == SECTION_DISCOVERIES else []
        out.append(_projected(first, _seal(sec.body), ts=sec.at, attachments=refs))
    return out


def unfold_notice(group: list, reconstructed: str) -> list[Projected]:
    """A DevCake comment with a fold → its Record section as the entry (the
    legacy body, verbatim) plus every late section as its own entry; a
    fold without a Record section keeps the head as the entry. No fold ⇒
    the comment passes through unchanged."""
    first = group[0]
    head, inner = strip_fold(reconstructed)
    if inner is None:
        # no fold: a pre-card comment (paged or not) passes through page by
        # page — the folder keeps every legacy body, labels included
        return [_projected(e, e.body or "") for e in group]
    sections = fold_sections(inner)
    record = next((s for s in sections if s.title == SECTION_RECORD), None)
    out = [_projected(first, _seal(record.body if record is not None else head))]
    for sec in sections:
        if sec is record or sec.at is None:
            continue
        out.append(_projected(first, _seal(sec.body), ts=sec.at, attachments=[]))
    return out


def unfold_entries(entries) -> list[Projected]:
    """The mirror's entry list (ADR-0042 §8): cards unfold into the legacy
    sequence, folded notices into their record, the status comment is
    omitted (a view — every fact in it is already here; it stays on the
    watermark), everything else passes through. Late sections land at
    their own time (stable sort — vendor entries are already ascending)."""
    out: list[Projected] = []
    for group, reconstructed in _page_groups(entries):
        first = group[0]
        if reconstructed is None:
            out.extend(_projected(e, e.body or "") for e in group)
            continue
        if is_status_comment(first.body) or is_stall_notice(first.body):
            continue
        card = unfold_card(group, reconstructed)
        if card is not None:
            out.extend(card)
            continue
        out.extend(unfold_notice(group, reconstructed))
    out.sort(key=lambda p: _aware(p.ts))
    return out


# ── the multi-file attachment pipe ───────────────────────────────────────

async def post_attachments_comment(mgr, pmo_id: str, kind: str, *,
                                   files: Sequence[tuple[str, str]],
                                   comment_of,
                                   reply_to: str | None = None) -> str | None:
    """`post_attachment_comment` for several files at once (a step card
    carries the transcript AND the discovery record): uploads each in
    order, then posts `comment_of(urls)` where `urls` maps filename → url
    or None (that upload failed → the builder's inline form for it)."""
    urls: dict[str, str | None] = {}
    supported = _attachments_supported(mgr)
    for filename, content in files:
        url = None
        if supported:
            try:
                url = await mgr.pmo.upload_attachment(pmo_id, filename,
                                                      content.encode())
            except Exception:  # noqa: BLE001 — fall back to the builder's inline form
                log.exception("attachment upload failed for %s — inline fallback",
                              filename)
        urls[filename] = url
    body, externalize = comment_of(urls)
    return await mgr._feed(pmo_id, kind, body, externalize=externalize,
                           reply_to=reply_to)
