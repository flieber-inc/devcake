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


def test_malformed_marker_gates_instead_of_silent_default():
    """Audit A26: a `devcake-repo:`-shaped but unparseable marker (hyphens,
    >12 chars, bad leading char) previously fell through to the instance
    default — a typo'd routing intent silently landed on the wrong repo and
    stickiness then latched it there. Gate with a fix-the-marker reason."""
    for bad in ("`devcake-repo:my-repo`", "`devcake-repo:thirteenchars1`",
                "`devcake-repo:9lead`", "`devcake-repo:`"):
        name, reason = resolve_repo(_m(bad), INST_DEF, REPOS, [])
        assert name is None and "unparseable" in reason, bad
    # sticky missions gate too — the typo stays visible, nothing re-routes
    name, reason = resolve_repo(_m("`devcake-repo:my-repo`"), INST_DEF, REPOS,
                                [_run("beta")])
    assert name is None and "unparseable" in reason


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
