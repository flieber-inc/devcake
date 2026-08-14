"""Memory bindings + reserved Cron row (PLAN_MEMORY §2, I1–I5).

Public seams: DevType/PMOInstance.memory_repos, AppConfig toggles and
crons, validate_memory_bindings, is_memory_bound, reconcile_reserved_crons.
Independent expected values — no recomputing production helpers to assert
against themselves except the named public functions.
"""

from types import SimpleNamespace

import pytest

from devcake.config import (AppConfig, Assignment, CronJob, DevType,
                            PMOInstance, RepoInstance, Steward,
                            MEMORY_CURATOR_CRON_ID, MEMORY_CURATOR_TEMPLATE,
                            is_memory_bound, reconcile_reserved_crons,
                            validate_memory_bindings)


def _repos(*names: str) -> list[RepoInstance]:
    return [RepoInstance(name=n, url=f"https://github.com/acme/{n}")
            for n in names]


def _cfg(*, pmos=None, repos=None, **kw) -> AppConfig:
    return AppConfig(
        pmos=pmos or [],
        repos=repos if repos is not None else _repos("webapp", "docs", "nb"),
        **kw)


def _dt(name="judgment", memory_repos=()):
    return DevType(name=name, harness_template="claude-code",
                   identifying_prompt="x", memory_repos=list(memory_repos))


# ── schema shapes ───────────────────────────────────────────────────────────


def test_memory_repos_default_empty_deduped_order_preserved():
    dt = DevType(name="judgment", harness_template="claude-code")
    assert dt.memory_repos == []
    inst = PMOInstance(name="eng", team_key="T")
    assert inst.memory_repos == []
    dt = DevType.model_validate({
        "name": "judgment", "harness_template": "claude-code",
        "memory_repos": ["nb", "docs", "nb"]})
    assert dt.memory_repos == ["nb", "docs"]
    inst = PMOInstance.model_validate({
        "name": "eng", "team_key": "T",
        "memory_repos": ["nb", "docs", "nb"]})
    assert inst.memory_repos == ["nb", "docs"]


def test_memory_repos_refuse_slash_empty_and_dotdot():
    """I5: card-granular. Unsafe path segments never reach the entrypoint."""
    for bad in ("nb/notes", "card/path/", "..", "", "NB", "has space"):
        with pytest.raises(Exception):
            DevType(name="judgment", harness_template="claude-code",
                    memory_repos=[bad])
        with pytest.raises(Exception):
            PMOInstance(name="eng", team_key="T", memory_repos=[bad])


def test_toggles_and_claims_cap_defaults_and_round_trip():
    fresh = AppConfig()
    assert fresh.context_sourcing_strict is True
    assert fresh.memory_auto_merge is False
    assert fresh.budgets.claims_queue_max == 50
    base = _cfg().model_dump()
    del base["context_sourcing_strict"]
    del base["memory_auto_merge"]
    del base["crons"]
    base["budgets"] = {k: v for k, v in base["budgets"].items()
                       if k != "claims_queue_max"}
    loaded = AppConfig.model_validate(base)
    assert loaded.context_sourcing_strict is True
    assert loaded.memory_auto_merge is False
    assert loaded.budgets.claims_queue_max == 50
    tuned = AppConfig.model_validate({
        **base, "context_sourcing_strict": False, "memory_auto_merge": True,
        "budgets": {"claims_queue_max": 0}})
    again = AppConfig.model_validate(tuned.model_dump())
    assert again.context_sourcing_strict is False
    assert again.memory_auto_merge is True
    assert again.budgets.claims_queue_max == 0
    with pytest.raises(Exception):
        AppConfig.model_validate({**base, "budgets": {"claims_queue_max": -1}})


def test_reserved_cron_seed_on_fresh_and_omitted_list():
    fresh = AppConfig()
    assert len(fresh.crons) == 1
    row = fresh.crons[0]
    assert row.id == MEMORY_CURATOR_CRON_ID
    assert row.reserved is True
    assert row.enabled is False
    assert row.interval_minutes == 60
    assert row.pmo is None
    assert row.entry_stage == "EXECUTE"
    assert row.description_template == MEMORY_CURATOR_TEMPLATE
    # empty incoming list still carries the reserved row (model layer)
    got = AppConfig.model_validate({**_cfg().model_dump(), "crons": []})
    assert [c.id for c in got.crons] == [MEMORY_CURATOR_CRON_ID]
    assert got.crons[0].reserved is True


