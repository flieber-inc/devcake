"""ADR-0032 addendum — a mission prompt's accumulated sections are built to
byte budgets: the prompt is one argv element with a hard kernel ceiling
(docs/07 §4 exit 17), and a mission gated on hundreds of finished siblings
carried a quarter-megabyte of handoff excerpts on the command line. The
blocker note and the reference-repository list keep what fits, count the
rest, and point at the workspace, which always holds everything."""
import logging

from fakes import make_mission_manager

from devcake.domain.orchestrator import dispatch
from devcake.domain.orchestrator.markers import HANDOFF_EXCERPT_MAX
from devcake.prompts import (BLOCKER_NOTE_MAX_BYTES, PROMPT_MAX_BYTES,
                             REFERENCE_REPOS_NOTE_MAX_BYTES,
                             STEWARD_PROMPT_MAX_BYTES, onboard_prompt)

from test_transitions import mission  # noqa: F401


def _notes(n: int) -> list[dict[str, str]]:
    return [{"mission_key": f"T-{i}", "title": f"title {i}",
             "handoff": ("h" * (HANDOFF_EXCERPT_MAX - 10)) + f" end{i}"}
            for i in range(n)]


def test_budgets_sit_under_the_kernel_ceiling():
    # the sum of the app-built sections must leave room for identity,
    # playbook and brief under the whole-prompt budget, which itself sits
    # under MAX_ARG_STRLEN (131,072) with headroom
    assert PROMPT_MAX_BYTES < 131072
    assert STEWARD_PROMPT_MAX_BYTES == PROMPT_MAX_BYTES
    assert BLOCKER_NOTE_MAX_BYTES + REFERENCE_REPOS_NOTE_MAX_BYTES \
        < PROMPT_MAX_BYTES // 2


def test_blocker_note_keeps_head_counts_rest_and_points_at_mission_md(
        tmp_path, caplog):
    mgr = make_mission_manager(tmp_path)
    notes = _notes(400)                        # ≈ 300 KB unbudgeted
    with caplog.at_level(logging.INFO, logger="devcake.missions"):
        note = dispatch._blocker_repos_note(mgr, [], [], notes)
    assert len(note.encode()) <= BLOCKER_NOTE_MAX_BYTES
    # the head rides in order, the tail is counted, never silently dropped
    assert "`T-0` — title 0" in note and "end0" in note
    kept = sum(1 for n in notes if f"`{n['mission_key']}` —" in note)
    assert 0 < kept < 400
    assert f"{400 - kept} more finished blocker(s)" in note
    assert "/workspace/activity/MISSION.md" in note
    assert "Blocked by (completed — handoffs)" in note
    assert "the handoff is newer" in note      # the staleness rule survives
    assert any("blocker note trimmed to the prompt budget" in r.message
               for r in caplog.records)


def test_blocker_note_mounted_blockers_ride_first(tmp_path):
    mgr = make_mission_manager(tmp_path)
    notes = _notes(400)
    # the mounted blocker is the LAST note — it must still be listed, with
    # its handoff, ahead of the note-only ones
    entries = [{"repo_ref": "linear-t-399", "mission_key": "T-399"}]
    note = dispatch._blocker_repos_note(mgr, entries, [], notes)
    assert "`T-399` (`linear-t-399`) → /workspace/repo/" in note
    assert "end399" in note
    assert note.index("`T-399`") < note.index("`T-0`")
    assert "(no work-repo mount)" in note        # the note-only head too


def test_blocker_note_skip_reasons_share_the_budget(tmp_path):
    mgr = make_mission_manager(tmp_path)
    skips = [f"T-{i}: canceled — no work tree" for i in range(2000)]
    note = dispatch._blocker_repos_note(mgr, [], skips, [])
    assert len(note.encode()) <= BLOCKER_NOTE_MAX_BYTES
    assert "(skipped: T-0: canceled — no work tree)" in note
    listed = note.count("(skipped: ")
    assert 0 < listed < 2000
    assert f"{2000 - listed} more skipped blocker(s)" in note


def test_blocker_note_small_sets_are_untouched(tmp_path):
    mgr = make_mission_manager(tmp_path)
    note = dispatch._blocker_repos_note(
        mgr, [], ["T-9: not done (in_progress)"], _notes(5))
    assert "prompt budget" not in note
    assert all(f"end{i}" in note for i in range(5))
    assert "(skipped: T-9: not done (in_progress))" in note


def test_reference_repos_note_is_budgeted(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.adapters.registry import make_forge
    from devcake.config import PMOInstance, RepoInstance
    from devcake.domain.forge_runtime import ForgeRuntime
    from test_transitions import make_mgr
    names = [f"ref{i:03d}" for i in range(400)]
    rt = ForgeRuntime()
    rt.rebuild([RepeatInstance for RepeatInstance in
                [RepoInstance(name="alpha", url="https://github.com/o/a")]
                + [RepoInstance(name=n, url=f"https://github.com/o/{n}")
                   for n in names]], make_forge)
    m = mission()
    mgr, _fake, _store = make_mgr(tmp_path, m)
    mgr.forges = rt
    mgr.internal_forge = None
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["alpha"],
                               reference_repos=names)
    with caplog.at_level(logging.INFO, logger="devcake.missions"):
        note = dispatch._reference_repos_note(mgr, "alpha")
    assert len(note.encode()) <= REFERENCE_REPOS_NOTE_MAX_BYTES
    assert "`ref000` → /workspace/repo/ref000/" in note
    listed = note.count("→ /workspace/repo/")
    assert 0 < listed < 400
    assert f"{400 - listed} more reference repositories" in note
    assert "list that directory" in note
    assert "NEVER modify" in note
    assert any("reference-repository note trimmed" in r.message
               for r in caplog.records)
    # and the budgeted note still renders in a stage prompt
    out = onboard_prompt("ID", m, reference_repos=note)
    assert "Reference repositories (read-only)" in out


def test_prompt_size_report_names_the_heavy_section():
    small = "x" * 100
    assert dispatch.prompt_size_report(small, {"identity": small}) is None
    brief = "b" * (PROMPT_MAX_BYTES + 1)
    line = dispatch.prompt_size_report(
        "ID" + brief, {"identity": "ID", "description": brief,
                       "blocker note": ""})
    assert line is not None
    assert "past the" in line and "description" in line
    assert "blocker note" not in line            # empty sections are silent
