"""Orchestrator constants and feed/transition markers (docs/03, docs/04)."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..model import LABEL_CREATED, MissionType
from . import steps

# The full state machine is dispatchable, projects included (ADR-0006).
DISPATCHABLE_TYPES = {MissionType.ONBOARD, MissionType.PLAN,
                      MissionType.EXECUTE, MissionType.REVIEW}

# docs/03 §6 (normative) — the app-side trust boundary: a run may only
# transition through outcomes legal for its mission type. Devs ingest untrusted
# text (mission descriptions, human comments), so a forged outcome must never
# let an EXECUTE run approve its own work or an ONBOARD run skip REVIEW. The
# entrypoint mirrors this table, but old images may run — the app check is the
# invariant.
LEGAL_OUTCOMES: dict[str, frozenset[str]] = {
    "ONBOARD": frozenset({"plan_needed", "decomposed",
                          "human_needed"}),
    "PLAN": frozenset({"planned"}),
    "EXECUTE": frozenset({"executed", "human_needed"}),
    "REVIEW": frozenset({"reviewed", "human_needed"}),
}

# IRON RULE (ADR-0014 D2): every scan over feed bodies runs on
# feed._unquoted(body) — `>`-quoted lines never count, for humans quoting
# DevCake comments and for blockquoted model text alike. Quoting is the ONE
# quarantine convention; a new feed scan that reads raw bodies is a bug.
STEP_MARKER = re.compile(r"`(\d+)_(ONBOARD|PLAN|EXECUTE|REVIEW)\.md`")

# Stage label each checkpointed swap leaves on the mission (None = stage label
# removed). Consulted by the redelivery external-transition check in
# _transition: a live stage matching a present marker's value is our own swap
# resuming, anything else is an external change and halts the finalize.
# DERIVED from the step registry (ADR-0034) — a new stage-swapping checkpoint
# declares its stage_after at registration in steps.py; the old hand-copy
# ("MUST be registered here") cannot drift anymore.
_SWAP_MARKER_STAGE: dict[str, str | None] = steps.swap_marker_stage()

# docs/05 §4: feed comments longer than this are uploaded as .md attachments
# and referenced from a short comment (POST-time only — the Dev-side mirror
# inlines full bodies verbatim, ADR-0014 D3). Also the finalize post's
# inline-last-message truncation bound.
FEED_INLINE_MAX = 2048

# The mission's answer, carried on its own issue comment (docs/00 §1: the PMO
# is the single source of truth, so the feed is where downstream consumers —
# human, script, or bot, none named here — look for it). Public cross-repo
# contract, docs/05 §4: consumers match on `startswith`; treat the string as
# frozen. The marker must be the FIRST thing in the comment — startswith is
# what keeps a quoted or relayed copy of this string from ever classifying as
# the real answer (composes with the ADR-0014 D2 quarantine). Producer rules
# (finalize `_post_reply`): issues only; empty last message ⇒ no post;
# REVIEW/`reviewed` suppressed so approval noise cannot displace the EXECUTE
# answer; body blockquoted (ADR-0014); externalize=False; truncation points
# at the step transcript, not a non-existent attachment.
REPLY_MARKER = "<!-- DEVCAKE-REPLY -->"

# The deliverable-zip feed note, marked so any feed consumer can classify it
# as packaging bookkeeping — an auth-walled zip link must never be mistaken
# for the mission's answer. Same startswith contract as REPLY_MARKER. Wording
# says the zip is the audit copy, not the answer (docs/05 §4).
DELIVERABLE_MARKER = "<!-- DEVCAKE-DELIVERABLE -->"

# docs/03 §4.1 — merge-failure state markers, counted/located from the feed
# so the state stays fully PMO-derivable (no local clocks or counters). The
# comments carrying them are short by construction (< FEED_INLINE_MAX): the
# markers must stay inline, never externalized to attachments. NOTE:
# get_activity cursor-walks newest-first with a fail-loud ceiling of ~1,000
# comments (10 pages, docs/05 §3) — markers could age out only on an
# extraordinarily chatty mission, ~50× DevCake's post-hygiene comment rate.
# Per-mission repo override (M10, founder decision): a backticked line
# anywhere in the mission DESCRIPTION — `devcake-repo:<name>`. A description
# marker, not a label: repo names are an open-ended operator-renamable set,
# while the managed-label set is deliberately fixed (and Linear project
# labels leak workspace-wide). Mirrors the decomposition-marker precedent.
REPO_MARKER = re.compile(r"`devcake-repo:([a-z][a-z0-9]{0,11})`", re.IGNORECASE)
# the permissive twin (audit A26): anything devcake-repo:-SHAPED that the
# strict pattern rejects is a typo'd routing intent — resolution GATES it
# instead of silently falling through to the instance default
RAW_REPO_MARKER = re.compile(r"`devcake-repo:([^`]*)`", re.IGNORECASE)

CONFLICT_MARKER = re.compile(r"`devcake:conflict-resolve:(\d+)`")
MERGE_RETRY_MARKER = "`devcake:merge-retry`"
MERGE_HANDOFF_MARKER = "`devcake:merge-handoff`"
MAX_CONFLICT_RESOLVES = 2

# ADR-0031 — the Freshness Gate's re-review directive, counted exactly like
# CONFLICT_MARKER (max over unquoted feed bodies; PMO-derivable, restart-
# proof). Inherited marker doctrine, docs/03 §4.1: a human deleting the
# directive comments deliberately resets the count, and a human PASTING one
# unquoted inflates it toward exhaustion — humans own the feed. The directive
# comment must stay under FEED_INLINE_MAX so the marker is never externalized
# into an attachment.
FRESHNESS_MARKER = re.compile(r"`devcake:freshness-rereview:(\d+)`")
# The ONLY terminator of the re-review loop (per mission lifetime, like the
# conflict budget). A constant, not operator config, until live data
# demonstrates a need — knobs are debt too (docs/08 §1).
MAX_FRESHNESS_REREVIEWS = 2
# ADR-0031 Decision 3 — sentinel'd feed entries matching one of these regexes
# are material to the Freshness Gate DESPITE being DevCake-posted. Shipped
# EMPTY; the routed DISCOVERY-IN class joins when discovery routing ships.
# Nothing is elevated implicitly.
ELEVATED_MARKERS: list[re.Pattern[str]] = []

# ADR-0032 — the HANDOFF note: a marked section APPENDED to a completed
# mission's description at approve-finalize (description, not feed: zero
# extra PMO calls at dispatch — blocker Missions are already fetched whole —
# and immune to the feed-truncation direction problem). A DESCRIPTION marker
# like `devcake-repo:` and the decomposition footer, by the same precedent.
# Parse rule: the LAST marker line wins (appends accumulate across
# re-approves; the founder amends by editing in place — human-authored
# marker lines are a feature, not a forgery, because the description is
# operator-owned). Model-authored text is redacted AND backtick-defanged
# before the append, so a handoff can never smuggle a live marker.
HANDOFF_MARKER = "`devcake:handoff:v1`"
# App-side cap at append time — the entrypoint does not bound handoff_md,
# and a vendor description-cap failure must stay in best-effort territory.
HANDOFF_APPEND_MAX = 4000
# Per-blocker excerpt bound in the prompt note and MISSION.md.
HANDOFF_EXCERPT_MAX = 700


def handoff_of(description: str | None) -> str:
    """The mission's current handoff note: text after the LAST handoff
    marker line, up to the next `---` rule or the end. "" when absent."""
    text = description or ""
    idx = text.rfind(HANDOFF_MARKER)
    if idx < 0:
        return ""
    body = text[idx + len(HANDOFF_MARKER):]
    cut = body.find("\n---")
    if cut >= 0:
        body = body[:cut]
    return body.strip()

# Comment-provenance sentinel (docs/03 §8a, ADR-0007): every comment DevCake
# posts ends with this footer. Classification is content-based, NEVER
# author/credential-based — DevCake may post with the operator's own PMO key.
COMMENT_SENTINEL = "`devcake:v1`"
SENTINEL_RE = re.compile(r"`devcake:v1`\s*$")
# depth= is optional for backward compatibility: markers written under the
# depth-1 regime could only ever mark level-1 children, so absent ⇒ 1
# (ADR-0012). The PMO record itself holds the depth — no internal counters.
DECOMPOSITION_MARKER_RE = re.compile(
    r"`devcake:decomposition:v1 parent=(\S+) manifest=([0-9a-f]{64}) "
    r"part=(\d+)/(\d+)(?: depth=(\d+))?`"
)


def decomposition_marker(description: str | None) -> re.Match | None:
    """A mission's own decomposition marker: the LAST match in the
    description. The app appends the genuine footer AFTER the Dev-authored
    draft body, so marker-shaped text quoted inside the untrusted body can
    only ever precede it — anchoring to the last match keeps every marker
    read pinned to the app's own write. (New child bodies are additionally
    defanged at creation; last-match covers children created before that.)"""
    matches = list(DECOMPOSITION_MARKER_RE.finditer(description or ""))
    return matches[-1] if matches else None


def decomposition_depth(mission) -> int | None:
    """Generations of decomposition above `mission`, read from its own PMO
    record only. The app-managed DEVCAKE-CREATED label gates the read, so a
    forged marker in an untrusted description is inert (depth 0 without the
    label). None = the label is present but the marker is missing or
    unparseable — callers treat unknown as at-limit (fail-safe)."""
    if LABEL_CREATED not in mission.labels:
        return 0
    marker = decomposition_marker(mission.description)
    if not marker:
        return None
    return int(marker.group(5)) if marker.group(5) else 1


def at_decomposition_limit(mission, limit: int) -> bool:
    """THE at-limit predicate (ADR-0012), shared by the finalizer's depth
    gate and dispatch's {decomposition_rule} builder so the two can never
    drift: 0 = unlimited; unknown depth counts as at-limit, fail-safe."""
    if not limit:
        return False
    depth = decomposition_depth(mission)
    return depth is None or depth >= limit

AUDIT_PATH = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "state" / "events.jsonl"

# ── give-up watermark reader (2026-08 evaluation F8) ─────────────────────────
# The old reader re-parsed the ENTIRE append-only audit log once per candidate
# mission per poll cycle — O(missions × log size) JSON on a file only
# clear-runs ever truncates. This incremental reader parses only the appended
# tail: the module-level state carries (path, byte offset, per-pmo marks) and
# resets itself when the path changes (tests monkeypatch AUDIT_PATH) or the
# file shrank (clear-runs truncation). Safe without locks: the writer
# (feed._audit) is synchronous inside the one event loop, so a reader never
# observes a partial line.
_GIVEUP_STATE: dict = {"path": None, "offset": 0, "marks": {}}


def last_giveup_at(pmo_id: str):
    """Timestamp of the newest `devcake_failed` audit event for pmo_id, or
    None. Incremental over AUDIT_PATH (see block comment)."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    st = _GIVEUP_STATE
    path = AUDIT_PATH
    try:
        size = path.stat().st_size
    except OSError:
        st.update(path=path, offset=0, marks={})
        return None
    if st["path"] != path or size < st["offset"]:
        st.update(path=path, offset=0, marks={})
    if size > st["offset"]:
        with open(path) as f:
            f.seek(st["offset"])
            for line in f:
                try:
                    e = _json.loads(line)
                    if e.get("action") == "devcake_failed":
                        ts = _dt.fromisoformat(e["ts"])
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=_tz.utc)
                        st["marks"][e.get("pmo_id")] = ts
                except Exception:  # noqa: BLE001 — one bad audit line must never halt scheduling
                    continue
            st["offset"] = f.tell()
    return st["marks"].get(pmo_id)
