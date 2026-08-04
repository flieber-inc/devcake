"""Repo-level structural guards (2026-08 evaluation): claims that lived as
"probe-verified" prose in comments become executable rules.

These files sit OUTSIDE the app tree, so the runners mount them read-only
(`scripts/pytest_app.sh` and ci.yml both add the mounts). A missing mount
FAILS — a skip here would silently un-enforce the isolation contract.
"""

from pathlib import Path

import yaml

DAG = Path("/srv/dagu-dags/dev-run.yaml")
DOCKERFILE = Path("/srv/app.Dockerfile")

MOUNT_HINT = ("mount missing — the pytest runner must bind {src} "
              "(see scripts/pytest_app.sh / ci.yml)")


def _dag():
    assert DAG.exists(), MOUNT_HINT.format(src="dagu/dags → /srv/dagu-dags")
    return yaml.safe_load(DAG.read_text())


def _steps():
    doc = _dag()
    return {s["id"]: s for s in doc["steps"]}


def test_agent_step_never_mounts_the_mirrors():
    """ADR-0025's whole point, previously enforced by a comment ("no mirrors
    line; that is the point") and a one-time manual probe: the agent's
    container must not see /mirrors — only the provision step may, and only
    read-only."""
    steps = _steps()
    prov = steps["provision"]["container"]["volumes"]
    dev = steps["run_dev"]["container"]["volumes"]
    assert any(v == "devcake_mirrors:/mirrors:ro" for v in prov), \
        "provision must mount the mirror volume READ-ONLY"
    assert not any("/mirrors" in v for v in dev), \
        "the agent container must never see /mirrors (ADR-0025)"


def test_both_steps_share_exactly_the_per_run_workspace():
    steps = _steps()
    ws = "$DEVCAKE_WS_HOST/${params.RUN_ID}:/workspace"
    for sid in ("provision", "run_dev"):
        vols = steps[sid]["container"]["volumes"]
        assert ws in vols, f"{sid} must bind the per-run workspace"
    # and nothing else rides into the agent container
    assert steps["run_dev"]["container"]["volumes"] == [ws]


def test_run_id_precondition_fences_the_charset():
    doc = _dag()
    pres = {p.get("condition"): p.get("expected") for p in doc["preconditions"]}
    assert pres.get("${params.RUN_ID}") == "re:^[A-Za-z0-9_-]{6,64}$", \
        "the RUN_ID charset fence must match WorkspaceStore/docs (AUD-011)"


def test_no_dagu_auto_retry():
    """retry_policy limit 0 — Dagu retries would fight DevCake's own attempt
    counting (M1 field note)."""
    doc = _dag()
    for s in doc["steps"]:
        rp = s.get("retry_policy") or doc.get("retry_policy") or {}
        assert rp.get("limit", 0) == 0


def test_app_runs_one_uvicorn_worker():
    """The no-lock design (RunStore parse cache, process-local dispatch lock,
    'the app is ONE process') is only sound single-worker. The premise was
    stated in comments; this makes it a rule."""
    assert DOCKERFILE.exists(), \
        MOUNT_HINT.format(src="app/Dockerfile → /srv/app.Dockerfile")
    text = DOCKERFILE.read_text()
    uvicorn_lines = [ln for ln in text.splitlines()
                     if "uvicorn" in ln and ln.strip().startswith("CMD")]
    assert uvicorn_lines, "app CMD must start uvicorn"
    assert all("--workers" not in ln for ln in uvicorn_lines), \
        "multi-worker uvicorn breaks the single-process store premise"
    assert "gunicorn" not in text


def test_structural_mounts_are_enforced_not_optional():
    """Belt for the belt: both mounted files must exist — if a runner drops a
    mount, every test above must fail loudly rather than skip silently."""
    assert DAG.exists() and DOCKERFILE.exists()
