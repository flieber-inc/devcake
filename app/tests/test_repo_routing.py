"""Per-mission repo resolution (M10, docs/16 F3): the marker/default/zero
table, STICKINESS across routing edits (plan finding H3), and the
resolution-failure contract (vanished repos never crash or silently wedge)."""

import asyncio
from datetime import datetime, timezone

from devcake.config import PMOInstance
from devcake.domain.model import Mission
from devcake.domain.orchestrator import review
from devcake.domain.repo_routing import (REASON_ZERO_REPO, marker_repo,
                                         resolve_repo)
from devcake.domain.run import Run
from devcake.domain.orchestrator import dispatch, sweeps
from devcake.domain.orchestrator.markers import DELIVERABLE_MARKER


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _m(description="", key="T-1"):
    return Mission(pmo_id="p1", pmo_kind="issue", instance="linear", key=key,
                   title="t", status="backlog", description=description,
                   updated_at=datetime.now(timezone.utc))


def _run(repo_ref, seq=1):
    return Run(run_id=f"LINEAR-T-1-{seq}-EXECUTE-AAAAA{seq}", mission_key="T-1",
               mission_type="EXECUTE", dev_type="d", seq=seq, repo_ref=repo_ref)


INST = PMOInstance(name="linear", team_key="DEV")
INST_DEF = PMOInstance(name="linear", team_key="DEV", repos=["alpha"])
INST_SET = PMOInstance(name="linear", team_key="DEV", repos=["alpha", "beta"])
REPOS = {"alpha", "beta"}


def test_marker_parsing():
    assert marker_repo("body\n`devcake-repo:beta`\nrest") == "beta"
    assert marker_repo("`devcake-repo:BETA`") == "beta"     # case-insensitive
    assert marker_repo("devcake-repo:beta") is None         # backticks required
    assert marker_repo("") is None


def test_resolution_table_virgin_missions():
    # marker wins over the instance default (within the instance's repo set)
    assert resolve_repo(_m("`devcake-repo:beta`"), INST_SET, REPOS, []) == ("beta", None)
    # unknown marker gates with the fix-the-marker reason
    name, reason = resolve_repo(_m("`devcake-repo:gone`"), INST_DEF, REPOS, [])
    assert name is None and "unknown repo 'gone'" in reason
    # no marker → instance default
    assert resolve_repo(_m(), INST_DEF, REPOS, []) == ("alpha", None)
    # no marker, no default → zero-repo gate (un-gated in M11)
    assert resolve_repo(_m(), INST, REPOS, []) == (None, REASON_ZERO_REPO)


def test_resolution_sticky_once_a_run_exists():
    """Plan finding H3 + founder decision 2026-07-14 (audit A25): attempt
    1's PR/branch live on the resolved repo. A conflicting MARKER edit
    gates (explicit per-mission intent, visible human action needed); a
    changed instance DEFAULT does not — sticky wins silently, because a
    config default edit must not park every in-flight mission of the
    instance (and setting a FIRST default must not park every in-flight
    internal-forge mission)."""
    history = [_run("beta")]
    # sticky wins when no fresh signal disagrees (no marker, no default)
    assert resolve_repo(_m(), INST, REPOS, history) == ("beta", None)
    # a conflicting marker edit still gates
    name, reason = resolve_repo(_m("`devcake-repo:alpha`"), INST_DEF, REPOS, history)
    assert name is None and "sticky" in reason and "'beta'" in reason
    # a changed instance default: sticky wins SILENTLY (no gate)
    assert resolve_repo(_m(), INST_DEF, REPOS, history) == ("beta", None)
    # a matching marker is fine
    assert resolve_repo(_m("`devcake-repo:beta`"), INST_DEF, REPOS, history) == ("beta", None)
    # sticky repo vanished from config → gate, explicit restore-or-close reason
    name, reason = resolve_repo(_m(), INST_DEF, {"alpha"}, history)
    assert name is None and "no longer configured" in reason


def test_repo_set_routing_semantics():
    """Item 2 (founder decision 2026-07-15): the instance's repo SET is
    ordered — first entry is the default for unmarked missions; markers
    may pick any LISTED repo; a configured-but-unlisted repo gates."""
    # unmarked → first of the set
    assert resolve_repo(_m(), INST_SET, REPOS, []) == ("alpha", None)
    # marker picks any listed repo
    assert resolve_repo(_m("`devcake-repo:beta`"), INST_SET, REPOS, []) == ("beta", None)
    # configured but NOT in the set → gate with a fix-it reason
    inst_alpha_only = PMOInstance(name="linear", team_key="DEV", repos=["alpha"])
    name, reason = resolve_repo(_m("`devcake-repo:beta`"), inst_alpha_only,
                                REPOS, [])
    assert name is None and "repo set" in reason
    # empty set → the zero-repo gate (internal forge un-gates it)
    assert resolve_repo(_m(), INST, REPOS, []) == (None, REASON_ZERO_REPO)
    # sticky still wins silently over set edits (A25 semantics preserved)
    assert resolve_repo(_m(), INST_SET, REPOS, [_run("beta")]) == ("beta", None)


def test_empty_repo_set_marker_gates_not_external():
    """`repos: []` is zero-repo / per-mission internal only (docs/16 item 2).
    A `devcake-repo:` marker naming a configured external card must GATE as
    unlisted — never silently become a work target. The prior
    `if allowed and marker not in allowed` short-circuit treated the empty
    set as more permissive than a non-empty set (false-green trap for any
    routing test that only exercised unmarked empty instances)."""
    # empty work set + valid known marker → gate (not beta)
    name, reason = resolve_repo(_m("`devcake-repo:beta`"), INST, REPOS, [])
    assert name is None, "empty instance.repos must not accept external markers"
    assert "repo set" in reason and "beta" in reason
    # still not REASON_ZERO_REPO — live path must NOT re-route this to internal
    assert reason is not REASON_ZERO_REPO
    # listed marker still works when the set is non-empty
    assert resolve_repo(_m("`devcake-repo:beta`"), INST_SET, REPOS, []) \
        == ("beta", None)


