"""ADR-0007: the Relations Steward is advisory — the app validates every
proposed edge (unknown/self/terminal/duplicate/cycle are dropped) — plus the
StewardService cadence/degradation and the comment-provenance sentinel
classification the steward's output relies on."""
import asyncio
import base64
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from devcake.config import AppConfig, DevType
from devcake.domain.orchestrator import (StewardBusy, StewardService, StewardUnconfigured,
                              MissionManager)
from devcake.domain.model import Activity, ActivityEntry, AttachmentRef, Mission
from devcake.adapters.files.run_store import RunStore
from devcake.domain.run import Run
from devcake.domain.orchestrator import dispatch, steward

NOW = datetime.now(timezone.utc)


def m(pmo_id, key, status="backlog", blocked_by=()):
    return Mission(pmo_id=pmo_id, pmo_kind="issue", key=key, title=key,
                   status=status, labels={"DEVCAKE"}, updated_at=NOW,
                   blocked_by=list(blocked_by))


class MapPMO:
    def __init__(self, missions, activity=None, *, relations_supported=True):
        self.missions = missions
        self.activity = activity
        self.relations = []
        self.comments = []
        self.activity_calls = []
        self._relations_supported = relations_supported

    def capabilities(self):
        from fakes import fake_pmo_capabilities
        return fake_pmo_capabilities(relations_supported=self._relations_supported)

    async def list_all(self, team_ref):
        return self.missions

    async def create_relation(self, blocker_id, blocked_id):
        self.relations.append((blocker_id, blocked_id))

    async def post_feed(self, ref, markdown):
        self.comments.append((ref.pmo_id, markdown))

    async def get_activity(self, ref, full=False):
        self.activity_calls.append(full)
        return self.activity


def make_mgr(tmp_path, pmo):
    from fakes import make_mission_manager
    return make_mission_manager(
        tmp_path, pmo=pmo,
        forge=SimpleNamespace(descriptor=SimpleNamespace(pr_noun='pull request')),
        noop_audit=True,
    )


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_finalize_steward_stamps_rate_card_estimate(tmp_path):
    """ADR-0021 parity: steward spend is fleet spend — finalize_steward stamps
    the same cost_usd_estimated + rate_card_id as mission finalize (failed
    outcome path: the stamp must land regardless of run success)."""
    from fakes import priced_cost_inputs
    mgr = make_mgr(tmp_path, MapPMO([]))
    mgr.config.cost_inputs = priced_cost_inputs()
    run = Run(run_id="SYS-STEWARD-1-ZZZZZZ", mission_key="STEWARD",
              mission_type="STEWARD", dev_type="steward", seq=1,
              state="finalizing")
    mgr.runs.store.save(run)
    grok = {"input_tokens": 1_000_000, "cache_read_tokens": 2_000_000,
            "cache_write_tokens": None, "output_tokens": 500_000,
            "total_tokens": 3_500_000, "cost_usd": None,
            "model": "grok-4.5-build", "extraction_method": "end_event"}
    run_coro(steward.finalize_steward(
        mgr, run, {"result": {"outcome": "nope"}, "token_report": grok}))
    saved = mgr.runs.store.get(run.run_id).token_report
    assert saved["cost_usd_estimated"] == 5.60
    assert saved["rate_card_id"] == mgr.config.cost_inputs.rate_card_id
    assert saved["rate_card_id"].startswith("operator:")
    assert saved["cost_usd"] is None


def test_opus_steward_reports_price_at_the_new_rate_row(tmp_path):
    """ADR-0033 D10: with an operator claude-opus rate row, Opus steward
    spend is priced ($5/$0.50/$6.25/$25 per M; claude harnesses DO report
    cache-write, unlike grok). CAKE-174 ships an empty card — rates are
    explicit in the test."""
    from fakes import priced_cost_inputs
    mgr = make_mgr(tmp_path, MapPMO([]))
    mgr.config.cost_inputs = priced_cost_inputs()
    run = Run(run_id="SYS-STEWARD-2-ZZZZZZ", mission_key="STEWARD",
              mission_type="STEWARD", dev_type="steward", seq=2,
              state="finalizing")
    mgr.runs.store.save(run)
    claude = {"input_tokens": 1_000_000, "cache_read_tokens": 2_000_000,
              "cache_write_tokens": 400_000, "output_tokens": 100_000,
              "total_tokens": 3_500_000, "cost_usd": None,
              "model": "claude-opus-5", "extraction_method": "session_json"}
    run_coro(steward.finalize_steward(
        mgr, run, {"result": {"outcome": "nope"}, "token_report": claude}))
    saved = mgr.runs.store.get(run.run_id).token_report
    assert saved["cost_usd_estimated"] == 11.00     # 5 + 1 + 2.5 + 2.5
    assert saved["rate_card_id"] == mgr.config.cost_inputs.rate_card_id


def test_apply_steward_edges_validates_everything(tmp_path):
    # B already blocked by A; C is terminal; D is free
    pmo = MapPMO([m("ia", "T-A"), m("ib", "T-B", blocked_by=["ia"]),
                  m("ic", "T-C", status="done"), m("id", "T-D")])
    mgr = make_mgr(tmp_path, pmo)
    created, rejected = run_coro(steward.apply_steward_edges(mgr, [
        {"blocker": "T-X", "blocked": "T-A"},      # unknown key
        {"blocker": "T-A", "blocked": "T-A"},      # self-edge
        {"blocker": "T-C", "blocked": "T-D"},      # terminal blocker
        {"blocker": "T-A", "blocked": "T-B"},      # duplicate
        {"blocker": "T-B", "blocked": "T-A"},      # cycle (A already blocks B)
        {"blocker": "T-B", "blocked": "T-D"},      # legitimate
    ]))
    assert (created, rejected) == (1, 5)
    assert pmo.relations == [("ib", "id")]
    pmo_id, comment = pmo.comments[-1]
    assert pmo_id == "id" and "T-B" in comment
    assert comment.endswith("`devcake:v1`")        # notification is sentinel-signed


def test_apply_steward_edges_skips_when_relations_unsupported(tmp_path):
    pmo = MapPMO([m("ia", "T-A"), m("id", "T-D")], relations_supported=False)
    mgr = make_mgr(tmp_path, pmo)
    created, rejected = run_coro(steward.apply_steward_edges(mgr, [
        {"blocker": "T-A", "blocked": "T-D"},
    ]))
    assert (created, rejected) == (0, 1)
    assert pmo.relations == []


def test_creates_cycle_transitive():
    graph = {"a": {"b"}, "b": {"c"}}               # a depends on b depends on c
    assert steward._creates_cycle(graph, "a", "c")       # c←a would loop
    assert not steward._creates_cycle(graph, "c", "d")   # fresh edge is fine