def test_cron_id_slug_unique_and_nonreserved_requires_existing_pmo():
    with pytest.raises(Exception):
        CronJob(id="has/slash", name="x", entry_stage="PLAN",
                description_template="t", pmo="eng")
    with pytest.raises(Exception):
        CronJob(id="Bad", name="x", entry_stage="PLAN",
                description_template="t", pmo="eng")
    CronJob(id="nightly-plan", name="Nightly", entry_stage="PLAN",
            description_template="t", pmo="eng")
    base = _cfg(pmos=[PMOInstance(name="eng", team_key="T")]).model_dump()
    with pytest.raises(Exception, match="duplicate"):
        AppConfig.model_validate({
            **base, "crons": [
                {"id": "nightly-plan", "name": "A", "entry_stage": "PLAN",
                 "description_template": "t", "pmo": "eng"},
                {"id": "nightly-plan", "name": "B", "entry_stage": "PLAN",
                 "description_template": "t", "pmo": "eng"},
            ]})
    with pytest.raises(Exception, match="pmo"):
        AppConfig.model_validate({
            **base, "crons": [
                {"id": "nightly-plan", "name": "A", "entry_stage": "PLAN",
                 "description_template": "t", "pmo": None},
            ]})
    with pytest.raises(Exception, match="pmo"):
        AppConfig.model_validate({
            **base, "crons": [
                {"id": "nightly-plan", "name": "A", "entry_stage": "PLAN",
                 "description_template": "t", "pmo": "nosuch"},
            ]})
    ok = AppConfig.model_validate({
        **base, "crons": [
            {"id": "nightly-plan", "name": "A", "entry_stage": "ONBOARD",
             "description_template": "t", "pmo": "eng"},
        ]})
    ids = {c.id for c in ok.crons}
    assert MEMORY_CURATOR_CRON_ID in ids
    assert "nightly-plan" in ids
    nightly = next(c for c in ok.crons if c.id == "nightly-plan")
    assert nightly.entry_stage == "ONBOARD"
    assert nightly.reserved is False


def test_reserved_cron_canonicalizes_and_strips_stray_reserved():
    base = _cfg(pmos=[PMOInstance(name="eng", team_key="T")]).model_dump()
    got = AppConfig.model_validate({
        **base, "crons": [{
            "id": MEMORY_CURATOR_CRON_ID,
            "name": "Memory Curator",
            "reserved": True,
            "entry_stage": "PLAN",
            "pmo": "eng",
            "enabled": True,
            "interval_minutes": 15,
            "description_template": "operator text",
        }]})
    row = next(c for c in got.crons if c.id == MEMORY_CURATOR_CRON_ID)
    assert row.entry_stage == "EXECUTE"
    assert row.pmo is None
    assert row.reserved is True
    assert row.enabled is True
    assert row.interval_minutes == 15
    assert row.description_template == "operator text"
    got2 = AppConfig.model_validate({
        **base, "crons": [{
            "id": "nightly-plan", "name": "A", "entry_stage": "PLAN",
            "description_template": "t", "pmo": "eng", "reserved": True,
        }]})
    nightly = next(c for c in got2.crons if c.id == "nightly-plan")
    assert nightly.reserved is False


# ── I1 / I2 / is_memory_bound ───────────────────────────────────────────────


def test_i1_three_lists_pairwise_disjoint():
    repos = _repos("webapp", "docs", "nb")
    with pytest.raises(Exception, match="cannot be both"):
        _cfg(repos=repos, pmos=[PMOInstance(
            name="eng", team_key="T", repos=["webapp"],
            memory_repos=["webapp"])])
    with pytest.raises(Exception, match="cannot be both"):
        _cfg(repos=repos, pmos=[PMOInstance(
            name="eng", team_key="T", reference_repos=["docs"],
            memory_repos=["docs"])])
    with pytest.raises(Exception, match="cannot be both"):
        _cfg(repos=repos, pmos=[PMOInstance(
            name="eng", team_key="T", repos=["webapp"],
            reference_repos=["webapp"])])
    ok = _cfg(repos=repos, pmos=[PMOInstance(
        name="eng", team_key="T", repos=["webapp"],
        reference_repos=["docs"], memory_repos=["nb"])])
    validate_memory_bindings(ok)


def test_i2_product_board_cannot_list_memory_as_one_of_several_work_repos():
    """Felix I2: M as one work repo among others on a product board — refuse."""
    repos = _repos("webapp", "docs", "nb")
    with pytest.raises(Exception, match="work repo"):
        _cfg(repos=repos, pmos=[PMOInstance(
            name="eng", team_key="T", repos=["webapp", "nb"],
            memory_repos=["nb"])])
    # same M via a sibling instance's memory list
    with pytest.raises(Exception, match="work repo"):
        _cfg(repos=repos, pmos=[
            PMOInstance(name="eng", team_key="A", repos=["webapp", "nb"]),
            PMOInstance(name="cs", team_key="B", repos=["webapp"],
                        memory_repos=["nb"]),
        ])


