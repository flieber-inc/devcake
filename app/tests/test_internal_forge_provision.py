"""Gitea provisioner robustness (audit A13): the stored-creds reuse probe is
tri-state — a TRANSIENT failure (timeout/5xx) must never re-mint, because
re-minting deletes-then-creates the token pair an in-flight Dev already
holds; and BOTH tokens are validated, so a revoked write token can no longer
ride along silently behind a live read token."""

import asyncio
import json

import httpx

from devcake.adapters.gitea.provision import GiteaProvisioner


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


REPO = "linear-t-1"


def _seed_stored(tmp_path):
    d = tmp_path / "secrets" / "internal_forge"
    d.mkdir(parents=True)
    (d / f"mission-{REPO}.json").write_text(json.dumps({
        "repo_name": REPO,
        "clone_url": f"http://gitea:3000/devcake-internal/{REPO}.git",
        "username": "svc-linear-t-1-deadbeef",
        "token_write": "w-tok-1234", "token_read": "r-tok-1234"}))


def _prov(handler, tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    return GiteaProvisioner(url="http://gitea:3000", admin_user="a",
                            admin_password="p",
                            transport=httpx.MockTransport(handler))


def test_transient_probe_reuses_stored_without_reminting(tmp_path, monkeypatch):
    _seed_stored(tmp_path)
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        raise httpx.ConnectError("gitea hiccup")

    prov = _prov(handler, tmp_path, monkeypatch)
    creds = run_coro(prov.ensure_mission_repo("linear", "T-1"))
    assert creds.token_write == "w-tok-1234"     # reused untouched
    assert creds.token_read == "r-tok-1234"
    assert all(m == "GET" for m, _ in calls)     # probes only — nothing minted


def test_live_pair_reused_without_admin_calls(tmp_path, monkeypatch):
    _seed_stored(tmp_path)
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.headers.get("Authorization", "").startswith("token ")
        return httpx.Response(200, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    creds = run_coro(prov.ensure_mission_repo("linear", "T-1"))
    assert creds.token_write == "w-tok-1234"
    assert len(calls) == 2                       # one probe per token


def test_revoked_write_token_forces_remint(tmp_path, monkeypatch):
    """Pre-fix only token_read was probed — a dead write token was reused
    silently and EXECUTE failed at push time."""
    _seed_stored(tmp_path)
    minted = []

    def handler(request):
        auth = request.headers.get("Authorization", "")
        if auth == "token r-tok-1234":
            return httpx.Response(200, json={})
        if auth == "token w-tok-1234":
            return httpx.Response(401, json={"message": "token revoked"})
        if request.method == "POST" and request.url.path.endswith("/tokens"):
            minted.append(request.url.path)
            return httpx.Response(201, json={"sha1": f"new-tok-{len(minted)}"})
        return httpx.Response(200, json={})      # org/repo/user/collab admin ops

    prov = _prov(handler, tmp_path, monkeypatch)
    creds = run_coro(prov.ensure_mission_repo("linear", "T-1"))
    assert len(minted) == 2                      # fresh pair
    assert creds.token_write.startswith("new-tok-")


def test_create_operator_repo_mints_and_stores_card_tokens(tmp_path, monkeypatch):
    """Item 4: a 'gitea (internal)' card's repo lives in the SEPARATE
    devcake-repos org (the per-mission list/sweep walks devcake-internal
    only) and its full token set lands in the secret store under the card
    name — saving the card is all that's left."""
    import json as _json
    calls = []
    minted = []

    def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "GET" and "/repos/devcake-repos/newrepo" in path:
            return httpx.Response(404, json={})
        if request.method == "POST" and path.endswith("/tokens"):
            minted.append(path)
            return httpx.Response(201, json={"sha1": f"tok-{len(minted)}-1234"})
        return httpx.Response(201, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    out = run_coro(prov.create_operator_repo("newrepo"))
    assert out["clone_url"] == "http://gitea:3000/devcake-repos/newrepo.git"
    assert len(minted) == 3                       # write + ro + reviewer
    assert any("devcake-repos/newrepo/branch_protections" in p for _, p in calls)
    stored = _json.loads(
        (tmp_path / "secrets" / "connections" / "repo-newrepo.json").read_text())
    assert set(stored) == {"token", "token_ro", "reviewer_token"}
    assert all(v.startswith("tok-") for v in stored.values())


def test_create_operator_repo_refuses_existing(tmp_path, monkeypatch):
    def handler(request):
        if request.method == "GET" and "/repos/devcake-repos/taken" in request.url.path:
            return httpx.Response(200, json={"name": "taken"})
        return httpx.Response(201, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    import pytest as _pytest
    with _pytest.raises(ValueError, match="already exists"):
        run_coro(prov.create_operator_repo("taken"))
