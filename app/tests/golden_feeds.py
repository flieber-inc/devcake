"""The recorder behind the ADR-0042 golden test: run a three-step mission
(ONBOARD → EXECUTE → REVIEW) through `finalize` on the test fakes and
capture the feed as posted — bodies in order, the attachment names each
comment links — as plain JSON. The same function ran against the
pre-card build to record `fixtures/feeds/legacy_three_step.json`; the test
runs it against the current build and asserts that BOTH feeds unfold to
the same Dev folder. Synthetic identifiers only."""
from __future__ import annotations

import json
from types import SimpleNamespace

from devcake.domain.run import Run

from test_transitions import FakeForge, make_mgr, mission, run_coro

ENTRY = {"finding": "the config default changed to 5",
         "evidence": "src/config.py:42; repro: pytest -k retries",
         "scope": "every consumer of the retry setting"}

STEPS = [
    ("ONBOARD", {"outcome": "plan_needed", "summary": "s"},
     "Assessment: normal complexity, plan needed.\n\nTwo files change."),
    ("EXECUTE", {"outcome": "executed", "summary": "s",
                 "pr_url": "https://forge/pr/8", "discoveries": [ENTRY]},
     "Root cause: the skip gate.\n\nFix in the pull request."),
    ("REVIEW", {"outcome": "reviewed", "verdict": "approve",
                "report_md": "Review complete. Verdict: approve.",
                "summary": "s"},
     "Verdict: approve. The diff stays inside scope."),
]


def record_three_step(tmp_path) -> dict:
    """{"entries": [{"body", "attachments": [name…]}…], "uploads": [name…],
    "description": …} — the feed as the fakes saw it."""
    m = mission("in_progress", {"DEVCAKE"})
    forge = FakeForge()
    forge.descriptor = SimpleNamespace(pr_noun="pull request", pr_instructions="")
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    for seq, (mtype, result, last) in enumerate(STEPS, 1):
        # ONBOARD is dispatched off the bare DEVCAKE label (no stage label)
        stage = None if mtype == "ONBOARD" else f"DEVCAKE-{mtype}"
        m.labels = {"DEVCAKE"} | ({stage} if stage else set())
        run = Run(run_id=f"T-1-{seq}-{mtype}-AAAAAA", mission_key="T-1",
                  mission_pmo_id="p1", mission_type=mtype,
                  dev_type="senior-dev", seq=seq,
                  stage_label_at_dispatch=stage,
                  state="finalizing", repo_ref="main")
        store.save(run)
        run_coro(mgr.finalize(run, {
            "result": result, "transcript_md": f"dump of step {seq}",
            "last_message_md": last,
            "token_report": {"extraction_method": "unavailable", "model": "m"}}))
    uploads = [name for name, _ in fake.uploads]
    entries = [{"body": body,
                "attachments": [n for n in uploads if f"https://fake/{n}" in body]}
               for body in fake.comments]
    return {"entries": entries, "uploads": uploads,
            "description": m.description or ""}


if __name__ == "__main__":          # record a fixture: python golden_feeds.py OUT.json
    import sys
    import tempfile
    from pathlib import Path
    out = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as d:
        out.write_text(json.dumps(record_three_step(Path(d)), indent=1,
                                  ensure_ascii=False) + "\n")
    print("recorded", out, len(json.loads(out.read_text())["entries"]), "entries")