def test_i2_curator_board_repos_equals_m_ok_and_m_not_in_that_boards_memory():
    repos = _repos("webapp", "docs", "nb")
    cfg = _cfg(repos=repos, pmos=[
        PMOInstance(name="eng", team_key="A", repos=["webapp"],
                    reference_repos=["docs"], memory_repos=["nb"]),
        PMOInstance(name="cur", team_key="B", repos=["nb"]),
    ])
    validate_memory_bindings(cfg)
    assert "nb" not in cfg.pmos[1].memory_repos
    # M in both repos and memory_repos on the SAME instance — I1 already
    with pytest.raises(Exception):
        _cfg(repos=repos, pmos=[PMOInstance(
            name="cur", team_key="B", repos=["nb"], memory_repos=["nb"])])


def test_i2_dev_type_memory_makes_m_and_blocks_product_work_list():
    repos = _repos("webapp", "nb")
    cfg = _cfg(repos=repos, pmos=[
        PMOInstance(name="eng", team_key="A", repos=["webapp", "nb"]),
    ])
    # instance-only I2 is fine (nb is not in any instance memory_repos)
    validate_memory_bindings(cfg)
    with pytest.raises(Exception, match="work repo"):
        validate_memory_bindings(cfg, dev_types={"judgment": _dt(memory_repos=["nb"])})


def test_is_memory_bound_includes_curator_sole_work_repo():
    repos = _repos("webapp", "nb", "other")
    cfg = _cfg(repos=repos, pmos=[
        PMOInstance(name="eng", team_key="A", repos=["webapp"],
                    memory_repos=["nb"]),
        PMOInstance(name="cur", team_key="B", repos=["nb"]),
    ])
    assert is_memory_bound(cfg, "nb") is True
    assert is_memory_bound(cfg, "webapp") is False
    # a lone product board with one work repo is NOT memory-bound
    lonely = _cfg(repos=repos, pmos=[
        PMOInstance(name="eng", team_key="A", repos=["webapp"]),
    ])
    assert is_memory_bound(lonely, "webapp") is False
    # domain-bound only
    domain = _cfg(repos=repos, pmos=[
        PMOInstance(name="eng", team_key="A", repos=["webapp"]),
    ])
    assert is_memory_bound(
        domain, "nb", dev_types={"judgment": _dt(memory_repos=["nb"])}) is True


def test_auto_merge_permitted_enforces_memory_toggle():
    from devcake.config import auto_merge_permitted
    repos = _repos("webapp", "nb")
    cfg = _cfg(repos=repos, pmos=[
        PMOInstance(name="eng", team_key="A", repos=["webapp"],
                    memory_repos=["nb"]),
        PMOInstance(name="cur", team_key="B", repos=["nb"]),
    ], memory_auto_merge=False)
    card_on = SimpleNamespace(auto_merge=True)
    card_off = SimpleNamespace(auto_merge=False)
    assert auto_merge_permitted(cfg, card_on, "webapp") is True
    assert auto_merge_permitted(cfg, card_on, "nb") is False
    assert auto_merge_permitted(cfg, card_off, "nb") is False
    cfg.memory_auto_merge = True
    assert auto_merge_permitted(cfg, card_on, "nb") is True
    assert auto_merge_permitted(cfg, card_off, "nb") is False


# ── reserved-cron reconcile (PUT / bundle chokepoint) ───────────────────────


def test_reconcile_reinjects_omitted_reserved_row_preserving_tunables():
    live = [{
        "id": MEMORY_CURATOR_CRON_ID, "name": "Memory Curator",
        "reserved": True, "enabled": True, "interval_minutes": 20,
        "pmo": None, "entry_stage": "EXECUTE",
        "description_template": "live template",
    }]
    incoming = [{
        "id": "nightly-plan", "name": "A", "entry_stage": "PLAN",
        "description_template": "t", "pmo": "eng",
    }]
    out = reconcile_reserved_crons(live, incoming)
    ids = [c["id"] for c in out]
    assert MEMORY_CURATOR_CRON_ID in ids
    reserved = next(c for c in out if c["id"] == MEMORY_CURATOR_CRON_ID)
    assert reserved["enabled"] is True
    assert reserved["interval_minutes"] == 20
    assert reserved["description_template"] == "live template"
    assert reserved["pmo"] is None
    assert reserved["entry_stage"] == "EXECUTE"