def test_config_defaults():
    from devcake.config import RepoInstance
    cfg = AppConfig()
    assert cfg.intake_paused is False
    assert cfg.steward.enabled is False           # manual-only by default
    assert cfg.steward.interval_minutes == 60
    assert cfg.steward.dev_type == "steward"   # name hint until first-setup
    # merge doctrine lives on RepoInstance (ADR-0020 / docs/03 §4.1)
    repo = RepoInstance(name="main", url="https://github.com/o/r")
    assert repo.auto_merge is False
    assert repo.auto_resolve_merge_conflicts is True
    assert repo.merge_retry_window_minutes == 30
    # roundtrips through dump/validate (the /api/v1/config PUT path)
    assert AppConfig.model_validate(cfg.model_dump()) == cfg
    with pytest.raises(Exception):                         # ge=0 enforced
        RepoInstance.model_validate(
            {"name": "main", "url": "https://github.com/o/r",
             "merge_retry_window_minutes": -5})


def test_deep_merge_preserves_nested_siblings():
    from devcake.config import deep_merge
    base = AppConfig().model_dump()
    merged = deep_merge(base, {"steward": {"enabled": True}})
    assert merged["steward"]["enabled"] is True
    assert merged["steward"]["dev_type"] == "steward"  # sibling survived


# ── StewardService cadence + degradation ──────────────────────────────────────

def make_service(tmp_path, enabled=True, dev_type="steward"):
    cfg = AppConfig()
    cfg.steward.enabled = enabled
    cfg.steward.dev_type = dev_type
    mgr = make_mgr(tmp_path, MapPMO([]))
    dispatched = []

    async def fake_dispatch(dt, missions, **kw):
        dispatched.append(dt.name)
        return Run(run_id=f"TEAM-{len(dispatched)}-STEWARD-AAAAAA",
                   mission_key="TEAM", mission_type="STEWARD",
                   dev_type=dt.name, seq=len(dispatched))

    mgr.dispatch_steward = fake_dispatch
    svc = StewardService(cfg, {"steward": DevType(name="steward",
                                                    harness_template="claude-code")},
                        mgr)
    return svc, mgr, dispatched


def steward_run(n, state):
    return Run(run_id=f"TEAM-{n}-STEWARD-{'X' * 6}", mission_key="TEAM",
               mission_type="STEWARD", dev_type="steward", seq=n, state=state)


def test_maybe_dispatch_respects_interval_and_toggle(tmp_path):
    svc, mgr, dispatched = make_service(tmp_path, enabled=False)
    svc._last_at = time.monotonic() - 10**6
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == []                        # toggle off → never

    svc, mgr, dispatched = make_service(tmp_path)
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == []                        # interval not elapsed
    svc._last_at = time.monotonic() - 10**6
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == ["steward"]            # elapsed → dispatched
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == ["steward"]            # watermark advanced


def test_watermark_not_advanced_when_dispatch_returns_none(tmp_path):
    svc, mgr, dispatched = make_service(tmp_path)

    async def skip(dt, missions, **kw):
        dispatched.append("skip")
        return None

    mgr.dispatch_steward = skip
    svc._last_at = time.monotonic() - 10**6
    before = svc._last_at
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == ["skip"]
    assert svc._last_at == before


def test_watermark_not_advanced_on_dispatch_failure(tmp_path):
    svc, mgr, dispatched = make_service(tmp_path)

    async def boom(dt, missions, **kw):
        raise RuntimeError("dagu down")

    mgr.dispatch_steward = boom
    svc._last_at = time.monotonic() - 10**6
    before = svc._last_at
    with pytest.raises(RuntimeError):
        run_coro(svc.maybe_dispatch([]))
    assert svc._last_at == before                  # next cycle retries


def test_degraded_after_three_dead_runs_skips_periodic_only(tmp_path):
    svc, mgr, dispatched = make_service(tmp_path)
    for i, st in enumerate(("failed", "timed_out", "failed"), start=1):
        mgr.runs.store.save(steward_run(i, st))
    assert svc.degraded()
    svc._last_at = time.monotonic() - 10**6
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == []                        # periodic backs off
    run = run_coro(svc.run_now())                  # manual is the reset signal
    assert run.mission_type == "STEWARD"
    # a finished run clears the condition (store-derived, restart-safe)
    mgr.runs.store.save(steward_run(9, "finished"))
    assert svc.degraded() is None


def test_run_now_errors(tmp_path):
    svc, mgr, dispatched = make_service(tmp_path, dev_type=None)
    with pytest.raises(StewardUnconfigured):
        run_coro(svc.run_now())
    svc, mgr, dispatched = make_service(tmp_path)
    active = steward_run(1, "running")
    mgr.runs.store.save(active)
    with pytest.raises(StewardBusy):
        run_coro(svc.run_now())


def test_run_now_honors_intake_pause(tmp_path):
    """docs/03 §4b + docs/11: intake pause freezes NEW steward runs too —
    including the manual "Run now" path. Periodic is already withheld by
    the poll segment; run_now must not be a back door."""
    from devcake.config import PMOInstance
    svc, mgr, dispatched = make_service(tmp_path)
    svc.config.intake_paused = True
    with pytest.raises(StewardUnconfigured, match="intake"):
        run_coro(svc.run_now())
    assert dispatched == []

    svc, mgr, dispatched = make_service(tmp_path)
    svc.config.intake_paused = False
    # instance switch alone freezes this board's steward
    mgr.instance = PMOInstance(name="eng", team_key="ENG", intake_paused=True)
    with pytest.raises(StewardUnconfigured, match="intake"):
        run_coro(svc.run_now())
    assert dispatched == []


def test_maybe_dispatch_self_guards_intake_pause(tmp_path):
    """ADR-0034: pause guard travels with the steward path, not only the
    poll caller — so a future caller cannot reintroduce a back door."""
    svc, mgr, dispatched = make_service(tmp_path)
    svc.config.intake_paused = True
    svc._last_at = time.monotonic() - 10**6
    run_coro(svc.maybe_dispatch([]))
    assert dispatched == []


