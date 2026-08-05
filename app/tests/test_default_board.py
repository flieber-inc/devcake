"""ADR-0030: the auto-provisioned default PMO board.

Covers the provisioner's idempotent ensure (create vs adopt, PAT liveness
vs re-mint, dependency enablement under admin), the managed-row config
doctrine (reserved name, reconcile across wholesale list replaces, stray
managed-flag stripping), and the instance registration helper (register,
repair, operator-adoption skip)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from devcake.adapters.gitea.provision import (BOARD_REPO, BOARD_USER, PMO_ORG,
                                              GiteaProvisioner)
from devcake.config import (AppConfig, MANAGED_BOARD_NAME, PMOInstance,
                            reconcile_managed_pmos)


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _prov(handler, tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    return GiteaProvisioner(url="http://gitea:3000", admin_user="a",
                            admin_password="p",
                            transport=httpx.MockTransport(handler))


def _board_row(**over):
    base = {"name": MANAGED_BOARD_NAME, "system": "gitea_issues",
            "team_key": f"{PMO_ORG}/{BOARD_REPO}",
            "api_base": "http://gitea:3000", "managed": True}
    base.update(over)
    return base


# ── provisioner ──────────────────────────────────────────────────────────────

def test_ensure_pmo_board_creates_mints_and_enables_deps(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith(
                f"/repos/{PMO_ORG}/{BOARD_REPO}"):
            return httpx.Response(404)          # fresh stack — repo missing
        if request.method == "GET" and "/tokens" in request.url.path:
            return httpx.Response(200, json=[])  # no PAT yet
        if request.method == "POST" and request.url.path.endswith(
                f"/users/{BOARD_USER}/tokens"):
            return httpx.Response(201, json={"sha1": "board-tok-12345678"})
        return httpx.Response(201, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    info = run_coro(prov.ensure_pmo_board())
    assert info == {"team_key": f"{PMO_ORG}/{BOARD_REPO}",
                    "api_base": "http://gitea:3000",
                    "minted": True, "adopted": False}
    # the PAT landed in the secret store under the managed instance name
    stored = json.loads(
        (tmp_path / "secrets" / "connections"
         / f"pmo-{MANAGED_BOARD_NAME}.json").read_text())
    assert stored["api_key"] == "board-tok-12345678"
    # issue dependencies were enabled UNDER ADMIN (the board PAT cannot)
    patches = [p for m, p in calls if m == "PATCH"]
    assert patches == [f"/api/v1/repos/{PMO_ORG}/{BOARD_REPO}"]
    # the board user became a write collaborator
    assert any(m == "PUT" and p.endswith(f"/collaborators/{BOARD_USER}")
               for m, p in calls)


def test_ensure_pmo_board_adopts_and_keeps_live_pat(tmp_path, monkeypatch):
    d = tmp_path / "secrets" / "connections"
    d.mkdir(parents=True)
    (d / f"pmo-{MANAGED_BOARD_NAME}.json").write_text(
        json.dumps({"api_key": "live-tok-abcdefgh"}))
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith(
                f"/repos/{PMO_ORG}/{BOARD_REPO}"):
            return httpx.Response(200, json={"name": BOARD_REPO})  # adopt
        if request.method == "GET" and "/tokens" in request.url.path:
            return httpx.Response(200, json=[
                {"name": "devcake-board", "token_last_eight": "abcdefgh"}])
        return httpx.Response(201, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    info = run_coro(prov.ensure_pmo_board())
    assert info["adopted"] is True and info["minted"] is False
    # a live PAT is never re-minted (delete-then-create would revoke it)
    assert not any(m in ("DELETE",) or p.endswith(f"/users/{BOARD_USER}/tokens")
                   and m == "POST" for m, p in calls
                   if "/tokens" in p and m != "GET")
    # no repo creation on adopt
    assert not any(m == "POST" and p.endswith(f"/orgs/{PMO_ORG}/repos")
                   for m, p in calls)


# ── config doctrine ──────────────────────────────────────────────────────────

def test_reserved_board_name_refused_for_operator_rows():
    with pytest.raises(ValueError, match="reserved for the auto-provisioned"):
        AppConfig(pmos=[PMOInstance(name=MANAGED_BOARD_NAME,
                                    system="gitea_issues", team_key="x/y")])
    # the app-stamped (or bundle-carried) managed row validates
    cfg = AppConfig(pmos=[PMOInstance(**_board_row())])
    assert cfg.pmos[0].managed is True


def test_managed_defaults_false_and_roundtrips():
    inst = PMOInstance(name="linear", team_key="T")
    assert inst.managed is False
    cfg = AppConfig(pmos=[PMOInstance(**_board_row())])
    assert AppConfig.model_validate(cfg.model_dump()).pmos[0].managed is True


def test_reconcile_reinjects_omitted_managed_row_when_forge_present():
    current = [_board_row(), {"name": "linear", "system": "linear",
                              "team_key": "DEV", "managed": False}]
    incoming = [{"name": "linear", "system": "linear", "team_key": "DEV"}]
    out = reconcile_managed_pmos(current, incoming, internal_forge_present=True)
    assert [p["name"] for p in out] == ["linear", MANAGED_BOARD_NAME]
    assert out[1]["managed"] is True
    # provisioner absent → deletion allowed (torn-out-Gitea escape hatch)
    out2 = reconcile_managed_pmos(current, incoming, internal_forge_present=False)
    assert [p["name"] for p in out2] == ["linear"]


def test_reconcile_canonicalizes_identity_keeps_operator_tunables():
    current = [_board_row()]
    incoming = [_board_row(team_key="hacked/elsewhere", system="linear",
                           intake_paused=True, repos=["webapp"],
                           reference_repos=["docs1"])]
    out = reconcile_managed_pmos(current, incoming, internal_forge_present=True)
    assert out[0]["team_key"] == f"{PMO_ORG}/{BOARD_REPO}"   # identity: live
    assert out[0]["system"] == "gitea_issues"
    assert out[0]["intake_paused"] is True                   # tunables: incoming
    assert out[0]["repos"] == ["webapp"]
    assert out[0]["reference_repos"] == ["docs1"]


def test_reconcile_strips_stray_managed_flag():
    out = reconcile_managed_pmos(
        [], [{"name": "sneaky", "system": "linear", "team_key": "T",
              "managed": True}],
        internal_forge_present=True)
    assert out[0]["managed"] is False


def test_bundle_apply_would_keep_board_row():
    """The apply_bundle choke point feeds reconcile the PARSED config — an
    old profile without the row must come out with it re-added and the
    result must still validate (the exact shape apply_bundle rebuilds)."""
    live = AppConfig(pmos=[PMOInstance(**_board_row())])
    old_profile_cfg = AppConfig(pmos=[PMOInstance(name="linear",
                                                  team_key="DEV")])
    reconciled = reconcile_managed_pmos(
        [p.model_dump() for p in live.pmos],
        [p.model_dump() for p in old_profile_cfg.pmos],
        internal_forge_present=True)
    rebuilt = AppConfig.model_validate(
        {**old_profile_cfg.model_dump(), "pmos": reconciled})
    assert {p.name for p in rebuilt.pmos} == {"linear", MANAGED_BOARD_NAME}


# ── instance registration helper ─────────────────────────────────────────────

def _services(tmp_path, monkeypatch, *, pmos=(), board_info=None):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    cfg = AppConfig(pmos=list(pmos))
    reloads = []
    forge = SimpleNamespace(ensure_pmo_board=None)

    async def ensure_pmo_board():
        return board_info or {"team_key": f"{PMO_ORG}/{BOARD_REPO}",
                              "api_base": "http://gitea:3000",
                              "minted": False, "adopted": True}
    forge.ensure_pmo_board = ensure_pmo_board
    return SimpleNamespace(config=cfg, internal_forge=forge,
                           reload_connections=lambda: reloads.append(1),
                           _reloads=reloads)


def test_ensure_default_board_registers_row(tmp_path, monkeypatch):
    from devcake.api.default_board import ensure_default_board
    s = _services(tmp_path, monkeypatch)
    run_coro(ensure_default_board(s))
    row = s.config.pmos[0]
    assert (row.name, row.system, row.managed) == (
        MANAGED_BOARD_NAME, "gitea_issues", True)
    assert row.team_key == f"{PMO_ORG}/{BOARD_REPO}"
    assert s._reloads  # managers must pick the new instance up


def test_ensure_default_board_noop_when_healthy(tmp_path, monkeypatch):
    from devcake.api.default_board import ensure_default_board
    s = _services(tmp_path, monkeypatch, pmos=[PMOInstance(**_board_row())])
    run_coro(ensure_default_board(s))
    assert s._reloads == []            # nothing changed, nothing reloaded


def test_ensure_default_board_reloads_on_remint(tmp_path, monkeypatch):
    from devcake.api.default_board import ensure_default_board
    s = _services(tmp_path, monkeypatch, pmos=[PMOInstance(**_board_row())],
                  board_info={"team_key": f"{PMO_ORG}/{BOARD_REPO}",
                              "api_base": "http://gitea:3000",
                              "minted": True, "adopted": True})
    run_coro(ensure_default_board(s))
    assert s._reloads  # the running adapter cached the dead PAT


def test_ensure_default_board_adopts_operator_instance(tmp_path, monkeypatch):
    from devcake.api.default_board import ensure_default_board
    manual = PMOInstance(name="myboard", system="gitea_issues",
                         team_key=f"{PMO_ORG}/{BOARD_REPO}",
                         api_base="http://gitea:3000")
    s = _services(tmp_path, monkeypatch, pmos=[manual])
    run_coro(ensure_default_board(s))
    # duplicate-target validator would refuse a second row — adopt, don't crash
    assert [p.name for p in s.config.pmos] == ["myboard"]
    assert s._reloads == []


def test_ensure_default_board_skips_without_forge(tmp_path, monkeypatch):
    from devcake.api.default_board import ensure_default_board
    s = _services(tmp_path, monkeypatch)
    s.internal_forge = None
    run_coro(ensure_default_board(s))
    assert s.config.pmos == [] and s._reloads == []


def test_board_mission_repo_provisions_with_a_valid_machine_user(
        tmp_path, monkeypatch):
    """End-to-end shape of the founder-reported gate (2026-08-05).

    A zero-repo mission on the DEFAULT board routes to the internal forge.
    Gitea refuses a username with consecutive hyphens, so the derived name
    had to be valid for `POST /admin/users` to create it — otherwise the
    collaborator PUT that follows 422'd with "user does not exist" and the
    mission surfaced as "internal forge unreachable — mission gated"."""
    from devcake.adapters.gitea.provision import _svc_user
    from devcake.ports.internal_forge import internal_repo_name

    repo = internal_repo_name("board", "devcake-pmo/missions#21")
    assert repo == "board-devcake-pmo-missions-21"
    created_users, collaborators = [], []

    def handler(request):
        path = request.url.path
        if request.method == "POST" and path.endswith("/admin/users"):
            name = json.loads(request.content)["username"]
            # the real Gitea rule (measured on 1.27.1)
            if "--" in name or name.endswith("-"):
                return httpx.Response(
                    422, json={"message": "[Username]: invalid username"})
            created_users.append(name)
            return httpx.Response(201, json={})
        if request.method == "PUT" and "/collaborators/" in path:
            user = path.rsplit("/", 1)[1]
            if user not in created_users and user != "devcake-reviewer":
                return httpx.Response(422, json={
                    "message": f"user does not exist [uid: 0, name: {user}]"})
            collaborators.append(user)
            return httpx.Response(204)
        if request.method == "POST" and "/tokens" in path:
            return httpx.Response(201, json={"sha1": "tok-12345678"})
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(201, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    creds = run_coro(prov.ensure_mission_repo("board",
                                              "devcake-pmo/missions#21"))

    assert creds.username == _svc_user(repo)
    assert creds.username in created_users     # the user really got created
    assert creds.username in collaborators     # and the PUT accepted it
