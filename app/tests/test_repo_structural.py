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
    read-only. (2026-08-13: steps moved to the docker.run action form —
    volumes live under `with:`; mount semantics re-measured live on 2.13.0.)"""
    steps = _steps()
    prov = steps["provision"]["with"]["volumes"]
    dev = steps["run_dev"]["with"]["volumes"]
    assert any(v == "devcake_mirrors:/mirrors:ro" for v in prov), \
        "provision must mount the mirror volume READ-ONLY"
    assert not any("/mirrors" in v for v in dev), \
        "the agent container must never see /mirrors (ADR-0025)"


def test_both_steps_share_exactly_the_per_run_workspace():
    steps = _steps()
    ws = "$DEVCAKE_WS_HOST/${params.RUN_ID}:/workspace"
    for sid in ("provision", "run_dev"):
        vols = steps[sid]["with"]["volumes"]
        assert ws in vols, f"{sid} must bind the per-run workspace"
    # and nothing else rides into the agent container
    assert steps["run_dev"]["with"]["volumes"] == [ws]


def test_both_steps_thread_the_container_limits_nested():
    """ContainerLimits ride the NESTED `resources:` key on BOTH steps —
    nested ON PURPOSE at Dagu 2.13.0 (its host: decode lacks mapstructure
    Squash, so the flat form silently drops embedded Resources fields —
    measured; upstream fix dagucloud/dagu#2557). When a future bump ships
    the fix, the nested form stops matching: FLATTEN the keys into host:
    and update this pin WITH a live inspect proof (Memory lands nonzero)."""
    steps = _steps()
    for sid in ("provision", "run_dev"):
        host = steps[sid]["with"]["host"]
        res = host["resources"]
        assert res["Memory"] == "${params.MEMORY_BYTES}"
        assert res["NanoCPUs"] == "${params.NANO_CPUS}"
        assert res["PidsLimit"] == "${params.PIDS}"
        assert host["NetworkMode"] == "devcake_runtime"
        # env must ride the SDK container.Env list — the docker.run `env:`
        # key is silently DROPPED by 2.13.0's decode (measured: a hello run
        # died phaseless at the startup grace before this moved)
        env = steps[sid]["with"]["container"]["Env"]
        assert any(e.startswith("DEVCAKE_PHASE=") for e in env)
        assert "env" not in steps[sid]["with"]


def test_devcake_images_never_pull():
    """Audit A7: pins name tags the operator typed. pull: missing would
    reach Docker Hub for a never-baked :TAG-cli_version."""
    doc = _dag()
    steps = _steps()
    handler = doc.get("handler_on") or doc.get("handlerOn")
    withs = [
        handler["exit"]["with"],
        steps["provision"]["with"],
        steps["run_dev"]["with"],
    ]
    for with_ in withs:
        assert with_["pull"] == "never", with_


def test_run_id_precondition_fences_the_charset():
    doc = _dag()
    pres = {p.get("condition"): p.get("expected") for p in doc["preconditions"]}
    assert pres.get("${params.RUN_ID}") == "re:^[A-Za-z0-9_-]{6,64}$", \
        "the RUN_ID charset fence must match WorkspaceStore/docs (AUD-011)"


def test_no_ws_host_precondition_reappears_unverified():
    """Inverse guard, from a live incident (2026-08-04): a ${DEVCAKE_WS_HOST}
    precondition LOADED cleanly but never matched — Dagu 2.11.3 does not
    expand service env in `condition:` — so every dev-run sat `dispatched`
    until the startup grace killed it. If someone re-adds a WS_HOST fence,
    this fails until they delete it here WITH a live hello-smoke proof that
    the expansion now works (the load-clean trap is exactly what bit us)."""
    doc = _dag()
    conditions = [p.get("condition") for p in doc["preconditions"]]
    assert "${DEVCAKE_WS_HOST}" not in conditions, \
        "re-adding the WS_HOST precondition requires live-smoke proof — see docstring"


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


def test_both_steps_carry_the_nested_engine_knobs():
    """ADR-0023 addendum (founder go 2026-08-13): rootless podman rides a
    CUSTOM seccomp profile — Docker's default plus ONE allow rule for the
    userns/mount syscall set — inline in the DAG (the Docker API takes
    profile CONTENT; only the docker CLI reads files). NOT unconfined: this
    pin json-parses the blob and asserts the rule, so a lazy
    'seccomp=unconfined' can never slip in. /dev/fuse rides the nested
    Resources block (embedded-struct decode, dagucloud/dagu#2557) as the
    fuse-overlayfs fallback for kernels <5.13."""
    import json
    steps = _steps()
    blobs = set()
    for sid in ("provision", "run_dev"):
        host = steps[sid]["with"]["host"]
        so = host["SecurityOpt"]
        assert len(so) == 1 and so[0].startswith("seccomp={"), \
            "nested-engine seccomp must be an inline profile, never unconfined"
        blobs.add(so[0])
        prof = json.loads(so[0][len("seccomp="):])
        assert prof["defaultAction"] != "SCMP_ACT_ALLOW"
        # the FULL measured 15-name rule, as one unconditional entry — set
        # EQUALITY (audit: a >= subset check would tolerate a rule that
        # silently grew, and Docker's own CAP_SYS_ADMIN rule nearly
        # satisfies a subset)
        engine_rule = {
            "clone", "clone3", "unshare", "setns", "mount", "umount2",
            "pivot_root", "keyctl", "move_mount", "open_tree", "fsopen",
            "fsconfig", "fsmount", "sethostname", "setdomainname"}
        assert any(set(r.get("names", [])) == engine_rule
                   and r.get("action") == "SCMP_ACT_ALLOW"
                   and not r.get("includes") and not r.get("excludes")
                   for r in prof["syscalls"]), \
            "the exact 15-syscall nested-engine allow rule is gone"
        devs = host["resources"]["Devices"]
        assert {"PathOnHost": "/dev/fuse", "PathInContainer": "/dev/fuse",
                "CgroupPermissions": "rwm"} in devs
        assert {"PathOnHost": "/dev/net/tun",
                "PathInContainer": "/dev/net/tun",
                "CgroupPermissions": "rwm"} in devs   # pasta tap networking
    assert len(blobs) == 1               # anchor+alias: ONE profile, two steps


def test_exit_handler_reclaims_workspace_ownership():
    """Audit 2026-08-13 B1: nested podman writes land on /workspace as host
    uid 100000+ — the app (uid 1000) could neither chmod nor unlink them.
    The DAG's exit handler re-chowns as root; DELETION stays in
    workspaces.py (the one reclaim seam). Live-drilled on 2.13.0: runs on
    failure AND abort, SKIPPED on rejected runs (the step precondition —
    without it, an empty RUN_ID would degrade the bind to the workspaces
    ROOT and chown every live run's tree)."""
    doc = _dag()
    h = doc["handler_on"]["exit"]
    # the fence is load-bearing — same regex as the DAG-level precondition
    assert h["preconditions"] == [
        {"condition": "${params.RUN_ID}",
         "expected": "re:^[A-Za-z0-9_-]{6,64}$"}], \
        "exit handler must carry its own RUN_ID fence (runs even on Rejected)"
    assert h["action"] == "docker.run"
    w = h["with"]
    # ONLY the per-run workspace — never the base, never the mirrors
    assert w["volumes"] == ["$DEVCAKE_WS_HOST/${params.RUN_ID}:/workspace"]
    # User must ride the SDK container.User field — a sibling `user:` /
    # `User:` on `with:` is the same silent-drop class as docker.run's
    # documented `env:` key (2.13.0 decode).
    assert w["container"]["User"] == "0"
    assert "user" not in w and "User" not in w
    ep = " ".join(w["container"]["Entrypoint"])
    assert "chown -R -h 1000:1000 /workspace" in ep and "|| true" in ep
    assert w["host"] == {"NetworkMode": "none"}
    assert w["auto_remove"] is True
    assert h["timeout_sec"] > 0