def test_run_now_gates_on_missing_referenced_secret_env(tmp_path, monkeypatch):
    """Steward runs use the same gate as mission dispatch: a referenced-but-
    unstored secret env var refuses run_now with a 422-bound
    StewardUnconfigured naming the var; storing the value lifts it."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    svc, mgr, dispatched = make_service(tmp_path)
    svc.dev_types["steward"] = DevType(
        name="steward", harness_template="claude-code",
        secret_env=["DD_API_KEY"],
        mcp_setup_commands=["claude mcp add logs -e K=$DD_API_KEY -- x"])
    with pytest.raises(StewardUnconfigured, match="DD_API_KEY"):
        run_coro(svc.run_now())
    assert dispatched == []
    from devcake import secrets as s
    s.write_harness_secret("DD_API_KEY", "k")
    run_coro(svc.run_now())
    assert dispatched == ["steward"]


def test_activity_payload_marks_provenance(tmp_path):
    mission = m("i1", "T-1")
    entries = [
        ActivityEntry(ts=NOW, author="felipe", kind="comment",
                      body="🧾 DevCake transcript `2_EXECUTE.md` (run `x`)\n\n"
                           "stuff\n\n`devcake:v1`"),
        ActivityEntry(ts=NOW, author="felipe", kind="comment",
                      body="please use tabs, not spaces"),
    ]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = make_mgr(tmp_path, pmo)
    md = run_coro(mgr.activity_payload("i1"))["activity_md"]
    assert md.count("— 🤖 DevCake") == 1           # entry headers, not the legend
    assert md.count("— 🧑 HUMAN") == 1
    # same author on both entries — provenance came from the sentinel, not the name
    assert dispatch._derive_seq(
        Activity(mission=mission, entries=entries)) == 3   # STEP_MARKER intact


def test_derive_seq_ignores_quoted_markers():
    # ADR-0014 D2: `>`-quoted lines are quarantined — a human citing a
    # transcript name (or a blockquoted last message) must never bump seq
    mission = m("i1", "T-1")
    mixed = [ActivityEntry(ts=NOW, author="felipe", kind="comment",
                           body="🧾 DevCake transcript `2_EXECUTE.md` (run `x`)\n"
                                "> as the Dev said in `7_EXECUTE.md` and `9_PLAN.md`\n"
                                "`devcake:v1`")]
    assert dispatch._derive_seq(
        Activity(mission=mission, entries=mixed)) == 3     # 2 counts; 7/9 don't
    quoted_only = [ActivityEntry(ts=NOW, author="h", kind="comment",
                                 body="> see `7_EXECUTE.md` for details")]
    assert dispatch._derive_seq(
        Activity(mission=mission, entries=quoted_only)) == 1


def _paginated_entries(markdown: str, *, limit: int = 400) -> list[ActivityEntry]:
    from devcake.domain.orchestrator.feed import split_vendor_comments
    from devcake.domain.orchestrator.markers import COMMENT_SENTINEL
    parts = split_vendor_comments(markdown, limit)
    assert len(parts) >= 2, "fixture must actually paginate"
    return [
        ActivityEntry(ts=NOW, author="cake", kind="comment",
                      body=p + "\n\n" + COMMENT_SENTINEL)
        for p in parts
    ]


def _att_text(payload, name) -> str:
    row = next(a for a in payload["attachments"] if a["filename"] == name)
    return base64.b64decode(row["content_b64"]).decode()


def test_activity_payload_coalesces_paginated_transcript_into_step_file(tmp_path):
    """GitHub pages a transcript; the Dev folder must still have 2_EXECUTE.md
    the way Linear/Gitea do via the attachment."""
    from devcake.domain.orchestrator.feed import blockquote
    dump = "hello from the run\n" + ("step-body-line\n" * 80)
    comment = (
        "🧾 DevCake transcript `2_EXECUTE.md` (run `R`)\n\n"
        + blockquote("---\n\n" + dump)
    )
    entries = _paginated_entries(comment, limit=350)
    mission = m("i1", "T-1")
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    names = [a["filename"] for a in payload["attachments"]]
    assert names == ["2_EXECUTE.md"]
    assert dump.rstrip("\n") in _att_text(payload, "2_EXECUTE.md")
    assert "Part 1 of" in payload["activity_md"]   # feed mirror stays faithful


def test_activity_payload_coalesces_paginated_plan_into_plan_file(tmp_path):
    plan = "# the plan\n\n" + ("do the thing\n" * 60)
    comment = "📋 DevCake plan for this mission (`PLAN_1.md`):\n\n" + plan
    entries = _paginated_entries(comment, limit=350)
    mission = m("i1", "T-1")
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert "PLAN_1.md" in [a["filename"] for a in payload["attachments"]]
    got = _att_text(payload, "PLAN_1.md")
    assert "# the plan" in got
    assert got.count("do the thing") == 60


def test_activity_payload_does_not_overwrite_existing_step_attachment(tmp_path):
    """Linear/Gitea already shipped 2_EXECUTE.md as a real attachment."""
    from devcake.domain.orchestrator.feed import blockquote
    from devcake.domain.orchestrator.markers import COMMENT_SENTINEL
    dump = "inline dump that must lose to the attachment"
    comment = (
        "🧾 DevCake transcript `2_EXECUTE.md` (run `R`)\n\n"
        + blockquote("---\n\n" + dump) + "\n\n" + COMMENT_SENTINEL
    )
    url = "https://uploads.linear.app/2_EXECUTE.md"
    entries = [
        ActivityEntry(ts=NOW, author="cake", kind="comment", body=comment,
                      attachments=[AttachmentRef(
                          url=url, name="2_EXECUTE.md")]),
    ]
    mission = m("i1", "T-1")
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    async def _dl(u):
        return b"attachment-bytes"
    pmo.download_asset = _dl
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    names = [a["filename"] for a in payload["attachments"]]
    assert names == ["2_EXECUTE.md"]
    assert _att_text(payload, "2_EXECUTE.md") == "attachment-bytes"


def test_activity_payload_inlines_long_bodies_verbatim(tmp_path):
    # ADR-0014 D3: ACTIVITY.md is a faithful MIRROR — long feed bodies appear
    # whole and inline, never externalized to entry-*.md previews
    mission = m("i1", "T-1")
    long_body = "first " + "x" * 3000
    entries = [ActivityEntry(ts=NOW, author="a", kind="comment", body=long_body)]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert long_body in payload["activity_md"]
    assert payload["attachments"] == []            # no entry-*.md files


def test_activity_payload_dedupes_downloaded_attachment_names(tmp_path):
    # docs/07 §2 collision rule: two feed attachments sharing a filename get
    # -2/-3 suffixes, each index line naming its OWN file
    mission = m("i1", "T-1")
    url1, url2 = "https://uploads.linear.app/a", "https://uploads.linear.app/b"
    entries = [
        ActivityEntry(ts=NOW, author="a", kind="comment",
                      body=f"[report.md]({url1})",
                      attachments=[AttachmentRef(url=url1, name="report.md")]),
        ActivityEntry(ts=NOW, author="b", kind="comment",
                      body=f"[report.md]({url2})",
                      attachments=[AttachmentRef(url=url2, name="report.md")]),
    ]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    pmo.download_asset = _returns(b"data")
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    names = [a["filename"] for a in payload["attachments"]]
    assert names == ["report.md", "report-2.md"]
    assert "[attachment: report.md]" in payload["activity_md"]
    assert "[attachment: report-2.md]" in payload["activity_md"]


def test_activity_payload_returns_mission_md_brief(tmp_path):
    # ADR-0014 D3: MISSION.md carries the brief with the FULL description;
    # ACTIVITY.md keeps a minimal header pointing at it
    mission = m("i1", "T-1")
    mission.description = "long brief " * 500
    pmo = MapPMO([], activity=Activity(mission=mission, entries=[]))
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert ("long brief " * 500).strip() in payload["mission_md"]
    assert "## Description" in payload["mission_md"]
    assert "## Description" not in payload["activity_md"]
    assert "MISSION.md" in payload["activity_md"]  # the pointer


def test_activity_payload_requests_full_history(tmp_path):
    pmo = MapPMO([], activity=Activity(mission=m("i1", "T-1"), entries=[]))
    mgr = make_mgr(tmp_path, pmo)
    run_coro(mgr.activity_payload("i1"))
    assert pmo.activity_calls == [True]            # builder = full mode, always


def test_activity_payload_renders_reply_nesting(tmp_path):
    mission = m("i1", "T-1")
    entries = [
        ActivityEntry(ts=NOW, author="felipe", kind="comment",
                      body="root post", entry_id="c1"),
        ActivityEntry(ts=NOW, author="cake", kind="comment",
                      body="the reply", entry_id="c2", parent_id="c1"),
    ]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = make_mgr(tmp_path, pmo)
    md = run_coro(mgr.activity_payload("i1"))["activity_md"]
    marker_at = md.find("↳ reply to felipe")
    assert marker_at != -1 and marker_at < md.find("the reply")


def test_activity_payload_materializes_mission_attachments(tmp_path):
    # description/native assets: files downloaded as siblings + listed in
    # MISSION.md; links rendered as markdown links, never downloaded
    mission = m("i1", "T-1")
    act = Activity(mission=mission, entries=[], mission_attachments=[
        AttachmentRef(url="https://uploads.linear.app/spec",
                      name="spec.pdf", kind="file"),
        AttachmentRef(url="https://github.com/x/pull/1",
                      name="PR #1", kind="link")])
    pmo = MapPMO([], activity=act)
    pmo.download_asset = _returns(b"bytes")
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert [a["filename"] for a in payload["attachments"]] == ["spec.pdf"]
    assert "[attachment: spec.pdf]" in payload["mission_md"]
    assert "[link: PR #1](https://github.com/x/pull/1)" in payload["mission_md"]


def test_activity_payload_project_brief_is_mission_md(tmp_path):
    # project-fidelity fix: the project branch rides get_activity(full=True)
    # (updates/documents mirror); the brief still lands in MISSION.md and an
    # empty feed renders the honest placeholder, not the pre-fix stub
    proj = Mission(pmo_id="p9", pmo_kind="project", key="P-1", title="proj",
                   status="backlog", description="the project brief",
                   updated_at=NOW)
    pmo = MapPMO([], activity=Activity(mission=proj, entries=[]))
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("p9", "project"))
    assert pmo.activity_calls == [True]
    assert "the project brief" in payload["mission_md"]
    assert "(no project updates yet" in payload["activity_md"]
    assert "no comment feed" not in payload["activity_md"]


def test_activity_payload_renders_truncation_banner(tmp_path):
    act = Activity(mission=m("i1", "T-1"), entries=[], truncated=True)
    pmo = MapPMO([], activity=act)
    mgr = make_mgr(tmp_path, pmo)
    md = run_coro(mgr.activity_payload("i1"))["activity_md"]
    assert "FEED TRUNCATED" in md.splitlines()[0]   # loud, first line
    # which end the hard stop drops is adapter-specific — never claim one
    assert "OLDEST" not in md
    assert "NEWEST" not in md


def _returns(value):
    async def _f(*a, **k):
        return value
    return _f


def test_steward_seq_scoped_to_own_instance():
    """Audit A29 (cosmetic): run ids are collision-free regardless, but the
    human-visible seq counted OTHER instances' STEWARD runs too."""
    import inspect
    from devcake.domain.orchestrator import steward as steward_mod
    # the seq computation lives in the shared launch body (ADR-0033) —
    # both flavors inherit the own-instance scoping
    assert "_run_is_ours" in inspect.getsource(steward_mod._launch_steward_inner)