def test_malformed_marker_gates_instead_of_silent_default():
    """Audit A26: a `devcake-repo:`-shaped but unparseable marker (hyphens,
    >39 chars, bad leading char) previously fell through to the instance
    default — a typo'd routing intent silently landed on the wrong repo and
    stickiness then latched it there. Gate with a fix-the-marker reason."""
    too_long = "a" * 40  # INSTANCE_NAME_BODY max is 39
    for bad in ("`devcake-repo:my-repo`", f"`devcake-repo:{too_long}`",
                "`devcake-repo:9lead`", "`devcake-repo:`",
                "`devcake-repo:has.dot`"):
        name, reason = resolve_repo(_m(bad), INST_DEF, REPOS, [])
        assert name is None and "unparseable" in reason, bad
    # sticky missions gate too — the typo stays visible, nothing re-routes
    name, reason = resolve_repo(_m("`devcake-repo:my-repo`"), INST_DEF, REPOS,
                                [_run("beta")])
    assert name is None and "unparseable" in reason


def test_vanished_repo_contract_in_sweeps_and_review(tmp_path):
    """A DEVCAKE-MERGE-parked mission whose repo vanished must surface a
    visible reason (sweeps) / fail the run cleanly (review) — never crash."""
    from fakes import FakeForgeRuntime, make_mission_manager

    mgr = make_mission_manager(forge_runtime=FakeForgeRuntime(None))
    m = _m()
    m.repo, m.repo_reason = None, "repo 'beta' no longer configured"
    run_coro(sweeps.merge_sweep(mgr, m))                # must not raise
    assert "no longer configured" in mgr.blocked_reasons["p1"]

    run = _run("gone")
    run.mission_pmo_id = "p1"
    run_coro(review.finalize_review(mgr, run, {"verdict": "approve"}))   # must not raise
    assert "no longer configured" in (run.verdict or "")


def test_zero_repo_mission_visible_but_gated(tmp_path):
    """Zero-repo missions derive fully but never dispatch; the reason is
    surfaced through blocked_reasons for /health and the missions API."""
    from fakes import FakeForgeRuntime, make_mission_manager
    from devcake.adapters.files.run_store import RunStore
    from devcake.config import AppConfig, DevType

    mgr = make_mission_manager(
        tmp_path, pmo=None, forge_runtime=FakeForgeRuntime(None),
        config=AppConfig(), instance=INST,
        dev_types={"senior-dev": DevType(name="senior-dev",
                                         harness_template="claude-code")},
        runs=type("Runs", (), {"store": RunStore(tmp_path / "runs")})(),
    )

    m = _m()
    m.labels = {"DEVCAKE"}
    m.repo, m.repo_reason = None, REASON_ZERO_REPO
    dispatched = run_coro(mgr.schedule([m], gate={}))
    assert dispatched == 0
    assert mgr.blocked_reasons["p1"] == REASON_ZERO_REPO