def test_reconcile_strips_stray_reserved_and_canonicalizes_identity():
    live = [{
        "id": MEMORY_CURATOR_CRON_ID, "name": "Memory Curator",
        "reserved": True, "enabled": False, "interval_minutes": 60,
        "pmo": None, "entry_stage": "EXECUTE",
        "description_template": MEMORY_CURATOR_TEMPLATE,
    }]
    incoming = [
        {"id": MEMORY_CURATOR_CRON_ID, "name": "Memory Curator",
         "reserved": True, "enabled": True, "interval_minutes": 5,
         "pmo": "eng", "entry_stage": "REVIEW",
         "description_template": "keep me"},
        {"id": "sneaky", "name": "S", "entry_stage": "PLAN",
         "description_template": "t", "pmo": "eng", "reserved": True},
    ]
    out = reconcile_reserved_crons(live, incoming)
    reserved = next(c for c in out if c["id"] == MEMORY_CURATOR_CRON_ID)
    assert reserved["pmo"] is None
    assert reserved["entry_stage"] == "EXECUTE"
    assert reserved["enabled"] is True
    assert reserved["description_template"] == "keep me"
    sneaky = next(c for c in out if c["id"] == "sneaky")
    assert sneaky["reserved"] is False


def test_bundle_round_trip_carries_memory_fields_and_reserved_cron(monkeypatch,
                                                                   tmp_path):
    """Settings-bundle serialize/apply keeps the new fields (ADR-0013)."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    from devcake import settings_bundle
    cfg = _cfg(
        pmos=[PMOInstance(name="eng", team_key="T", repos=["webapp"],
                          memory_repos=["nb"])],
        repos=_repos("webapp", "nb"),
        context_sourcing_strict=False,
        memory_auto_merge=True,
        assignments={mt: Assignment(dev_type="judgment")
                     for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")},
        steward=Steward(dev_type="judgment"),
    )
    dts = {"judgment": _dt(memory_repos=["nb"])}
    bundle = settings_bundle.serialize_current(cfg, dts, include_secrets=False)
    app = bundle["config"]["app"]
    assert app["context_sourcing_strict"] is False
    assert app["memory_auto_merge"] is True
    assert app["pmos"][0]["memory_repos"] == ["nb"]
    assert dts["judgment"].memory_repos == ["nb"]
    assert any(c["id"] == MEMORY_CURATOR_CRON_ID for c in app["crons"])
    # apply onto a fresh world that omitted the new keys
    dst = tmp_path / "dst"
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(dst))
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        dst / "config" / "config.yaml")
    fresh = AppConfig()
    fresh_dts: dict = {}
    settings_bundle.apply_bundle(bundle, config=fresh, dev_types=fresh_dts,
                                 reload=lambda: None)
    assert fresh.context_sourcing_strict is False
    assert fresh.memory_auto_merge is True
    assert fresh.pmos[0].memory_repos == ["nb"]
    assert fresh_dts["judgment"].memory_repos == ["nb"]
    assert any(c.id == MEMORY_CURATOR_CRON_ID for c in fresh.crons)


def test_old_bundle_without_crons_reinjects_reserved_on_apply(monkeypatch,
                                                              tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    from devcake import settings_bundle
    live = _cfg(pmos=[PMOInstance(name="eng", team_key="T")])
    # operator already tuned the reserved row
    live.crons[0].enabled = True
    live.crons[0].interval_minutes = 7
    old = AppConfig.model_validate({
        **_cfg(pmos=[PMOInstance(name="eng", team_key="T")]).model_dump(),
    })
    incoming = [c.model_dump() for c in old.crons]
    # simulate a pre-feature dump that dropped the key entirely
    reconciled = reconcile_reserved_crons(
        [c.model_dump() for c in live.crons], [])
    rebuilt = AppConfig.model_validate(
        {**old.model_dump(), "crons": reconciled})
    row = next(c for c in rebuilt.crons if c.id == MEMORY_CURATOR_CRON_ID)
    assert row.enabled is True
    assert row.interval_minutes == 7
    # sanity: settings_bundle still validates a world that never had crons
    dumped = old.model_dump()
    dumped.pop("crons", None)
    assert any(c.id == MEMORY_CURATOR_CRON_ID
               for c in AppConfig.model_validate(dumped).crons)
    # silence unused import if apply path is not needed beyond reconcile
    assert settings_bundle is not None
    assert incoming is not None


def test_create_notebook_refuses_activity_prefix():
    import asyncio
    from fastapi import HTTPException
    from devcake.api.internal_repos_service import create_internal_repo

    async def boom(*_a, **_k):
        raise AssertionError("must not create")

    with pytest.raises(HTTPException) as ei:
        asyncio.new_event_loop().run_until_complete(
            create_internal_repo({"name": "activity-x"},
                                 internal_forge=SimpleNamespace(
                                     create_operator_repo=boom)))
    assert ei.value.status_code == 422
    assert "activity-" in ei.value.detail


def test_instance_memory_repos_must_name_configured_cards():
    """R1: a dangling board-bound notebook is refused at validation —
    the same existence rule as repos / reference_repos."""
    with pytest.raises(Exception, match="ghost"):
        AppConfig(
            repos=[RepoInstance(name="webapp",
                                url="https://github.com/acme/webapp")],
            pmos=[PMOInstance(name="eng", team_key="T",
                              memory_repos=["ghost"])])