def test_activity_payload_marks_unavailable_attachment(tmp_path):
    # review 2.2 pin: a failed download leaves the honest placeholder line
    mission = m("i1", "T-1")
    url = "https://uploads.linear.app/gone"
    entries = [ActivityEntry(ts=NOW, author="a", kind="comment", body="x",
                             attachments=[AttachmentRef(url=url, name="g.md")])]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))

    async def _boom(u):
        raise RuntimeError("410 gone")
    pmo.download_asset = _boom
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert f"[attachment unavailable: {url}]" in payload["activity_md"]
    assert payload["attachments"] == []


def test_activity_payload_reserved_name_attachment_suffixed(tmp_path):
    # review 2.2 pin: an attachment literally named MISSION.md never clobbers
    # the brief (docs/07 §2 dedupe seed)
    mission = m("i1", "T-1")
    entries = [ActivityEntry(ts=NOW, author="a", kind="comment", body="x",
                             attachments=[AttachmentRef(
                                 url="https://uploads.linear.app/m",
                                 name="MISSION.md")])]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    pmo.download_asset = _returns(b"d")
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert [a["filename"] for a in payload["attachments"]] == ["MISSION-2.md"]


def test_activity_payload_orphan_reply_marked(tmp_path):
    # review 2.2: a reply whose parent was deleted keeps an honest marker
    mission = m("i1", "T-1")
    entries = [ActivityEntry(ts=NOW, author="a", kind="comment",
                             body="reply body", entry_id="c9",
                             parent_id="gone")]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = make_mgr(tmp_path, pmo)
    md = run_coro(mgr.activity_payload("i1"))["activity_md"]
    assert "↳ reply to (deleted comment)" in md


def test_activity_payload_basenames_slashed_attachment_names(tmp_path):
    # full-diff review: a `[v1/report.md](url)` link must agree end-to-end —
    # index line, snapshot path, and folder file all use the basename, and
    # two slashed names colliding on it dedupe instead of desyncing
    mission = m("i1", "T-1")
    entries = [
        ActivityEntry(ts=NOW, author="a", kind="comment", body="x",
                      attachments=[AttachmentRef(
                          url="https://uploads.linear.app/a",
                          name="v1/report.md")]),
        ActivityEntry(ts=NOW, author="b", kind="comment", body="y",
                      attachments=[AttachmentRef(
                          url="https://uploads.linear.app/b",
                          name="v2/report.md")]),
    ]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    pmo.download_asset = _returns(b"d")
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert [a["filename"] for a in payload["attachments"]] == \
        ["report.md", "report-2.md"]
    assert "[attachment: report.md]" in payload["activity_md"]
    assert "v1/report.md" not in payload["activity_md"]


# ── ADR-0033: the discovery flavor (dispatch + package) ──────────────────────

def _src_run(store, mgr, pmo_id="src", seq=2, n_entries=3):
    entries = [{"finding": f"finding {i} about the config",
                "evidence": f"src/x.py:{i}; repro: pytest -k f{i}",
                "scope": f"scope {i}"} for i in range(n_entries)]
    r = Run(run_id=f"L-T-S-{seq}-EXECUTE-AAAAAA", mission_key="T-S",
            mission_pmo_id=pmo_id, mission_type="EXECUTE",
            dev_type="senior-dev", seq=seq, state="finished",
            pmo_ref=mgr.instance_name)
    r.result = {"outcome": "executed", "summary": "s", "discoveries": entries}
    store.save(r)
    return r


def _fam(*members):
    from devcake.domain.orchestrator.family_graph import Family
    return Family(members=list(members))


def test_build_discovery_package_curates_not_accumulates(tmp_path):
    from devcake.domain.orchestrator.markers import HANDOFF_MARKER
    done = m("d1", "T-D", status="done")
    done.description = (f"built the schema\n\n---\n{HANDOFF_MARKER}\n"
                        f"renamed Store to Vault\n")
    src = m("src", "T-S", status="in_progress")
    src.description = "implement the API"
    open_m = m("o1", "T-O")
    mgr = make_mgr(tmp_path, MapPMO([done, src, open_m]))
    _src_run(mgr.runs.store, mgr, n_entries=3)
    package, included = steward.build_discovery_package(
        mgr, _fam(done, src, open_m), {"src": [(2, 2)]},
        mgr.runs.store.all())
    assert included == [("src", 2)]
    assert "**T-D** · done" in package and "Handoff: renamed Store" in package
    assert "**T-O**" in package
    assert "From **T-S** step 2" in package
    assert "finding 0 about the config" in package
    assert "finding 1 about the config" in package
    assert "finding 2" not in package          # marker n=2 bounds the batch
    assert "Evidence: src/x.py:0" in package   # full fidelity, not excerpts


