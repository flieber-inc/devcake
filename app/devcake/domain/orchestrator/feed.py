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
from .markers import COMMENT_SENTINEL, FEED_INLINE_MAX, SENTINEL_RE

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


def _audit(mgr, pmo_id: str, action: str, detail: str = "") -> None:
    # Belt-and-braces: detail should be names/counts only, but exception
    # fragments (e.g. activity_repo_push_failed) can embed secret shapes —
    # match settings_bundle.audit_event so on-disk JSONL is scrubbed too.
    detail = redact(detail)
    markers.AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(markers.AUDIT_PATH, "a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                            "instance": getattr(mgr, "instance_name", ""),
                            "pmo_id": pmo_id, "action": action, "detail": detail}) + "\n")
    mgr._grace_next.add(pmo_id)
    # mirror every audit action as a span so OO alerts can fire on them
    # (ISSUES #23: `devcake_needs_human` was a file-only record no alert
    # could ever see). One span name, action as attribute — the alert set
    # queries devcake_audit_action.
    with tracer.start_as_current_span("audit.event") as span:
        span.set_attribute("devcake.audit.action", action)
        span.set_attribute("devcake.pmo.id", pmo_id)
        span.set_attribute("devcake.audit.detail", detail[:500])


def _trip_breaker(mgr, name: str, reason: str) -> None:
    """Single choke point for tripping a breaker: sets the in-memory dict
    AND emits a span — breakers had no telemetry at all, so the documented
    DEV_AUTH alert could never fire (ISSUES #23)."""
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
    long text already lives in its own attachment); the sentinel
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
    if externalize and len(markdown) > FEED_INLINE_MAX:
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
    await mgr.pmo.post_feed(
        MissionRef(pmo_id, "issue"),
        markdown.rstrip() + "\n\n" + COMMENT_SENTINEL)


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