def test_two_repos_route_tokens_and_dialects_per_run(tmp_path, monkeypatch):
    """M10 exit criterion, hermetic half: two configured repos on DIFFERENT
    forges — each run's spec env + secret payload derive from ITS repo
    (url, dialect, token), never from a global."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    secrets_store.write_connection_secret("repo", "ghrepo", "token", "ghp_github_token_000000000000")
    secrets_store.write_connection_secret("repo", "glrepo", "token", "glpat-gitlab_token_000000000")
    from devcake.adapters.registry import make_forge
    from devcake.config import AppConfig, DevType, RepoInstance
    from devcake.domain.forge_runtime import ForgeRuntime
    from devcake.domain.orchestrator import MissionManager

    gh = RepoInstance(name="ghrepo", forge="github", url="https://github.com/o/r")
    gl = RepoInstance(name="glrepo", forge="gitlab", url="https://gitlab.com/g/p")
    rt = ForgeRuntime()
    rt.rebuild([gh, gl], make_forge)
    assert set(rt.forges) == {"ghrepo", "glrepo"}

    from fakes import make_mission_manager
    mgr = make_mission_manager(
        config=AppConfig(),
        forge_runtime=rt,
        instance=PMOInstance(name="linear", team_key="DEV",
                             repos=["ghrepo", "glrepo"]),
        dev_types={"senior-dev": DevType(name="senior-dev",
                                         harness_template="claude-code")},
    )
    dt = mgr.dev_types["senior-dev"]

    env_gh = dispatch._protocol_spec_env(mgr, 
        mission_id="p1", mission_key="T-1", mission_type="EXECUTE",
        dev_type=dt, seq=1, extra_args="", repo=gh, forge=rt.get("ghrepo"))
    env_gl = dispatch._protocol_spec_env(mgr, 
        mission_id="p2", mission_key="T-2", mission_type="EXECUTE",
        dev_type=dt, seq=1, extra_args="", repo=gl, forge=rt.get("glrepo"))
    assert env_gh["DEVCAKE_REPO_URL"] == gh.url and env_gl["DEVCAKE_REPO_URL"] == gl.url
    assert env_gh["DEVCAKE_CLONE_USER"] == "x-access-token"     # github dialect
    assert env_gl["DEVCAKE_CLONE_USER"] == "oauth2"             # gitlab dialect

    secrets_store.write_harness_secret("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    r_gh = _run("ghrepo"); r_gh.mission_type = "EXECUTE"; r_gh.dev_type = "senior-dev"
    r_gl = _run("glrepo", seq=2); r_gl.mission_type = "EXECUTE"; r_gl.dev_type = "senior-dev"
    assert mgr.runspec_secret_payload(r_gh)["env"]["DEVCAKE_FORGE_TOKEN"] \
        == "ghp_github_token_000000000000"
    assert mgr.runspec_secret_payload(r_gl)["env"]["DEVCAKE_FORGE_TOKEN"] \
        == "glpat-gitlab_token_000000000"
    # breaker isolation at the runtime level: latch ghrepo, glrepo untouched
    rt.latch("ghrepo", "401")
    assert "ghrepo" in rt.breakers and "glrepo" not in rt.breakers


def test_resolve_repo_history_assembly(tmp_path):
    """_resolve_repo's history filter: this mission's runs only, STEWARD runs
    excluded, foreign-instance records excluded, newest first."""
    from fakes import FakeForgeRuntime
    from devcake.adapters.files.run_store import RunStore
    from devcake.domain.orchestrator import MissionManager

    store = RunStore(tmp_path / "runs")
    mine_old = _run("alpha"); mine_old.mission_pmo_id = "p1"; mine_old.pmo_ref = "linear"
    mine_new = _run("beta", seq=2); mine_new.mission_pmo_id = "p1"; mine_new.pmo_ref = "linear"
    steward = _run("alpha", seq=3); steward.mission_pmo_id = "p1"
    steward.pmo_ref, steward.mission_type = "linear", "STEWARD"
    other_mission = _run("alpha", seq=4); other_mission.mission_pmo_id = "p9"
    other_instance = _run("alpha", seq=5); other_instance.mission_pmo_id = "p1"
    other_instance.pmo_ref = "linearb"
    from datetime import timedelta
    from devcake.domain.run import utcnow
    mine_old.created_at = utcnow() - timedelta(hours=2)
    mine_new.created_at = utcnow() - timedelta(hours=1)
    for r in (mine_old, mine_new, steward, other_mission, other_instance):
        store.save(r)

    from fakes import make_mission_manager
    fr = FakeForgeRuntime(object())   # instances: {"main"} — irrelevant
    fr._inst.name = "main"
    mgr = make_mission_manager(
        instance=INST,
        forge_runtime=fr,
        runs=type("Runs", (), {"store": store})(),
    )
    # repo names must include beta for sticky to resolve
    from devcake.config import RepoInstance
    import devcake.domain.repo_routing as rr
    got = []
    orig = rr.resolve_repo
    def spy(mission, instance, names, history):
        got.append([r.run_id for r in history])
        return orig(mission, instance, {"alpha", "beta"}, history)
    rr.resolve_repo = spy
    try:
        name, reason = dispatch.resolve_repo(mgr, _m())
    finally:
        rr.resolve_repo = orig
    # newest-first, only THIS mission's non-steward, same-instance records
    assert got[0] == [mine_new.run_id, mine_old.run_id]
    assert name == "beta" and reason is None      # sticky = newest repo_ref


def test_resolve_repo_sticky_survives_mixed_naive_aware_created_at(tmp_path):
    """Sticky history sort must not TypeError on mixed naive/aware created_at;
    newest after aware() (naive as UTC) wins."""
    from fakes import FakeForgeRuntime, make_mission_manager
    from devcake.adapters.files.run_store import RunStore
    import devcake.domain.repo_routing as rr

    store = RunStore(tmp_path / "runs")
    older = _run("alpha")
    older.mission_pmo_id = "p1"
    older.pmo_ref = "linear"
    older.created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    newer = _run("beta", seq=2)
    newer.mission_pmo_id = "p1"
    newer.pmo_ref = "linear"
    newer.created_at = datetime(2026, 1, 2, 12, 0, 0)  # naive
    store.save(older)
    store.save(newer)

    fr = FakeForgeRuntime(object())
    fr._inst.name = "main"
    mgr = make_mission_manager(
        instance=INST,
        forge_runtime=fr,
        runs=type("Runs", (), {"store": store})(),
    )
    orig = rr.resolve_repo

    def spy(mission, instance, names, history):
        return orig(mission, instance, {"alpha", "beta"}, history)

    rr.resolve_repo = spy
    try:
        name, reason = dispatch.resolve_repo(mgr, _m())
    finally:
        rr.resolve_repo = orig
    assert name == "beta" and reason is None


def test_internal_repo_naming_and_port_helper():
    """The internal-repo naming convention lives on the PORT (domain may
    derive it to detect prior internal routing across restarts) — not in the
    adapter (F1 import boundary)."""
    from devcake.ports.internal_forge import internal_repo_name
    assert internal_repo_name("linear", "DEV-17") == "linear-dev-17"
    assert internal_repo_name("linteama", "PRJ-Big Report!") == "linteama-prj-big-report"
    # bounded for the run-id / repo-name budget
    assert len(internal_repo_name("linear", "X" * 80)) <= 60
    # CAKE-151: underscore in the instance identity must survive scrubbing
    assert internal_repo_name("acme_eng", "DEV-1") == "acme_eng-dev-1"
    assert internal_repo_name("acme_eng", "DEV-1") != internal_repo_name(
        "acme", "ENG-DEV-1")


def test_resolve_repo_live_ungates_zero_repo_to_internal(tmp_path, monkeypatch):
    """M11 exit criterion (hermetic half): a mission with no marker and no
    instance default routes to a provisioned internal repo; a mission with an
    UNKNOWN marker stays gated (never silently redirected internal)."""
    from fakes import FakeForgeRuntime
    from devcake.adapters.files.run_store import RunStore
    from devcake.domain.orchestrator import MissionManager
    from devcake.ports.internal_forge import MissionRepoCredentials

    provisioned = []

    class FakeInternal:
        def service_tokens(self):
            return {"reviewer_token": "rev-tok"}

        async def ensure_mission_repo(self, instance, key):
            provisioned.append((instance, key))
            name = f"{instance}-{key}".lower()
            return MissionRepoCredentials(
                repo_name=name, clone_url=f"http://gitea:3000/devcake-internal/{name}.git",
                username=f"svc-{name}", token_write="w-tok", token_read="r-tok")

        def mission_repo_binding(self, creds):
            # port contract (2026-08 F9) — the REAL row doctrine is pinned by
            # test_gitea_mission_repo_binding_row against the gitea adapter;
            # this fake mirrors it so resolve_repo_live's registration path
            # stays observable
            from types import SimpleNamespace
            from devcake.config import RepoInstance
            inst = RepoInstance.model_construct(
                name=creds.repo_name, forge="gitea", url=creds.clone_url,
                default_branch="main", api_base=None,
                auto_merge=True, auto_resolve_merge_conflicts=True,
                merge_retry_window_minutes=30)
            return inst, SimpleNamespace(name="fake-internal-adapter")

    class RT(FakeForgeRuntime):
        def register_internal(self, name, inst, forge):
            self.instances[name] = inst
            self.forges[name] = forge
            self.internal.add(name)

        @property
        def instances(self):
            return self._insts

        @instances.setter
        def instances(self, v):
            self._insts = v

    from fakes import make_mission_manager
    rt = RT(None)
    rt._insts = {}
    mgr = make_mission_manager(
        instance=INST,                                    # no default_repo
        forge_runtime=rt,
        internal_forge=FakeInternal(),
        runs=type("Runs", (), {"store": RunStore(tmp_path / "runs")})(),
    )

    # zero-repo mission → internal (always auto-merge, ADR-0020; the row
    # doctrine itself is pinned by test_gitea_mission_repo_binding_row)
    name, reason = run_coro(mgr.resolve_repo_live(_m()))
    assert name == "linear-t-1" and reason is None
    assert provisioned == [("linear", "T-1")]
    assert "linear-t-1" in rt.internal
    inst = rt.instances["linear-t-1"]
    assert inst.auto_merge is True
    assert inst.auto_resolve_merge_conflicts is True
    assert inst.merge_retry_window_minutes == 30

    # unknown marker → GATED, never redirected internal
    m2 = _m(description="`devcake-repo:nope`")
    name2, reason2 = run_coro(mgr.resolve_repo_live(m2))
    assert name2 is None and "unknown repo 'nope'" in reason2

    # ── audit A4: terminal missions never (re-)provision — Clear sticks ──
    # restart-recovery bait: a prior run points at the internal repo, the
    # runtime lost the registration (admin Clear / restart), mission is done
    rt._insts.pop("linear-t-1", None)
    rt.internal.discard("linear-t-1")
    prior = Run(run_id="LINEAR-T-1-2-EXECUTE-BBBBBB", mission_key="T-1",
                mission_type="EXECUTE", dev_type="d", seq=2,
                repo_ref="linear-t-1")
    prior.mission_pmo_id = "p1"
    mgr.runs.store.save(prior)
    done = _m()
    done.status = "done"
    provisioned.clear()
    name3, reason3 = run_coro(mgr.resolve_repo_live(done))
    assert provisioned == []                  # no resurrection of the repo
    assert name3 is None and reason3          # sticky-vanished gate, unscheduled

    # zero-repo path likewise: a canceled mission with no runs never provisions
    canceled = _m(key="T-9")
    canceled.pmo_id = "p9"
    canceled.status = "canceled"
    name4, reason4 = run_coro(mgr.resolve_repo_live(canceled))
    assert provisioned == [] and name4 is None


def test_build_zip_manifest_attributes_omissions_honestly(tmp_path):
    """Audit A16: MANIFEST.txt blamed every omission on the size cap — a
    fetch failure was listed under the wrong explanation."""
    import io
    import zipfile
    from devcake.domain.orchestrator.deliver import _SAFETY, _build_zip
    from devcake.ports.forge import PRFile

    class F:
        async def file_content(self, path, ref):
            if path == "bad.bin":
                raise RuntimeError("500 from forge")
            return b"x" * (2000 if path == "big.bin" else 10)

    files = [PRFile(path="ok.txt", status="modified"),
             PRFile(path="bad.bin", status="modified"),
             PRFile(path="big.bin", status="added")]
    data, omitted = run_coro(_build_zip(F(), files, "main", cap=_SAFETY + 100))
    assert set(omitted) == {"bad.bin", "big.bin"}
    z = zipfile.ZipFile(io.BytesIO(data))
    assert "ok.txt" in z.namelist()
    manifest = z.read("MANIFEST.txt").decode()
    fetch_part = manifest.split("could not be fetched")[1]
    assert "bad.bin" in fetch_part.split("size cap")[0]
    cap_part = manifest.split("size cap")[1]
    assert "big.bin" in cap_part and "bad.bin" not in cap_part


def test_build_zip_manifest_discloses_forge_truncation(tmp_path):
    """CAKE-68: when the forge truncated the changed-file list, MANIFEST
    must say additional paths are unknown and point at the forge PR — never
    invent dropped filenames."""
    import io
    import zipfile
    from devcake.domain.orchestrator.deliver import _build_zip
    from devcake.ports.forge import PRFile

    class F:
        async def file_content(self, path, ref):
            return b"ok"

    files = [PRFile(path="kept.py", status="added")]
    data, omitted = run_coro(_build_zip(
        F(), files, "main", cap=10 * 1024 * 1024,
        truncated=True, pr_url="https://gitlab.example/o/r/-/merge_requests/9"))
    assert omitted == []  # no named path omissions — withheld names unknown
    z = zipfile.ZipFile(io.BytesIO(data))
    assert "kept.py" in z.namelist()
    manifest = z.read("MANIFEST.txt").decode()
    assert "forge truncated the changed-file list" in manifest
    assert "additional paths unknown" in manifest
    assert "https://gitlab.example/o/r/-/merge_requests/9" in manifest
    assert "could not be fetched" not in manifest
    assert "size cap" not in manifest


def test_deliver_feed_note_discloses_truncated_file_list(tmp_path):
    """Truncation is a real omission for feed accounting: the note must not
    treat len(files) as a complete change set, and the zip MANIFEST must
    carry the truncation arm."""
    import io
    import zipfile
    from devcake.ports.forge import PRFile, PRFilesResult, PullRequest

    uploaded = {}
    feed = []

    class FakePMO:
        async def upload_attachment(self, pmo_id, name, data):
            uploaded[name] = data
            return f"https://pmo/{name}"

        def capabilities(self):
            from devcake.ports.pmo import PMOCapabilities
            return PMOCapabilities(attachment_max_bytes=10 * 1024 * 1024,
                                   relations_supported=True)

    class FakeForge:
        async def pr_state(self, n):
            return PullRequest(
                number=n, url="https://gitlab.example/o/r/-/merge_requests/3",
                state="closed", merged=True, merge_commit_sha="deadbeef")

        async def pr_files(self, n):
            return PRFilesResult(
                files=[PRFile(path="only_known.py", status="added")],
                truncated=True)

        async def file_content(self, path, ref):
            return b"body"

    class RT:
        internal = {"linear-t-1"}

        def get(self, name):
            return FakeForge()

    from fakes import make_mission_manager
    from devcake.adapters.files.run_store import RunStore
    mgr = make_mission_manager(
        pmo=FakePMO(),
        forge_runtime=RT(),
        runs=type("Runs", (), {"store": RunStore(tmp_path / "runs")})(),
    )

    async def _feed(pmo_id, kind, md):
        feed.append(md)

    mgr._feed = _feed
    mgr._attachment_cap = lambda: 10 * 1024 * 1024

    run = _run("linear-t-1")
    run.mission_pmo_id = "p1"
    run.mission_key = "T-TRUNC"
    run.finalized_steps = []
    pr = PullRequest(number=3, url="https://gitlab.example/o/r/-/merge_requests/3",
                     state="closed", merged=True)
    run_coro(mgr.deliver_internal_zip(run, pr))

    assert feed, "expected a deliverable feed note"
    note = feed[0]
    assert "truncated the changed-file list" in note
    assert "additional paths unknown" in note
    assert "https://gitlab.example/o/r/-/merge_requests/3" in note
    z = zipfile.ZipFile(io.BytesIO(uploaded["T-TRUNC-deliverable.zip"]))
    assert "MANIFEST.txt" in z.namelist()
    assert "forge truncated the changed-file list" in z.read("MANIFEST.txt").decode()


def test_internal_zip_delivery(tmp_path, monkeypatch):
    """M11 zip delivery: an internal-forge merge packages the changed files
    and attaches them to the PMO feed; failure never un-Dones the mission."""
    import zipfile, io
    from devcake.domain.orchestrator import MissionManager
    from devcake.ports.forge import PRFile, PRFilesResult, PullRequest

    uploaded = {}
    feed = []

    class FakePMO:
        async def upload_attachment(self, pmo_id, name, data):
            uploaded[name] = data
            return f"https://pmo/{name}"
        def capabilities(self):
            from devcake.ports.pmo import PMOCapabilities
            return PMOCapabilities(attachment_max_bytes=10*1024*1024,
                                   relations_supported=True)

    class FakeForge:
        async def pr_state(self, n):
            return PullRequest(number=n, url="http://gitea/pr/1",
                               state="closed", merged=True,
                               merge_commit_sha="abc123")
        async def pr_files(self, n):
            return PRFilesResult(files=[PRFile(path="report/REPORT.md", status="added"),
                    PRFile(path="report/data.bin", status="added"),
                    PRFile(path="gone.txt", status="removed")])
        async def file_content(self, path, ref):
            return {"report/REPORT.md": b"# report",
                    "report/data.bin": b"\x00\x01\x02"}[path]

    class RT:
        internal = {"linear-t-1"}
        def get(self, name): return FakeForge()

    from fakes import make_mission_manager
    from devcake.adapters.files.run_store import RunStore
    mgr = make_mission_manager(
        pmo=FakePMO(),
        forge_runtime=RT(),
        runs=type("Runs", (), {"store": RunStore(tmp_path / "runs")})(),
    )

    async def _feed(pmo_id, kind, md): feed.append(md)
    mgr._feed = _feed
    mgr._attachment_cap = lambda: 10*1024*1024

    run = _run("linear-t-1"); run.mission_pmo_id = "p1"; run.mission_key = "T-1"
    run.finalized_steps = []
    pr = PullRequest(number=1, url="http://gitea/pr/1", state="closed", merged=True)
    run_coro(mgr.deliver_internal_zip(run, pr))

    assert "T-1-deliverable.zip" in uploaded
    z = zipfile.ZipFile(io.BytesIO(uploaded["T-1-deliverable.zip"]))
    names = set(z.namelist())
    assert "report/REPORT.md" in names and "report/data.bin" in names
    assert "gone.txt" not in names                 # removed files excluded
    assert z.read("report/data.bin") == b"\x00\x01\x02"   # binary intact
    assert any(DELIVERABLE_MARKER in f for f in feed)
    assert "deliver:zip" in run.finalized_steps    # idempotency recorded
    # idempotent: a second call does nothing
    uploaded.clear()
    run_coro(mgr.deliver_internal_zip(run, pr))
    assert not uploaded


def _mission_delivery_setup(tmp_path, feed_body):
    """deliver_internal_zip_for_mission scaffolding: internal repo, one feed
    entry with the given body, capture of uploads."""
    from devcake.domain.model import Activity, ActivityEntry
    from devcake.ports.forge import PRFile, PRFilesResult, PullRequest
    from fakes import make_mission_manager
    from devcake.adapters.files.run_store import RunStore

    uploaded = {}
    m = _m(key="T-1")
    m.repo = "linear-t-1"

    class FakePMO:
        async def get_activity(self, ref, full=False):
            return Activity(mission=m, entries=[
                ActivityEntry(ts=datetime.now(timezone.utc), author="a",
                              kind="comment", body=feed_body)])
        async def upload_attachment(self, pmo_id, name, data):
            uploaded[name] = data
            return f"https://pmo/{name}"

    class FakeForge:
        async def pr_state(self, n):
            return PullRequest(number=n, url="http://gitea/pr/1",
                               state="closed", merged=True,
                               merge_commit_sha="abc123")
        async def pr_files(self, n):
            return PRFilesResult(files=[PRFile(path="a.txt", status="added")])
        async def file_content(self, path, ref):
            return b"data"

    class RT:
        internal = {"linear-t-1"}
        def get(self, name): return FakeForge()

    mgr = make_mission_manager(
        pmo=FakePMO(), forge_runtime=RT(),
        runs=type("Runs", (), {"store": RunStore(tmp_path / "runs")})(),
    )
    feed = []
    async def _feed(pmo_id, kind, md): feed.append(md)
    mgr._feed = _feed
    mgr._attachment_cap = lambda: 10 * 1024 * 1024
    pr = PullRequest(number=1, url="http://gitea/pr/1", state="closed",
                     merged=True)
    return mgr, uploaded, feed, m, pr


def test_mission_zip_delivery_ignores_quoted_deliverable_marker(tmp_path):
    # ADR-0014 D2: a `>`-quoted mention of the zip name (a human quoting the
    # delivery comment, or a blockquoted last message) must not suppress a
    # real delivery
    mgr, uploaded, feed, m, pr = _mission_delivery_setup(
        tmp_path, "> attaching `T-1-deliverable.zip` next\n`devcake:v1`")
    run_coro(mgr.deliver_internal_zip_for_mission(m, pr))
    assert "T-1-deliverable.zip" in uploaded
    assert any(DELIVERABLE_MARKER in f for f in feed)


def test_mission_zip_delivery_skips_on_unquoted_marker(tmp_path):
    # the guard itself (review finding #9), previously untested: an unquoted
    # mention of the deliverable in the feed = already delivered, do nothing
    mgr, uploaded, feed, m, pr = _mission_delivery_setup(
        tmp_path, "📦 Deliverable attached: [T-1-deliverable.zip](https://x)")
    run_coro(mgr.deliver_internal_zip_for_mission(m, pr))
    assert not uploaded


def test_attach_merged_changeset_default_off():
    from devcake.config import AppConfig
    assert AppConfig().attach_merged_changeset_to_pmo is False


def test_external_zip_delivery_gated_by_toggle(tmp_path):
    """Configured (non-internal) work repos only zip when the operator
    enables attach_merged_changeset_to_pmo; internal always zips."""
    from devcake.ports.forge import PRFile, PRFilesResult, PullRequest
    from fakes import make_mission_manager
    from devcake.adapters.files.run_store import RunStore
    from devcake.config import AppConfig

    uploaded = {}
    feed = []

    class FakePMO:
        async def upload_attachment(self, pmo_id, name, data):
            uploaded[name] = data
            return f"https://pmo/{name}"
        def capabilities(self):
            from devcake.ports.pmo import PMOCapabilities
            return PMOCapabilities(attachment_max_bytes=10 * 1024 * 1024,
                                   relations_supported=True)

    class FakeForge:
        async def pr_state(self, n):
            return PullRequest(number=n, url="http://gh/pr/1",
                               state="closed", merged=True,
                               merge_commit_sha="deadbeef")
        async def pr_files(self, n):
            return PRFilesResult(files=[PRFile(path="src/a.py", status="added")])
        async def file_content(self, path, ref):
            return b"print(1)\n"

    class RT:
        internal = set()          # external — not in forges.internal
        def get(self, name): return FakeForge()

    cfg = AppConfig()
    mgr = make_mission_manager(
        pmo=FakePMO(), forge_runtime=RT(),
        runs=type("Runs", (), {"store": RunStore(tmp_path / "runs")})(),
        config=cfg,
    )
    async def _feed(pmo_id, kind, md): feed.append(md)
    mgr._feed = _feed
    mgr._attachment_cap = lambda: 10 * 1024 * 1024

    run = _run("alpha"); run.mission_pmo_id = "p1"; run.mission_key = "T-1"
    run.finalized_steps = []
    pr = PullRequest(number=1, url="http://gh/pr/1", state="closed",
                     merged=True)

    # default OFF → no zip for external
    run_coro(mgr.deliver_internal_zip(run, pr))
    assert not uploaded
    assert "deliver:zip" not in run.finalized_steps

    # toggle ON → zip
    cfg.attach_merged_changeset_to_pmo = True
    run_coro(mgr.deliver_internal_zip(run, pr))
    assert "T-1-deliverable.zip" in uploaded
    assert "deliver:zip" in run.finalized_steps
    assert any(DELIVERABLE_MARKER in f for f in feed)


def test_external_mission_zip_respects_toggle(tmp_path):
    """Merge-sweep path for a non-internal repo: toggle off skips, on delivers."""
    from devcake.domain.model import Activity, ActivityEntry
    from devcake.ports.forge import PRFile, PRFilesResult, PullRequest
    from fakes import make_mission_manager
    from devcake.adapters.files.run_store import RunStore
    from devcake.config import AppConfig
    from datetime import datetime, timezone

    uploaded = {}
    m = _m(key="T-1")
    m.repo = "alpha"

    class FakePMO:
        async def get_activity(self, ref, full=False):
            return Activity(mission=m, entries=[
                ActivityEntry(ts=datetime.now(timezone.utc), author="a",
                              kind="comment", body="no zip yet")])
        async def upload_attachment(self, pmo_id, name, data):
            uploaded[name] = data
            return f"https://pmo/{name}"

    class FakeForge:
        async def pr_state(self, n):
            return PullRequest(number=n, url="http://gh/pr/1",
                               state="closed", merged=True,
                               merge_commit_sha="abc")
        async def pr_files(self, n):
            return PRFilesResult(files=[PRFile(path="a.txt", status="added")])
        async def file_content(self, path, ref):
            return b"data"

    class RT:
        internal = set()
        def get(self, name): return FakeForge()

    cfg = AppConfig()
    mgr = make_mission_manager(
        pmo=FakePMO(), forge_runtime=RT(),
        runs=type("Runs", (), {"store": RunStore(tmp_path / "runs")})(),
        config=cfg,
    )
    feed = []
    async def _feed(pmo_id, kind, md): feed.append(md)
    mgr._feed = _feed
    mgr._attachment_cap = lambda: 10 * 1024 * 1024
    pr = PullRequest(number=1, url="http://gh/pr/1", state="closed",
                     merged=True)

    run_coro(mgr.deliver_internal_zip_for_mission(m, pr))
    assert not uploaded

    cfg.attach_merged_changeset_to_pmo = True
    run_coro(mgr.deliver_internal_zip_for_mission(m, pr))
    assert "T-1-deliverable.zip" in uploaded


def test_deliver_zip_ref_from_pr_state_merge_commit_sha(tmp_path):
    """CAKE-77: deliver pins the zip to PullRequest.merge_commit_sha from
    pr_state — never via adapter-private forge._req."""
    from devcake.ports.forge import PRFile, PRFilesResult, PullRequest
    from fakes import make_mission_manager
    from devcake.adapters.files.run_store import RunStore

    uploaded = {}
    seen_refs: list[str] = []
    req_calls: list[tuple[str, str]] = []

    class FakePMO:
        async def upload_attachment(self, pmo_id, name, data):
            uploaded[name] = data
            return f"https://pmo/{name}"

        def capabilities(self):
            from devcake.ports.pmo import PMOCapabilities
            return PMOCapabilities(attachment_max_bytes=10 * 1024 * 1024,
                                   relations_supported=True)

    class FakeForge:
        async def pr_state(self, n):
            return PullRequest(number=n, url="http://gitea/pr/1",
                               state="closed", merged=True,
                               merge_commit_sha="deadbeef")

        async def pr_files(self, n):
            return PRFilesResult(files=[PRFile(path="a.txt", status="added")])

        async def file_content(self, path, ref):
            seen_refs.append(ref)
            return b"data"

        async def _req(self, method, path):
            # Reach-through detector: recording beats raising — a swallowed
            # exception would still yield the "main" fallback and hide the leak.
            req_calls.append((method, path))
            return {"merge_commit_sha": "should-not-be-used"}

    class RT:
        internal = {"linear-t-1"}

        def get(self, name):
            return FakeForge()

    mgr = make_mission_manager(
        pmo=FakePMO(), forge_runtime=RT(),
        runs=type("Runs", (), {"store": RunStore(tmp_path / "runs")})(),
    )
    feed = []

    async def _feed(pmo_id, kind, md):
        feed.append(md)

    mgr._feed = _feed
    mgr._attachment_cap = lambda: 10 * 1024 * 1024

    run = _run("linear-t-1")
    run.mission_pmo_id = "p1"
    run.mission_key = "T-1"
    run.finalized_steps = []
    pr = PullRequest(number=1, url="http://gitea/pr/1",
                     state="closed", merged=True)
    run_coro(mgr.deliver_internal_zip(run, pr))

    assert "T-1-deliverable.zip" in uploaded
    assert seen_refs == ["deadbeef"]
    assert req_calls == []


def test_deliver_zip_ref_falls_back_to_main_without_req(tmp_path):
    """CAKE-77: when neither pr_state nor the inbound PR carries
    merge_commit_sha, the zip ref is the default branch name — still with
    no forge._req reach-through."""
    from devcake.ports.forge import PRFile, PRFilesResult, PullRequest
    from fakes import make_mission_manager
    from devcake.adapters.files.run_store import RunStore

    uploaded = {}
    seen_refs: list[str] = []
    req_calls: list[tuple[str, str]] = []

    class FakePMO:
        async def upload_attachment(self, pmo_id, name, data):
            uploaded[name] = data
            return f"https://pmo/{name}"

        def capabilities(self):
            from devcake.ports.pmo import PMOCapabilities
            return PMOCapabilities(attachment_max_bytes=10 * 1024 * 1024,
                                   relations_supported=True)

    class FakeForge:
        async def pr_state(self, n):
            return PullRequest(number=n, url="http://gitea/pr/1",
                               state="closed", merged=True,
                               merge_commit_sha=None)

        async def pr_files(self, n):
            return PRFilesResult(files=[PRFile(path="a.txt", status="added")])

        async def file_content(self, path, ref):
            seen_refs.append(ref)
            return b"data"

        async def _req(self, method, path):
            req_calls.append((method, path))
            return {"merge_commit_sha": "should-not-be-used"}

    class RT:
        internal = {"linear-t-1"}

        def get(self, name):
            return FakeForge()

    mgr = make_mission_manager(
        pmo=FakePMO(), forge_runtime=RT(),
        runs=type("Runs", (), {"store": RunStore(tmp_path / "runs")})(),
    )
    feed = []

    async def _feed(pmo_id, kind, md):
        feed.append(md)

    mgr._feed = _feed
    mgr._attachment_cap = lambda: 10 * 1024 * 1024

    run = _run("linear-t-1")
    run.mission_pmo_id = "p1"
    run.mission_key = "T-1"
    run.finalized_steps = []
    pr = PullRequest(number=1, url="http://gitea/pr/1",
                     state="closed", merged=True)
    run_coro(mgr.deliver_internal_zip(run, pr))

    assert "T-1-deliverable.zip" in uploaded
    assert seen_refs == ["main"]
    assert req_calls == []


def test_onboard_runspec_carries_extra_repo_read_tokens(tmp_path, monkeypatch):
    """Item 2 full scope: an ONBOARD run of a multi-repo instance gets every
    OTHER set repo as {name, url, clone_user, token} with the READ token
    preferred — EXECUTE (and every non-ONBOARD stage) never gets extras."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from test_transitions import make_mgr, mission
    from devcake import secrets as s
    from devcake.adapters.registry import make_forge
    from devcake.config import PMOInstance, RepoInstance
    from devcake.domain.forge_runtime import ForgeRuntime

    s.write_connection_secret("repo", "alpha", "token", "alpha-write-token")
    s.write_connection_secret("repo", "beta", "token", "beta-write-token")
    s.write_connection_secret("repo", "beta", "token_ro", "beta-ro-token")
    rt = ForgeRuntime()
    rt.rebuild([RepoInstance(name="alpha", url="https://github.com/o/a"),
                RepoInstance(name="beta", forge="gitlab",
                             url="https://gitlab.com/o/b")], make_forge)

    m = mission()
    mgr, _fake, _store = make_mgr(tmp_path, m)
    mgr.forges = rt
    mgr.internal_forge = None
    mgr.instance = PMOInstance(name="linear", team_key="DEV",
                               repos=["alpha", "beta"])

    onboard = Run(run_id="LINEAR-T-1-1-ONBOARD-AAAAAA", mission_key="T-1",
                  mission_type="ONBOARD", dev_type="senior-dev", seq=1,
                  repo_ref="alpha", pmo_ref="linear", state="dispatched")
    payload = mgr.runspec_secret_payload(onboard)
    assert payload["env"]["DEVCAKE_FORGE_TOKEN"] == "alpha-write-token"
    assert payload["extra_repos"] == [
        {"name": "beta", "url": "https://gitlab.com/o/b",
         "clone_user": "oauth2", "token": "beta-ro-token"}]

    execute = Run(run_id="LINEAR-T-1-2-EXECUTE-BBBBBB", mission_key="T-1",
                  mission_type="EXECUTE", dev_type="senior-dev", seq=2,
                  repo_ref="alpha", pmo_ref="linear", state="dispatched")
    assert "extra_repos" not in mgr.runspec_secret_payload(execute)

    # the prompt-side counterpart: multi-repo instances get the section
    # (primary listed first), single-repo instances get nothing
    txt = dispatch._onboard_repo_options(mgr, "beta")
    assert "`beta`" in txt and "`alpha`" in txt
    assert txt.index("`beta`") < txt.index("`alpha`")
    assert "devcake-repo:" in txt and "blocked_by" in txt
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["alpha"])
    assert dispatch._onboard_repo_options(mgr, "alpha") == ""