def test_build_discovery_package_skips_gone_run_records(tmp_path):
    src = m("src", "T-S")
    mgr = make_mgr(tmp_path, MapPMO([src]))
    package, included = steward.build_discovery_package(
        mgr, _fam(src), {"src": [(7, 1)]}, mgr.runs.store.all())
    assert included == []                      # sweep terminates it, not us
    assert "(none)" in package


def test_dispatch_steward_discovery_stamps_duty_and_excludes_primary(
        tmp_path, monkeypatch):
    from devcake.domain.orchestrator import family_graph
    src = m("src", "T-S", status="in_progress")
    src.repo = "web"
    mgr = make_mgr(tmp_path, MapPMO([src]))
    _src_run(mgr.runs.store, mgr, n_entries=1)
    captured = {}

    async def fake_launch(mgr_, dt_, *, duty, prompt_text, blocker_work=None, batches=None, context_stale=frozenset(), context_omit=frozenset()):
        captured.update(duty=duty, prompt=prompt_text, bw=blocker_work)
        return "RUN-SENTINEL"
    monkeypatch.setattr(steward, "_launch_steward", fake_launch)
    monkeypatch.setattr(steward.dispatch, "steward_repo", lambda m_: "home")
    monkeypatch.setattr(family_graph, "blocker_read_credential",
                        lambda mgr_, name: ("configured", None, None, "tok"))
    dt = DevType(name="steward", harness_template="claude-code")
    out = asyncio.new_event_loop().run_until_complete(
        steward.dispatch_steward_discovery(mgr, dt, _fam(src),
                                           {"src": [(2, 1)]}))
    assert out == "RUN-SENTINEL"
    assert captured["duty"] == "discovery"
    assert "laminarity" in captured["prompt"].lower()
    assert '"outcome": "stewarded"' in captured["prompt"]
    assert "finding 0 about the config" in captured["prompt"]
    assert captured["bw"] == [{"repo_ref": "web", "mission_key": "T-S"}]


def test_dispatch_steward_discovery_no_batches_no_run(tmp_path, monkeypatch):
    src = m("src", "T-S")
    mgr = make_mgr(tmp_path, MapPMO([src]))
    monkeypatch.setattr(steward.dispatch, "steward_repo", lambda m_: "home")
    out = asyncio.new_event_loop().run_until_complete(
        steward.dispatch_steward_discovery(
            mgr, DevType(name="steward", harness_template="claude-code"),
            _fam(src), {"src": [(9, 1)]}))     # record gone ⇒ nothing to route
    assert out is None


def test_relations_dispatch_still_builds_the_relations_prompt(
        tmp_path, monkeypatch):
    a = m("a", "T-1")
    mgr = make_mgr(tmp_path, MapPMO([a]))
    captured = {}

    async def fake_launch(mgr_, dt_, *, duty, prompt_text, blocker_work=None, batches=None, context_stale=frozenset(), context_omit=frozenset()):
        captured.update(duty=duty, prompt=prompt_text, bw=blocker_work)
        return "R"
    monkeypatch.setattr(steward, "_launch_steward", fake_launch)
    asyncio.new_event_loop().run_until_complete(steward.dispatch_steward(
        mgr, DevType(name="steward", harness_template="claude-code"), [a]))
    assert captured["duty"] == ""              # relations = the legacy duty
    assert "RELATIONS STEWARD" in captured["prompt"]
    assert captured["bw"] is None


# ── ADR-0033: apply_discovery_routes (validate / deliver / receipt) ─────────

class RoutePMO(MapPMO):
    """Per-mission feeds that ABSORB posts — the board stays self-
    consistent, so the receipt→re-scan→label-drop arithmetic is exercised
    for real, not against a frozen fixture."""

    def __init__(self, missions, feeds=None):
        super().__init__(missions)
        self.feeds = feeds or {}          # pmo_id → Activity
        self.swaps = []

    def _mission(self, pmo_id):
        return next((mm for mm in self.missions if mm.pmo_id == pmo_id),
                    self.missions[0])

    async def get_activity(self, ref, full=False):
        self.activity_calls.append((ref.pmo_id, full))
        return self.feeds.setdefault(
            ref.pmo_id, Activity(mission=self._mission(ref.pmo_id),
                                 entries=[], truncated=False))

    async def post_feed(self, ref, markdown):
        self.comments.append((ref.pmo_id, markdown))
        self.feeds.setdefault(
            ref.pmo_id, Activity(mission=self._mission(ref.pmo_id),
                                 entries=[], truncated=False)
        ).entries.append(ActivityEntry(
            ts=NOW, author="devcake", kind="comment", body=markdown,
            entry_id=f"e{len(self.comments)}"))

    async def swap_labels(self, ref, remove, add):
        self.swaps.append((ref.pmo_id, set(remove), set(add)))


def _ae(body):
    return ActivityEntry(ts=NOW, author="devcake", kind="comment",
                         body=body, entry_id=body[:24])


def _route_setup(tmp_path, *, n=2, entries=3, recipient_bodies=(),
                 recipient_truncated=False, extra_missions=()):
    from devcake.domain.orchestrator.markers import discovery_marker
    src = m("src", "T-S", status="in_progress")
    tgt = m("tgt", "T-T", status="in_progress", blocked_by=["src"])
    pmo = RoutePMO([src, tgt, *extra_missions])
    pmo.feeds["src"] = Activity(
        mission=src, entries=[_ae(discovery_marker(2, n))], truncated=False)
    pmo.feeds["tgt"] = Activity(
        mission=tgt, entries=[_ae(b) for b in recipient_bodies],
        truncated=recipient_truncated)
    mgr = make_mgr(tmp_path, pmo)
    mgr.instance.discovery_routing = True
    _src_run(mgr.runs.store, mgr, seq=2, n_entries=entries)
    run = Run(run_id="L-TEAM-1-STEWARD-AAAAAA", mission_key="TEAM",
              mission_type="STEWARD", dev_type="steward", seq=1,
              pmo_ref=mgr.instance_name, steward_duty="discovery",
              steward_batches=[{"pmo_id": "src", "key": "T-S", "step": 2}])
    return pmo, mgr, run


def _route(target="T-T", source="T-S", step=2, finding=1,
           because="target touches the same config"):
    return {"target": target, "source": source, "step": step,
            "finding": finding, "because": because}


def _apply(mgr, run, routes):
    return asyncio.new_event_loop().run_until_complete(
        steward.apply_discovery_routes(mgr, run, routes))


