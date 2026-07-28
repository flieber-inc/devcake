"""ADR-0019: per-PMO assignment overrides — the same derived Mission Type
staffs the global Dev Type on one instance and the override on another, and
CLI args always follow the row that named the Dev Type (never mixed)."""
import asyncio
from types import SimpleNamespace

from devcake.config import Assignment, DevType, PMOInstance
from devcake.domain.model import MissionType

from test_dependencies import DepPMO, m


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _mgr(tmp_path, instance):
    from fakes import make_mission_manager
    mgr = make_mission_manager(
        tmp_path, pmo=DepPMO(),
        forge=SimpleNamespace(descriptor=SimpleNamespace(pr_noun="pull request")),
        instance=instance,
        dev_types={
            "judgment": DevType(name="judgment", harness_template="claude-code"),
            "implementer": DevType(name="implementer", harness_template="grok-build"),
        },
        noop_audit=True,
    )
    dispatched = []

    async def fake_dispatch(mission, mtype, dev_type):
        dispatched.append((mission.key, mtype.value, dev_type.name))
        return None

    mgr.dispatch = fake_dispatch
    return mgr, dispatched


def test_schedule_staffs_by_instance_override(tmp_path):
    """Dual-crew staffing: backlog derives to ONBOARD on both instances, but
    cs overrides ONBOARD to another Dev Type — eng keeps the global row."""
    eng, eng_dispatched = _mgr(
        tmp_path / "eng", PMOInstance(name="eng", team_key="ENG"))
    cs, cs_dispatched = _mgr(
        tmp_path / "cs", PMOInstance(
            name="cs", team_key="CS",
            assignments={"ONBOARD": Assignment(dev_type="implementer")}))
    run_coro(eng.schedule([m("p1", "T-1")]))
    run_coro(cs.schedule([m("p1", "T-1")]))
    assert eng_dispatched == [("T-1", "ONBOARD", "judgment")]
    assert cs_dispatched == [("T-1", "ONBOARD", "implementer")]


def test_schedule_skips_when_override_names_missing_dev_type(tmp_path):
    """An override naming a Dev Type that no longer exists behaves exactly
    like an unassigned global row: skip, never crash (docs/15 fail-safe) —
    and the WHY is surfaced in blocked_reasons (M10 repo-gate precedent;
    overrides skip PUT-time existence checks, so this state is reachable
    via a raw config PUT)."""
    cs, dispatched = _mgr(
        tmp_path, PMOInstance(
            name="cs", team_key="CS",
            assignments={"ONBOARD": Assignment(dev_type="vanished")}))
    run_coro(cs.schedule([m("p1", "T-1")]))
    assert dispatched == []
    assert "vanished" in cs.blocked_reasons["p1"]
    assert "ONBOARD" in cs.blocked_reasons["p1"]


def test_schedule_surfaces_unassigned_mission_type(tmp_path):
    """The pre-existing global path gains the same visibility: an empty
    global row (hand-edited config) names the unassigned type instead of
    skipping silently."""
    cs, dispatched = _mgr(tmp_path, PMOInstance(name="cs", team_key="CS"))
    cs.config.assignments["ONBOARD"] = Assignment(dev_type="")
    run_coro(cs.schedule([m("p1", "T-1")]))
    assert dispatched == []
    assert "unassigned" in cs.blocked_reasons["p1"]


def test_dispatch_spec_env_uses_override_args_wholesale(tmp_path):
    """A REAL dispatch() on an overriding instance carries the OVERRIDE row's
    CLI args into the runspec — never the global row's (flags are harness-
    specific; mixing them across rows is the mismatch the UI warns about)."""
    from test_prompt_templates import _ForgeWithDescriptor
    from test_transitions import make_mgr, mission

    m_ = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, _fake, _store = make_mgr(tmp_path, m_, forge=_ForgeWithDescriptor())
    mgr.instance = PMOInstance(
        name="linear", team_key="DEV", repos=["main"],
        assignments={"EXECUTE": Assignment(dev_type="senior-dev",
                                           extra_cli_args="--cs-flag")})
    mgr.config.assignments["EXECUTE"].extra_cli_args = "--global-flag"
    launched = []

    async def launch(run, image):
        launched.append(run)
    mgr.runs.bootstrap = type("B", (), {"launch": staticmethod(launch)})()

    run = run_coro(mgr.dispatch(m_, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    assert run is not None and launched
    assert run.spec_env["DEVCAKE_EXTRA_ARGS"] == "--cs-flag"
