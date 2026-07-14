"""Orchestrator constants and feed/transition markers (docs/03, docs/04)."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..model import (LABEL_EXECUTE, LABEL_PLAN, LABEL_REVIEW, MissionType)

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
    "ONBOARD": frozenset({"plan_needed", "executed_trivially", "decomposed",
                          "human_needed"}),
    "PLAN": frozenset({"planned"}),
    "EXECUTE": frozenset({"executed", "human_needed"}),
    "REVIEW": frozenset({"reviewed", "human_needed"}),
}

STEP_MARKER = re.compile(r"`(\d+)_(ONBOARD|PLAN|EXECUTE|REVIEW)\.md`")

# Stage label each checkpointed swap leaves on the mission (None = stage label
# removed). Consulted by the redelivery external-transition check in
# _transition: a live stage matching a present marker's value is our own swap
# resuming, anything else is an external change and halts the finalize.
# Stage-label-swapping checkpoints MUST be registered here, or their
# redeliveries will misread the swap as external (cosmetic skip, safe).
_SWAP_MARKER_STAGE: dict[str, str | None] = {
    "transition:planned:labels": LABEL_EXECUTE,
    "transition:executed:labels": LABEL_REVIEW,
    "transition:executed_trivially:labels": LABEL_REVIEW,
    "transition:plan_needed_attach:labels": LABEL_EXECUTE,
    "transition:plan_needed": LABEL_PLAN,
    "review:reject:labels": LABEL_EXECUTE,
    "review:conflict_routed": LABEL_EXECUTE,
    "review:done": None,           # REVIEW removed, mission done
    "review:merge_failed": None,   # REVIEW→MERGE; MERGE is not a stage label
    "review:merge_deferred": None,
}

# docs/05 §4: feed comments longer than this are uploaded as .md attachments
# and referenced from a short comment. docs/07 §2 externalizes long bodies
# into the Dev's activity folder at the same threshold, so Devs always see
# full content either way.
FEED_INLINE_MAX = 2048

# docs/03 §4.1 — merge-failure state markers, counted/located from the feed
# so the state stays fully PMO-derivable (no local clocks or counters). The
# comments carrying them are short by construction (< FEED_INLINE_MAX): the
# markers must stay inline, never externalized to attachments. NOTE:
# get_activity reads the newest 100 comments (docs/05 §3, v0 limit) — markers
# could age out on an extremely chatty mission.
CONFLICT_MARKER = re.compile(r"`devcake:conflict-resolve:(\d+)`")
MERGE_RETRY_MARKER = "`devcake:merge-retry`"
MERGE_HANDOFF_MARKER = "`devcake:merge-handoff`"
MAX_CONFLICT_RESOLVES = 2

# Comment-provenance sentinel (docs/03 §8a, ADR-0007): every comment DevCake
# posts ends with this footer. Classification is content-based, NEVER
# author/credential-based — DevCake may post with the operator's own PMO key.
COMMENT_SENTINEL = "`devcake:v1`"
SENTINEL_RE = re.compile(r"`devcake:v1`\s*$")
DECOMPOSITION_MARKER_RE = re.compile(
    r"`devcake:decomposition:v1 parent=(\S+) manifest=([0-9a-f]{64}) "
    r"part=(\d+)/(\d+)`"
)

AUDIT_PATH = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "state" / "events.jsonl"