def test_apply_routes_delivers_receipts_and_drops_label(tmp_path):
    pmo, mgr, run = _route_setup(tmp_path)
    mgr._discoveries_pending.add("src")
    delivered, rejected = _apply(mgr, run, [_route()])
    assert (delivered, rejected) == (1, 0)
    tgt_posts = [md for pid, md in pmo.comments if pid == "tgt"]
    assert len(tgt_posts) == 1
    body = tgt_posts[0]
    assert "`devcake:discovery-in:v1 src=T-S step=2`" in body
    assert "leads, not truths" in body
    assert "finding 0 about the config" in body        # verbatim from record
    assert "— steward:" in body
    src_posts = [md for pid, md in pmo.comments if pid == "src"]
    assert any("`devcake:discovery-routed:v1 step=2 to=T-T`" in md
               for md in src_posts)
    assert ("src", {"DEVCAKE-DISCOVERY"}, set()) in pmo.swaps
    assert "src" not in mgr._discoveries_pending


def _src_comments(pmo):
    return [md for pid, md in pmo.comments if pid == "src"]


def _receipted_nowhere(pmo):
    return any("to=-" in md for md in _src_comments(pmo))


def test_apply_routes_reject_ladder(tmp_path):
    done = m("d", "T-D", status="done", blocked_by=["src"])
    outside = m("o", "T-O")                    # no edges — its own family
    pmo, mgr, run = _route_setup(tmp_path, extra_missions=(done, outside))
    delivered, rejected = _apply(mgr, run, [
        _route(target="T-X"),                  # unknown target
        _route(target="T-S"),                  # self-route
        _route(target="T-D"),                  # terminal recipient
        _route(target="T-O"),                  # outside the family
        _route(step=9),                        # not in this run's package
        _route(finding=3),                     # marker n=2 bounds the batch
        "not a dict",                          # malformed
    ])
    assert delivered == 0 and rejected == 7
    assert not any(pid == "tgt" for pid, _ in pmo.comments)
    # a confused steward must not loop: terminal rejects disposition the batch
    assert _receipted_nowhere(pmo)
    assert ("src", {"DEVCAKE-DISCOVERY"}, set()) in pmo.swaps


def test_apply_routes_because_is_defanged(tmp_path):
    pmo, mgr, run = _route_setup(tmp_path)
    _apply(mgr, run, [_route(because="see `devcake:handoff:v1` upstream")])
    body = next(md for pid, md in pmo.comments if pid == "tgt")
    assert "devcake:handoff:v1" in body
    assert "`devcake:handoff:v1`" not in body


def test_apply_routes_dedup_and_idempotent_rerun(tmp_path):
    pmo, mgr, run = _route_setup(tmp_path)
    d1, _ = _apply(mgr, run, [_route()])
    assert d1 == 1
    before = len(pmo.comments)
    d2, _ = _apply(mgr, run, [_route()])       # redelivered finalize
    assert d2 == 0                             # dup skipped via live feed
    tgt_posts = [md for pid, md in pmo.comments if pid == "tgt"]
    assert len(tgt_posts) == 1                 # ONE delivery, ever
    # no duplicate receipt comment either (receipts pre-filtered)
    assert len(pmo.comments) == before


def test_apply_routes_no_numeric_budgets(tmp_path):
    """Addendum 14: routing has NO numeric budget — the (src, step) dedup
    and family size are the structural bounds. Prior receipts and prior
    deliveries of OTHER pairs never block a fresh route."""
    pmo, mgr, run = _route_setup(tmp_path, recipient_bodies=(
        "`devcake:discovery-in:v1 src=T-Z step=1`",))
    pmo.feeds["src"].entries.append(
        _ae("`devcake:discovery-routed:v1 step=1 to=T-Q`"))
    delivered, rejected = _apply(mgr, run, [_route()])
    assert (delivered, rejected) == (1, 0)
    assert any("discovery-in:v1 src=T-S" in md
               for pid, md in pmo.comments if pid == "tgt")


def test_apply_routes_truncated_recipient_is_terminal_raised(tmp_path):
    """A recipient past the full-read ceiling never heals (feeds only
    grow): the batch dispositions to=- and the receipt comment carries a
    human-directed reason (addendum 14) — no hold, no re-dispatch loop."""
    pmo, mgr, run = _route_setup(tmp_path, recipient_truncated=True)
    delivered, rejected = _apply(mgr, run, [_route()])
    assert (delivered, rejected) == (0, 1)
    assert not any(pid == "tgt" for pid, _ in pmo.comments)
    assert _receipted_nowhere(pmo)
    raised = [md for md in _src_comments(pmo) if "to=-" in md]
    assert any("readable page ceiling" in md and "T-T" in md
               and "DISCOVERY_2.md" in md for md in raised)
    assert ("src", {"DEVCAKE-DISCOVERY"}, set()) in pmo.swaps


def test_apply_routes_unreadable_recipient_does_not_receipt(tmp_path):
    pmo, mgr, run = _route_setup(tmp_path)

    async def _boom(ref, full=False):
        if ref.pmo_id == "tgt":
            raise RuntimeError("PMO 502")
        return await RoutePMO.get_activity(pmo, ref, full=full)
    pmo.get_activity = _boom
    delivered, rejected = _apply(mgr, run, [_route()])
    assert (delivered, rejected) == (0, 1)
    assert not _receipted_nowhere(pmo)


def test_apply_routes_delivery_post_fail_does_not_receipt(tmp_path):
    pmo, mgr, run = _route_setup(tmp_path)
    orig = pmo.post_feed

    async def _refuse(ref, markdown):
        if ref.pmo_id == "tgt":
            raise RuntimeError("comment API down")
        return await orig(ref, markdown)
    pmo.post_feed = _refuse
    delivered, rejected = _apply(mgr, run, [_route()])
    assert (delivered, rejected) == (0, 1)
    assert not _receipted_nowhere(pmo)


