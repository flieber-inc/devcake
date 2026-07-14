"""Per-mission repo resolution (M10, docs/16 F3): the marker/default/zero
table, STICKINESS across routing edits (plan finding H3), and the
resolution-failure contract (vanished repos never crash or silently wedge)."""

import asyncio
from datetime import datetime, timezone

from devcake.config import PMOInstance
from devcake.domain.model import Mission
from devcake.domain.repo_routing import (REASON_ZERO_REPO, marker_repo,
                                         resolve_repo)
from devcake.domain.run import Run


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
INST_DEF = PMOInstance(name="linear", team_key="DEV", default_repo="alpha")
REPOS = {"alpha", "beta"}


def test_marker_parsing():
    assert marker_repo("body\n`devcake-repo:beta`\nrest") == "beta"
    assert marker_repo("`devcake-repo:BETA`") == "beta"     # case-insensitive
    assert marker_repo("devcake-repo:beta") is None         # backticks required
    assert marker_repo("") is None


def test_resolution_table_virgin_missions():
    # marker wins over the instance default
    assert resolve_repo(_m("`devcake-repo:beta`"), INST_DEF, REPOS, []) == ("beta", None)
    # unknown marker gates with the fix-the-marker reason
    name, reason = resolve_repo(_m("`devcake-repo:gone`"), INST_DEF, REPOS, [])
    assert name is None and "unknown repo 'gone'" in reason
    # no marker → instance default
    assert resolve_repo(_m(), INST_DEF, REPOS, []) == ("alpha", None)
    # no marker, no default → zero-repo gate (un-gated in M11)
    assert resolve_repo(_m(), INST, REPOS, []) == (None, REASON_ZERO_REPO)


def test_resolution_sticky_once_a_run_exists():
    """Plan finding H3: attempt 1's PR/branch live on the resolved repo —
    a marker/default edit must GATE, never silently re-route (duplicate PR)."""
    history = [_run("beta")]
    # sticky wins when no fresh signal disagrees (no marker, no default)
    assert resolve_repo(_m(), INST, REPOS, history) == ("beta", None)
    # ANY conflicting fresh signal gates — an edited/removed marker OR a
    # changed instance default both read as mid-mission re-routing
    name, reason = resolve_repo(_m("`devcake-repo:alpha`"), INST_DEF, REPOS, history)
    assert name is None and "sticky" in reason and "'beta'" in reason
    name, reason = resolve_repo(_m(), INST_DEF, REPOS, history)   # default=alpha
    assert name is None and "sticky" in reason
    # a matching marker is fine
    assert resolve_repo(_m("`devcake-repo:beta`"), INST_DEF, REPOS, history) == ("beta", None)
    # sticky repo vanished from config → gate, explicit restore-or-close reason
    name, reason = resolve_repo(_m(), INST_DEF, {"alpha"}, history)
    assert name is None and "no longer configured" in reason


def test_vanished_repo_contract_in_sweeps_and_review(tmp_path):
    """A DEVCAKE-MERGE-parked mission whose repo vanished must surface a
    visible reason (sweeps) / fail the run cleanly (review) — never crash."""
    from fakes import FakeForgeRuntime
    from devcake.domain.orchestrator import MissionManager

    mgr = MissionManager.__new__(MissionManager)
    mgr.instance_name = "linear"
    mgr.blocked_reasons = {}
    mgr.merge_handoffs = {}
    mgr._merge_window_closed = set()
    mgr.forges = FakeForgeRuntime(None)          # nothing resolves
    m = _m()
    m.repo, m.repo_reason = None, "repo 'beta' no longer configured"
    run_coro(mgr._merge_sweep(m))                # must not raise
    assert "no longer configured" in mgr.blocked_reasons["p1"]

    run = _run("gone")
    run.mission_pmo_id = "p1"
    run_coro(mgr._finalize_review(run, {"verdict": "approve"}))   # must not raise
    assert "no longer configured" in (run.verdict or "")


