"""Delivery destination (ADR-0017 addendum): the marker grammar, the
description-append chokepoint, and the dispatch snapshot."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from devcake.domain.model import MissionRef, MissionType
from devcake.domain.orchestrator import feed
from devcake.domain.orchestrator.markers import (
    DELIVERY_MARKER_RE, DELIVERY_REPOSITORY, DELIVERY_TICKET, HANDOFF_MARKER,
    delivery_marker, delivery_of, delivery_recorded)


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ── grammar ──────────────────────────────────────────────────────────────────

def test_delivery_marker_bytes_are_pinned():
    # canary: the wire bytes of the record — a change here is a record
    # migration, never a refactor
    assert delivery_marker("ticket") == "`devcake:delivery:v1 to=ticket`"
    assert delivery_marker("repository") == "`devcake:delivery:v1 to=repository`"
    assert DELIVERY_MARKER_RE.fullmatch(delivery_marker("ticket"))
    with pytest.raises(ValueError):
        delivery_marker("archive")


def test_delivery_of_defaults_to_repository_and_last_marker_wins():
    assert delivery_of(None) == DELIVERY_REPOSITORY
    assert delivery_of("plain brief") == DELIVERY_REPOSITORY
    assert not delivery_recorded("plain brief")
    desc = ("brief\n\n---\n`devcake:delivery:v1 to=ticket`\nReason: a report\n"
            "\n---\n`devcake:delivery:v1 to=repository`\n")
    assert delivery_of(desc) == DELIVERY_REPOSITORY      # the person reverted
    assert delivery_recorded(desc)
    assert delivery_of("x `devcake:delivery:v1 to=ticket` y") == DELIVERY_TICKET


def test_delivery_of_ignores_defanged_prose():
    # a Dev quoting the marker in a body loses the backtick on every append
    # path (feed.marked_note) — the scan matches the backticked form only
    assert delivery_of("devcake:delivery:v1 to=ticket (quoted)") == DELIVERY_REPOSITORY


# ── the description-append chokepoint ────────────────────────────────────────

def test_marked_note_redacts_before_capping_and_defangs():
    secret = "ghp_" + "A" * 36
    body = "see `devcake:delivery:v1 to=ticket` and token " + secret + " tail"
    note = feed.marked_note(HANDOFF_MARKER, body, cap=60)
    assert note.startswith("\n\n---\n" + HANDOFF_MARKER + "\n")
    assert secret not in note and "ghp_A" not in note      # redacted, not split
    assert "`devcake:delivery:v1" not in note              # defanged
    assert "devcake:delivery:v1 to=ticket" in note          # words kept
    assert note.endswith("\n")


def test_marked_note_bytes_match_the_legacy_handoff_shape():
    # the handoff note's shape is a record (markers.handoff_of parses it);
    # routing it through the chokepoint changed no byte
    assert feed.marked_note(HANDOFF_MARKER, "what changed", cap=4000) == (
        "\n\n---\n" + HANDOFF_MARKER + "\nwhat changed\n")


class _PMO:
    def __init__(self, fail=False):
        self.fail = fail
        self.appended = []

    async def append_description(self, ref, text):
        if self.fail:
            raise RuntimeError("description at the vendor cap")
        self.appended.append((ref.pmo_id, text))


class _Mgr:
    def __init__(self, pmo):
        self.pmo = pmo
        self.audits = []

    def _audit(self, pmo_id, action, detail=""):
        self.audits.append((pmo_id, action, detail))


def test_append_note_returns_true_and_appends():
    mgr = _Mgr(_PMO())
    ok = run_coro(feed.append_note(mgr, MissionRef("p1", "issue"), "\n\n---\nnote",
                                   audit_action="x_failed"))
    assert ok is True and mgr.pmo.appended == [("p1", "\n\n---\nnote")]
    assert mgr.audits == []


def test_append_note_failure_audits_and_returns_false_never_raises():
    mgr = _Mgr(_PMO(fail=True))
    ok = run_coro(feed.append_note(mgr, MissionRef("p1", "issue"), "n",
                                   audit_action="delivery_note_failed"))
    assert ok is False
    assert mgr.audits == [("p1", "delivery_note_failed",
                           "description at the vendor cap")]


# ── the dispatch snapshot ────────────────────────────────────────────────────

def test_dispatch_snapshots_the_recorded_destination(tmp_path):
    from test_prompt_templates import _ForgeWithDescriptor
    from test_transitions import make_mgr, mission

    m = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    m.description = "brief\n\n---\n`devcake:delivery:v1 to=ticket`\nReason: a report\n"
    from devcake.config import PMOInstance
    mgr, fake, _store = make_mgr(tmp_path, m, forge=_ForgeWithDescriptor())
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["main"])
    launched = []

    async def launch(run, image):
        launched.append(run)
    mgr.runs.bootstrap = type("B", (), {"launch": staticmethod(launch)})()
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE, mgr.dev_types["senior-dev"]))
    assert run is not None and launched
    assert run.delivery_to == DELIVERY_TICKET

    m2 = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr2, _fake2, _s2 = make_mgr(tmp_path / "b", m2, forge=_ForgeWithDescriptor())
    mgr2.instance = PMOInstance(name="linear", team_key="DEV", repos=["main"])
    mgr2.runs.bootstrap = type("B", (), {"launch": staticmethod(launch)})()
    run2 = run_coro(mgr2.dispatch(m2, MissionType.EXECUTE, mgr2.dev_types["senior-dev"]))
    assert run2.delivery_to == DELIVERY_REPOSITORY


def test_legacy_run_record_reads_as_repository():
    from devcake.domain.run import Run
    r = Run(run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1", mission_pmo_id="p1",
            mission_type="EXECUTE", dev_type="d", seq=1)
    assert r.delivery_to == ""       # "" ⇒ repository for every consumer
    _ = datetime.now(timezone.utc)   # keep the import honest for future rows