def test_apply_routes_partial_multi_recipient_hold_withholds_all_receipts(
        tmp_path):
    """ADR-0033 addendum 10: a transient hold on ANY recipient in a
    multi-target batch must leave the whole (source, step) unreceipted so
    pending stays alive and the sweep re-drives held siblings. Success
    deliveries that already landed stay idempotent via discovery-in."""
    from devcake.domain.orchestrator import discovery

    tgt2 = m("tgt2", "T-U", status="in_progress", blocked_by=["src"])
    pmo, mgr, run = _route_setup(tmp_path, extra_missions=(tgt2,))
    pmo.feeds["tgt2"] = Activity(
        mission=tgt2, entries=[], truncated=False)
    src = next(mm for mm in pmo.missions if mm.pmo_id == "src")
    mgr._discoveries_pending.add("src")

    async def _hold_second(ref, full=False):
        if ref.pmo_id == "tgt2":
            raise RuntimeError("PMO 502")
        return await RoutePMO.get_activity(pmo, ref, full=full)
    pmo.get_activity = _hold_second

    delivered, rejected = _apply(mgr, run, [
        _route(target="T-T"),
        _route(target="T-U", finding=2),
    ])
    assert delivered == 1 and rejected == 1
    tgt_posts = [md for pid, md in pmo.comments if pid == "tgt"]
    assert len(tgt_posts) == 1
    assert "`devcake:discovery-in:v1 src=T-S step=2`" in tgt_posts[0]
    assert not any(pid == "tgt2" for pid, _ in pmo.comments)
    assert not any("discovery-routed:v1" in md for md in _src_comments(pmo))
    assert ("src", {"DEVCAKE-DISCOVERY"}, set()) not in pmo.swaps
    state = asyncio.new_event_loop().run_until_complete(
        discovery.scan_source(mgr, src))
    assert any(step == 2 for step, _n in state.pending)

    # Hold clears — re-drive must finish the sibling and only then receipt.
    pmo.get_activity = lambda ref, full=False: RoutePMO.get_activity(
        pmo, ref, full=full)
    delivered2, rejected2 = _apply(mgr, run, [
        _route(target="T-T"),
        _route(target="T-U", finding=2),
    ])
    assert (delivered2, rejected2) == (1, 0)
    assert len([md for pid, md in pmo.comments if pid == "tgt"]) == 1
    assert any("`devcake:discovery-in:v1 src=T-S step=2`" in md
               for pid, md in pmo.comments if pid == "tgt2")
    src_posts = _src_comments(pmo)
    assert any("`devcake:discovery-routed:v1 step=2 to=T-T`" in md
               for md in src_posts)
    assert any("`devcake:discovery-routed:v1 step=2 to=T-U`" in md
               for md in src_posts)
    state2 = asyncio.new_event_loop().run_until_complete(
        discovery.scan_source(mgr, src))
    assert not any(step == 2 for step, _n in state2.pending)


def test_apply_routes_toggle_off_writes_nothing(tmp_path):
    pmo, mgr, run = _route_setup(tmp_path)
    mgr.instance.discovery_routing = False
    try:
        delivered, rejected = _apply(mgr, run, [_route()])
        assert (delivered, rejected) == (0, 0)
        assert pmo.comments == []
        assert pmo.swaps == []
    finally:
        mgr.instance.discovery_routing = True


def test_apply_routes_routed_nowhere_still_receipts(tmp_path):
    # empty routes is a valid steward result — the batch must still be
    # dispositioned (`to=-`) or the sweep would re-dispatch forever
    pmo, mgr, run = _route_setup(tmp_path)
    delivered, rejected = _apply(mgr, run, [])
    assert (delivered, rejected) == (0, 0)
    src_posts = [md for pid, md in pmo.comments if pid == "src"]
    assert any("`devcake:discovery-routed:v1 step=2 to=-`" in md
               for md in src_posts)
    assert ("src", {"DEVCAKE-DISCOVERY"}, set()) in pmo.swaps


def test_finalize_steward_branches_on_duty(tmp_path):
    pmo, mgr, run = _route_setup(tmp_path)
    mgr.runs.store.save(run)
    payload = {"result": {"outcome": "stewarded", "summary": "s",
                          "routes": [_route()]},
               "token_report": {"extraction_method": "unavailable"}}
    asyncio.new_event_loop().run_until_complete(
        steward.finalize_steward(mgr, run, payload))
    assert run.state == "finished"
    assert any("discovery-in:v1" in md for pid, md in pmo.comments
               if pid == "tgt")
    assert pmo.relations == []                 # the relations arm never ran


def test_finalize_steward_stamps_relations_outcome_summary(tmp_path):
    """CAKE-167: UI-readable compact line from apply counts (not OTel only)."""
    pmo = MapPMO([m("ia", "T-A"), m("ib", "T-B"), m("ic", "T-C")])
    mgr = make_mgr(tmp_path, pmo)
    run = Run(run_id="L-TEAM-9-STEWARD-BBBBBB", mission_key="TEAM",
              mission_type="STEWARD", dev_type="steward", seq=9,
              pmo_ref=mgr.instance_name, steward_duty="",
              state="finalizing")
    mgr.runs.store.save(run)
    payload = {"result": {"outcome": "stewarded", "summary": "s",
                          "edges": [
                              {"blocker": "T-A", "blocked": "T-B"},
                              {"blocker": "T-A", "blocked": "T-C"},
                              {"blocker": "T-A", "blocked": "T-A"},
                          ]},
               "token_report": {"extraction_method": "unavailable"}}
    run_coro(steward.finalize_steward(mgr, run, payload))
    saved = mgr.runs.store.get(run.run_id)
    assert saved.state == "finished"
    assert saved.outcome_summary == "2 relations proposed (1 rejected)"


def test_finalize_steward_stamps_discovery_outcome_summary(tmp_path):
    """CAKE-167: discovery duty uses the shared/rejected phrasing."""
    pmo, mgr, run = _route_setup(tmp_path)
    mgr.runs.store.save(run)
    payload = {"result": {"outcome": "stewarded", "summary": "s",
                          "routes": [_route()]},
               "token_report": {"extraction_method": "unavailable"}}
    run_coro(steward.finalize_steward(mgr, run, payload))
    saved = mgr.runs.store.get(run.run_id)
    assert saved.state == "finished"
    assert saved.outcome_summary == "1 discoveries shared (0 rejected)"


def test_finalize_steward_failed_leaves_outcome_summary_empty(tmp_path):
    """Failed steward keeps relying on error/verdict — no summary line."""
    mgr = make_mgr(tmp_path, MapPMO([]))
    run = Run(run_id="L-TEAM-8-STEWARD-CCCCCC", mission_key="TEAM",
              mission_type="STEWARD", dev_type="steward", seq=8,
              pmo_ref=mgr.instance_name, state="finalizing")
    mgr.runs.store.save(run)
    run_coro(steward.finalize_steward(
        mgr, run, {"result": {"outcome": "nope"},
                   "token_report": {"extraction_method": "unavailable"}}))
    saved = mgr.runs.store.get(run.run_id)
    assert saved.state == "failed"
    assert saved.outcome_summary == ""


def test_apply_routes_skips_a_finding_already_on_the_recipient(tmp_path):
    """ADR-0033 addendum (content dedup): the same finding reaching a
    recipient from a second source is not delivered again — the fingerprint
    line of an earlier delivery (any source) is authoritative. The receipt
    still records the route so the batch dispositions."""
    from devcake.domain.orchestrator.markers import (
        FINDING_MARKER_RE, finding_fingerprint)
    sha = finding_fingerprint({"finding": "Finding 0 about the CONFIG!"})
    earlier = ("`devcake:discovery-in:v1 src=T-Q step=1`\n"
               "leads, not truths\n"
               "- finding 0 about the config\n"
               f"  `devcake:finding:v1 sha={sha}`\n")
    pmo, mgr, run = _route_setup(tmp_path, recipient_bodies=[earlier])
    mgr._discoveries_pending.add("src")
    audits = []
    mgr._audit = lambda pid, ev, *a, **kw: audits.append((pid, ev, a))
    delivered, rejected = _apply(mgr, run, [_route()])
    assert (delivered, rejected) == (0, 0)     # like the pair dedup: skipped
    assert not any(pid == "tgt" for pid, _ in pmo.comments)   # nothing new
    assert any(ev == "discovery_route_duplicate_content" and pid == "src"
               for pid, ev, _a in audits)
    assert any("`devcake:discovery-routed:v1 step=2 to=T-T`" in md
               for md in _src_comments(pmo))
    assert ("src", {"DEVCAKE-DISCOVERY"}, set()) in pmo.swaps
    assert "src" not in mgr._discoveries_pending
    # a fresh finding from the same batch still lands, stamped with its sha
    pmo2, mgr2, run2 = _route_setup(tmp_path / "b", recipient_bodies=[earlier])
    delivered, rejected = _apply(mgr2, run2, [_route(finding=2)])
    assert (delivered, rejected) == (1, 0)
    body = next(md for pid, md in pmo2.comments if pid == "tgt")
    assert "finding 1 about the config" in body
    assert "finding 0 about the config" not in body
    stamped = FINDING_MARKER_RE.findall(body)
    assert stamped == [finding_fingerprint({"finding": "finding 1 about the config"})]
    assert sha not in stamped