def test_zero_repo_mission_visible_but_gated(tmp_path):
    """Zero-repo missions derive fully but never dispatch; the reason is
    surfaced through blocked_reasons for /health and the missions API."""
    from fakes import FakeForgeRuntime
    from devcake.adapters.files.run_store import RunStore
    from devcake.config import AppConfig, DevType
    from devcake.domain.orchestrator import MissionManager

    mgr = MissionManager.__new__(MissionManager)
    mgr.instance_name = "linear"
    mgr.instance = INST
    mgr.config = AppConfig()
    mgr.dev_types = {"senior-dev": DevType(name="senior-dev",
                                           harness_template="claude-code")}
    mgr.pmo = None
    mgr.forges = FakeForgeRuntime(None)
    mgr.breakers, mgr.blocked_reasons, mgr.cycles = {}, {}, []
    mgr._grace, mgr._grace_next = set(), set()

    class Runs:
        pass
    mgr.runs = Runs()
    mgr.runs.store = RunStore(tmp_path / "runs")

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
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_github_token_000000000000")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-gitlab_token_000000000")
    from devcake.adapters.registry import make_forge
    from devcake.config import AppConfig, DevType, RepoInstance
    from devcake.domain.forge_runtime import ForgeRuntime
    from devcake.domain.orchestrator import MissionManager

    gh = RepoInstance(name="ghrepo", forge="github", url="https://github.com/o/r")
    gl = RepoInstance(name="glrepo", forge="gitlab", url="https://gitlab.com/g/p")
    rt = ForgeRuntime()
    rt.rebuild([gh, gl], make_forge)
    assert set(rt.forges) == {"ghrepo", "glrepo"}

    mgr = MissionManager.__new__(MissionManager)
    mgr.config = AppConfig()
    mgr.forges = rt
    mgr.dev_types = {"senior-dev": DevType(name="senior-dev",
                                           harness_template="claude-code")}
    dt = mgr.dev_types["senior-dev"]

    env_gh = mgr._protocol_spec_env(
        mission_id="p1", mission_key="T-1", mission_type="EXECUTE",
        dev_type=dt, seq=1, extra_args="", repo=gh, forge=rt.get("ghrepo"))
    env_gl = mgr._protocol_spec_env(
        mission_id="p2", mission_key="T-2", mission_type="EXECUTE",
        dev_type=dt, seq=1, extra_args="", repo=gl, forge=rt.get("glrepo"))
    assert env_gh["DEVCAKE_REPO_URL"] == gh.url and env_gl["DEVCAKE_REPO_URL"] == gl.url
    assert env_gh["DEVCAKE_CLONE_USER"] == "x-access-token"     # github dialect
    assert env_gl["DEVCAKE_CLONE_USER"] == "oauth2"             # gitlab dialect

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
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
    """_resolve_repo's history filter: this mission's runs only, MAPPER runs
    excluded, foreign-instance records excluded, newest first."""
    from fakes import FakeForgeRuntime
    from devcake.adapters.files.run_store import RunStore
    from devcake.domain.orchestrator import MissionManager

    store = RunStore(tmp_path / "runs")
    mine_old = _run("alpha"); mine_old.mission_pmo_id = "p1"; mine_old.pmo_ref = "linear"
    mine_new = _run("beta", seq=2); mine_new.mission_pmo_id = "p1"; mine_new.pmo_ref = "linear"
    mapper = _run("alpha", seq=3); mapper.mission_pmo_id = "p1"
    mapper.pmo_ref, mapper.mission_type = "linear", "MAPPER"
    other_mission = _run("alpha", seq=4); other_mission.mission_pmo_id = "p9"
    other_instance = _run("alpha", seq=5); other_instance.mission_pmo_id = "p1"
    other_instance.pmo_ref = "linearb"
    from datetime import timedelta
    from devcake.domain.run import utcnow
    mine_old.created_at = utcnow() - timedelta(hours=2)
    mine_new.created_at = utcnow() - timedelta(hours=1)
    for r in (mine_old, mine_new, mapper, other_mission, other_instance):
        store.save(r)

    mgr = MissionManager.__new__(MissionManager)
    mgr.instance_name = "linear"
    mgr.instance = INST

    class Runs:
        pass
    mgr.runs = Runs()
    mgr.runs.store = store
    mgr.forges = FakeForgeRuntime(object())   # instances: {"main"} — irrelevant
    mgr.forges._inst.name = "main"
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
        name, reason = mgr._resolve_repo(_m())
    finally:
        rr.resolve_repo = orig
    # newest-first, only THIS mission's non-mapper, same-instance records
    assert got[0] == [mine_new.run_id, mine_old.run_id]
    assert name == "beta" and reason is None      # sticky = newest repo_ref