def test_reference_repos_all_stages_and_never_work_targets(tmp_path, monkeypatch):
    """Founder request 2026-07-15: reference repos (plural) are cloned
    read-only for EVERY stage, are deduped against ONBOARD's triage
    siblings, and can never be routing targets."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from test_transitions import make_mgr, mission
    from devcake import secrets as s
    from devcake.adapters.registry import make_forge
    from devcake.config import PMOInstance, RepoInstance
    from devcake.domain.forge_runtime import ForgeRuntime

    for name in ("alpha", "docs", "guides"):
        s.write_connection_secret("repo", name, "token_ro", f"{name}-ro-token1")
        s.write_connection_secret("repo", name, "token", f"{name}-write-tok1")
    rt = ForgeRuntime()
    rt.rebuild([RepoInstance(name="alpha", url="https://github.com/o/a"),
                RepoInstance(name="docs", forge="gitlab",
                             url="https://gitlab.com/o/docs"),
                RepoInstance(name="guides", url="https://github.com/o/guides")],
               make_forge)
    inst = PMOInstance(name="linear", team_key="DEV", repos=["alpha"],
                       reference_repos=["docs", "guides"])

    m = mission()
    mgr, _fake, _store = make_mgr(tmp_path, m)
    mgr.forges = rt
    mgr.internal_forge = None
    mgr.instance = inst

    # every stage gets BOTH reference repos with read tokens
    for mt in ("PLAN", "EXECUTE", "REVIEW", "ONBOARD"):
        run = Run(run_id=f"LINEAR-T-1-1-{mt}-AAAAAA", mission_key="T-1",
                  mission_type=mt, dev_type="senior-dev", seq=1,
                  repo_ref="alpha", pmo_ref="linear", state="dispatched")
        payload = mgr.runspec_secret_payload(run)
        names = [x["name"] for x in payload["extra_repos"]]
        assert names == ["docs", "guides"], (mt, names)
        assert all(x["token"].endswith("-ro-token1")
                   for x in payload["extra_repos"])

    # STEWARD never gets extras
    steward_run = Run(run_id="LINEAR-TEAM-1-STEWARD-AAAAAA", mission_key="TEAM",
                     mission_type="STEWARD", dev_type="senior-dev", seq=1,
                     repo_ref="alpha", pmo_ref="linear", state="dispatched")
    assert "extra_repos" not in mgr.runspec_secret_payload(steward_run)

    # a marker naming a reference repo GATES — never a work target
    name, reason = resolve_repo(_m("`devcake-repo:docs`"), inst,
                                {"alpha", "docs", "guides"}, [])
    assert name is None and "REFERENCE" in reason

    # every stage's prompt names the reference clones
    note = dispatch._reference_repos_note(mgr, "alpha")
    assert "`docs`" in note and "`guides`" in note and "NEVER modify" in note
    from devcake.prompts import execute_prompt, plan_prompt
    out = execute_prompt("ID", m, "a", "pr {branch}", reference_repos=note)
    assert "Reference repositories (read-only)" in out
    assert "Reference repositories" in plan_prompt("ID", m, reference_repos=note)


def test_deliver_skips_when_attachments_unsupported(tmp_path):
    """No-attachment boards must not post a false packaging-failed notice."""
    mgr, uploaded, feed, m, pr = _mission_delivery_setup(
        tmp_path, "unrelated earlier comment")

    async def _boom(*a, **k):
        raise RuntimeError("github_issues: attachments are not supported")
    mgr.pmo.upload_attachment = _boom
    mgr.pmo.capabilities = lambda: type(
        "C", (), {"attachments_supported": False,
                  "attachment_max_bytes": 0})()
    run_coro(mgr.deliver_internal_zip_for_mission(m, pr))
    assert uploaded == {}
    assert not any("packaging failed" in (f or "") for f in feed)
    assert not any("DEVCAKE-DELIVERABLE" in (f or "") for f in feed)


def test_deliverable_note_is_marked_and_honest(tmp_path):
    """The feed note opens with DELIVERABLE_MARKER (downstream consumers
    classify on startswith) and says what the zip IS — the audit copy — so no
    reader, human or bot, mistakes it for the mission's answer."""
    mgr, uploaded, feed, m, pr = _mission_delivery_setup(
        tmp_path, "unrelated earlier comment")
    run_coro(mgr.deliver_internal_zip_for_mission(m, pr))
    note = next(f for f in feed if DELIVERABLE_MARKER in f)
    assert note.startswith(DELIVERABLE_MARKER)
    assert "T-1-deliverable.zip" in note
    assert "audit copy, not the answer" in note