def test_delivery_over_the_inline_ceiling_keeps_fingerprints(
        tmp_path, monkeypatch):
    """The ceiling fallback drops finding bodies but never a counted marker;
    the per-finding fingerprints ride the head so content dedup survives a
    truncated delivery."""
    from devcake.domain.orchestrator.markers import (
        FINDING_MARKER_RE, finding_fingerprint)
    monkeypatch.setattr(steward, "FEED_INLINE_MAX", 240)
    pmo, mgr, run = _route_setup(tmp_path)
    delivered, rejected = _apply(mgr, run, [_route(finding=1),
                                            _route(finding=2)])
    assert (delivered, rejected) == (2, 0)
    body = next(md for pid, md in pmo.comments if pid == "tgt")
    assert "`devcake:discovery-in:v1 src=T-S step=2`" in body
    assert "about the config" not in body                 # bodies dropped
    assert sorted(FINDING_MARKER_RE.findall(body)) == sorted(
        finding_fingerprint({"finding": f"finding {i} about the config"})
        for i in (0, 1))
    # ...and a re-route of either finding is now a content duplicate
    pmo2, mgr2, run2 = _route_setup(tmp_path / "b", recipient_bodies=[body])
    assert _apply(mgr2, run2, [_route(finding=2)]) == (0, 0)
    assert not any(pid == "tgt" for pid, _ in pmo2.comments)


def test_delivery_body_round_trips_as_a_content_duplicate(tmp_path):
    """The real delivery (blockquoted findings, unquoted head) re-read from
    the recipient's feed is recognised — by the pair marker first — and its
    fingerprints survive `unquoted` (the cross-source scan surface)."""
    from devcake.domain.orchestrator.feed import unquoted
    from devcake.domain.orchestrator.markers import (
        finding_fingerprint, finding_fingerprints)
    pmo, mgr, run = _route_setup(tmp_path)
    assert _apply(mgr, run, [_route()]) == (1, 0)
    body = next(md for pid, md in pmo.comments if pid == "tgt")
    assert finding_fingerprint({"finding": "finding 0 about the config"}) \
        in finding_fingerprints(unquoted(body))
    pmo2, mgr2, run2 = _route_setup(tmp_path / "b", recipient_bodies=[body])
    assert _apply(mgr2, run2, [_route()]) == (0, 0)   # pair dedup precedent
    assert not any(pid == "tgt" for pid, _ in pmo2.comments)
    assert any("`devcake:discovery-routed:v1 step=2 to=T-T`" in md
               for md in _src_comments(pmo2))


def test_recipient_never_receives_back_its_own_discovery(tmp_path):
    """The harvest post carries the same fingerprints, so a sibling routing
    the fact a mission discovered itself is a content duplicate."""
    from devcake.domain.orchestrator import discovery
    own = Run(run_id="L-T-T-1-EXECUTE-AAAAAA", mission_key="T-T",
              mission_pmo_id="tgt", mission_type="EXECUTE",
              dev_type="senior-dev", seq=1, state="finished")
    harvest_body, externalize = discovery.comment_body(
        own, [{"finding": "Finding 0 about the config",
               "evidence": "e", "scope": "s"}],
        "DISCOVERY_1.md", "https://files.example.test/d1")
    assert externalize is False
    assert harvest_body.count("`devcake:finding:v1 sha=") == 1
    pmo, mgr, run = _route_setup(tmp_path, recipient_bodies=[harvest_body])
    assert _apply(mgr, run, [_route()]) == (0, 0)
    assert not any(pid == "tgt" for pid, _ in pmo.comments)
    # the receipt says to=T-T yet nothing from T-S is there: it explains why
    assert any("already on the recipient from another source" in md
               and "`devcake:discovery-routed:v1 step=2 to=T-T`" in md
               for md in _src_comments(pmo))


def test_two_sources_proposing_the_same_finding_in_one_run_land_once(
        tmp_path):
    from devcake.domain.orchestrator.markers import (
        FINDING_MARKER_RE, discovery_marker)
    src2 = m("src2", "T-Z", status="in_progress")
    pmo, mgr, run = _route_setup(tmp_path, extra_missions=(src2,))
    tgt = next(mm for mm in pmo.missions if mm.pmo_id == "tgt")
    tgt.blocked_by.append("src2")                       # same family
    pmo.feeds["src2"] = Activity(
        mission=src2, entries=[_ae(discovery_marker(1, 1))], truncated=False)
    r2 = Run(run_id="L-T-Z-1-EXECUTE-AAAAAA", mission_key="T-Z",
             mission_pmo_id="src2", mission_type="EXECUTE",
             dev_type="senior-dev", seq=1, state="finished",
             pmo_ref=mgr.instance_name)
    r2.result = {"outcome": "executed", "summary": "s", "discoveries": [
        {"finding": "FINDING 0 about the config.", "evidence": "other",
         "scope": "other"}]}
    mgr.runs.store.save(r2)
    run.steward_batches.append({"pmo_id": "src2", "key": "T-Z", "step": 1})
    delivered, rejected = _apply(mgr, run, [
        _route(), _route(source="T-Z", step=1, finding=1)])
    assert (delivered, rejected) == (1, 0)
    posts = [md for pid, md in pmo.comments if pid == "tgt"]
    assert len(posts) == 1
    assert "`devcake:discovery-in:v1 src=T-S step=2`" in posts[0]
    assert "src=T-Z" not in posts[0]
    assert len(FINDING_MARKER_RE.findall(posts[0])) == 1
    receipts = [md for pid, md in pmo.comments if pid == "src2"]
    assert any("`devcake:discovery-routed:v1 step=1 to=T-T`" in md
               for md in receipts)


def test_fingerprint_is_script_agnostic():
    from devcake.domain.orchestrator.markers import finding_fingerprint
    fp = finding_fingerprint
    assert fp({"finding": "Übergabe schlägt fehl!"}) == \
        fp({"finding": "übergabe   schlägt fehl"})
    assert fp({"finding": "Übergabe schlägt fehl"}) != \
        fp({"finding": "bergabe schl gt fehl"})
    assert fp({"finding": "配置缺失"}) != fp({"finding": "凭据缺失"})
    assert fp({"finding": "配置缺失"}) != fp({"finding": ""})
    assert fp({"finding": "The cache is stale."}) == \
        fp({"finding": "the CACHE is stale"})
    assert len(fp({"finding": "x"})) == 12
