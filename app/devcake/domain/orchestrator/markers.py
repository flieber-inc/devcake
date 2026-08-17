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
# feed.unquoted(body) — `>`-quoted lines never count, for humans quoting
# DevCake comments and for blockquoted model text alike. Quoting is the ONE
# quarantine convention; a new feed scan that reads raw bodies is a bug.
STEP_MARKER = re.compile(r"`(\d+)_(ONBOARD|PLAN|EXECUTE|REVIEW)\.md`")
PLAN_FILE = re.compile(r"`(PLAN_\d+\.md)`")
# Producer: feed._part_label. Consumers must use this exact line.
PART_LINE = re.compile(r"^Part (\d+) of (\d+)$")

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
# The re-review budget itself lives at AppConfig.budgets.freshness_rereviews
# (ADR-0033 Decision 7 as amended, founder ruling 2026-08-13): counting
# budgets are operator knobs sized to the board, 0 = unlimited. The COUNT
# stays marker-derived from the feed — only the bound moved to config.
# ── ADR-0033 — discovery markers (the counterflow lane) ─────────────────────
# Harvest memorializes a run's `discoveries` on the SOURCE mission's feed:
# the full DISCOVERY_<seq>.md is always attached as a step deliverable
# (founder ruling 2026-08-13) and the marked comment is the scan surface.
# Counted, never trusted; posted with externalize=False so the marker can
# never leave the feed body; a human deleting the comment deliberately
# resets the derived state. Parameters ride inside the backticked token
# (decomposition-marker precedent): step= is the Mission Step seq, n= the
# entry count — pending detection stays board-derivable without opening
# attachments. NOT in ELEVATED_MARKERS: a
# mission's own discovery post must never trip its own freshness gate.
DISCOVERY_MARKER_RE = re.compile(r"`devcake:discovery:v1 step=(\d+) n=(\d+)`")
# Routed-receipt twin, posted on the source feed by the ROUTING half
# (ADR-0033 PR-2) once a step's discoveries are dispositioned: pending =
# posted − receipted, pure board arithmetic from day one.
DISCOVERY_ROUTED_MARKER_RE = re.compile(
    r"`devcake:discovery-routed:v1 step=(\d+) to=(\S+)`")
# Byte bounds, not operator knobs (AppConfig.budgets owns the COUNTS):
# per-field cap inside DISCOVERY_<seq>.md, per-entry preview bound in the
# source comment.
DISCOVERY_FIELD_MAX = 2000
DISCOVERY_PREVIEW_MAX = 200


def discovery_marker(seq: int, n: int) -> str:
    """Render twin of DISCOVERY_MARKER_RE (round-trip pinned in tests)."""
    return f"`devcake:discovery:v1 step={seq} n={n}`"


def discovery_posts(unquoted_text: str) -> list[tuple[int, int]]:
    """(step, n) for every discovery marker in unquoted feed text. The
    parameter name is the contract: callers pass feed.unquoted(body) — this
    module cannot import feed (cycle), so the IRON RULE is enforced at the
    call site."""
    return [(int(m.group(1)), int(m.group(2)))
            for m in DISCOVERY_MARKER_RE.finditer(unquoted_text)]


def discovery_receipts(unquoted_text: str) -> set[tuple[int, str]]:
    """(step, target) for every routed receipt in unquoted feed text —
    set-cardinality shape (distinct deliveries), not max-of-int."""
    return {(int(m.group(1)), m.group(2))
            for m in DISCOVERY_ROUTED_MARKER_RE.finditer(unquoted_text)}


# Delivery-side marker (ADR-0033 routing): one comment per recipient per
# steward run, one marker line per source batch — src= is the source
# mission KEY, step= its Mission Step. Set-cardinality counting: the
# distinct-pair dedup (a pair already on the recipient feed is never
# re-delivered) IS the fan-out bound — no numeric budget (addendum 14).
# A human deleting a delivery comment deliberately reopens that pair. The
# comment is posted externalize=False; long finding bodies are excerpted
# (DISCOVERY_IN_EXCERPT_MAX) with a pointer to the source's DISCOVERY_<n>.md
# attachment, so the marker can never be pushed off the feed body.
DISCOVERY_IN_MARKER_RE = re.compile(
    r"`devcake:discovery-in:v1 src=(\S+) step=(\d+)`")
DISCOVERY_IN_EXCERPT_MAX = 700


def discovery_in_keys(unquoted_text: str) -> set[tuple[str, int]]:
    """(src key, step) pairs delivered to a recipient, parsed from unquoted
    feed text (caller applies feed.unquoted — the IRON RULE call-site
    contract, same as discovery_posts)."""
    return {(m.group(1), int(m.group(2)))
            for m in DISCOVERY_IN_MARKER_RE.finditer(unquoted_text)}


# ADR-0031 Decision 3 — sentinel'd feed entries matching one of these
# regexes are material to the Freshness Gate DESPITE being DevCake-posted.
# First member (ADR-0033): a routed discovery landing after a REVIEW's
# context was assembled counts as material at the context-closing finalize —
# the seam doing exactly what it was built for. The SOURCE-side
# devcake:discovery:v1 marker is deliberately NOT here: a mission's own
# discovery post must never trip its own gate. Nothing is elevated
# implicitly.
ELEVATED_MARKERS: list[re.Pattern[str]] = [DISCOVERY_IN_MARKER_RE]

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


def defang(text: str) -> str:
    """The ONE marker-neutralizing transformation for model-authored text on
    any append path (ADR-0032 D3, ADR-0033 consequences): strip the opening
    backtick from `devcake:…` / `devcake-repo:…` tokens so quoted markers
    keep their words but lose their teeth — every scan matches the backticked
    form only. Extracted from review._append_handoff before harvest grew a
    second copy (the multi-site drift ADR-0034 exists to prevent)."""
    return text.replace("`devcake:", "devcake:").replace(
        "`devcake-repo:", "devcake-repo:")


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


def decomposition_parent_ref(mission) -> str | None:
    """Trusted decomposition parent id/key from a mission's own record.

    ONE parent-ref read for family gate, family_of, and any other consumer
    (chokepoint): the app-managed DEVCAKE-CREATED label gates the read so a
    forged marker in an untrusted description never joins a family or holds
    the family gate. Returns the marker's parent= token (pmo_id; key is a
    defensive alias at the caller's resolve step), or None when untrusted
    or unreadable."""
    if LABEL_CREATED not in mission.labels:
        return None
    marker = decomposition_marker(mission.description)
    return marker.group(1) if marker else None


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