def test_gitea_mission_repo_binding_row(monkeypatch):
    """2026-08 F9: the internal mission-repo row doctrine moved from domain
    into the gitea adapter — pin it at its new home. Always auto-merge (the
    zip deliverable only posts after merge; no human watches the internal
    Gitea), synthesized name via model_construct, app-side adapter on the
    service token."""
    from devcake.adapters.gitea.provision import GiteaProvisioner
    from devcake.ports.internal_forge import MissionRepoCredentials

    prov = GiteaProvisioner(url="http://gitea:3000", admin_user="a",
                            admin_password="p")
    monkeypatch.setattr(prov, "service_tokens",
                        lambda: {"app_token": "app-tok",
                                 "reviewer_token": "rev-tok"})
    creds = MissionRepoCredentials(
        repo_name="linear-t-9",
        clone_url="http://gitea:3000/devcake-internal/linear-t-9.git",
        username="svc-linear-t-9", token_write="w", token_read="r")
    inst, adapter = prov.mission_repo_binding(creds)
    assert inst.name == "linear-t-9"
    assert inst.url == creds.clone_url
    assert inst.auto_merge is True
    assert inst.auto_resolve_merge_conflicts is True
    assert inst.merge_retry_window_minutes == 30
    assert adapter is not None and hasattr(adapter, "merge")