def test_internal_repo_naming_and_port_helper():
    """The internal-repo naming convention lives on the PORT (domain may
    derive it to detect prior internal routing across restarts) — not in the
    adapter (F1 import boundary)."""
    from devcake.ports.internal_forge import internal_repo_name
    assert internal_repo_name("linear", "DEV-17") == "linear-dev-17"
    assert internal_repo_name("linteama", "PRJ-Big Report!") == "linteama-prj-big-report"
    # bounded for the run-id / repo-name budget
    assert len(internal_repo_name("linear", "X" * 80)) <= 60


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

    mgr = MissionManager.__new__(MissionManager)
    mgr.instance_name = "linear"
    mgr.instance = INST                                    # no default_repo
    rt = RT(None)
    rt._insts = {}
    mgr.forges = rt
    mgr.internal_forge = FakeInternal()

    class Runs:
        pass
    mgr.runs = Runs()
    mgr.runs.store = RunStore(tmp_path / "runs")

    # zero-repo mission → internal
    name, reason = run_coro(mgr.resolve_repo_live(_m()))
    assert name == "linear-t-1" and reason is None
    assert provisioned == [("linear", "T-1")]
    assert "linear-t-1" in rt.internal

    # unknown marker → GATED, never redirected internal
    m2 = _m(description="`devcake-repo:nope`")
    name2, reason2 = run_coro(mgr.resolve_repo_live(m2))
    assert name2 is None and "unknown repo 'nope'" in reason2


def test_internal_zip_delivery(tmp_path, monkeypatch):
    """M11 zip delivery: an internal-forge merge packages the changed files
    and attaches them to the PMO feed; failure never un-Dones the mission."""
    import zipfile, io
    from devcake.domain.orchestrator import MissionManager
    from devcake.ports.forge import PRFile, PullRequest

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
            return PullRequest(number=n, url="http://gitea/pr/1", state="closed", merged=True)
        async def pr_files(self, n):
            return [PRFile(path="report/REPORT.md", status="added"),
                    PRFile(path="report/data.bin", status="added"),
                    PRFile(path="gone.txt", status="removed")]
        async def file_content(self, path, ref):
            return {"report/REPORT.md": b"# report",
                    "report/data.bin": b"\x00\x01\x02"}[path]
        async def _req(self, method, path):
            return {"merge_commit_sha": "abc123"}

    class RT:
        internal = {"linear-t-1"}
        def get(self, name): return FakeForge()

    mgr = MissionManager.__new__(MissionManager)
    mgr.forges = RT()
    mgr.pmo = FakePMO()

    async def _feed(pmo_id, kind, md): feed.append(md)
    mgr._feed = _feed
    from devcake.domain.orchestrator import deliver
    mgr._attachment_cap = lambda: 10*1024*1024

    class Runs: pass
    mgr.runs = Runs()
    from devcake.adapters.files.run_store import RunStore
    mgr.runs.store = RunStore(tmp_path / "runs")

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
    assert any("Deliverable attached" in f for f in feed)
    assert "deliver:zip" in run.finalized_steps    # idempotency recorded
    # idempotent: a second call does nothing
    uploaded.clear()
    run_coro(mgr.deliver_internal_zip(run, pr))
    assert not uploaded
