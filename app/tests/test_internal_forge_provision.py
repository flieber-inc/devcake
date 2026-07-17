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


# ── skill store (docs/16 skill store v1) ─────────────────────────────────────

import base64


def _seed_files():
    return [
        {"path": "README.md",
         "content_b64": base64.b64encode(b"store readme").decode()},
        {"path": "tdd/SKILL.md",
         "content_b64": base64.b64encode(b"---\nname: tdd\n---\n").decode()},
        {"path": "tdd/reference.md",
         "content_b64": base64.b64encode(b"ref").decode()},
    ]


def _tree_response(paths, truncated=False):
    return httpx.Response(200, json={
        "tree": [{"path": p, "type": "blob", "size": 42} for p in paths],
        "truncated": truncated})


class _StoreRecorder:
    """Route the Gitea calls ensure_skill_store makes; record contents writes.

    empty_status: what GET git/trees/main returns before the first commit —
    Gitea 1.24 answers 400 "sha not found [main]" (live-verified 2026-07-17),
    while a missing repo/branch is 404. Both must read as "empty"."""

    def __init__(self, existing_paths=None, repo_exists=True, empty_status=404):
        self.existing_paths = existing_paths
        self.repo_exists = repo_exists
        self.empty_status = empty_status
        self.contents_batches = []
        self.calls = []

    def __call__(self, request):
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if method == "POST" and path == "/api/v1/orgs":
            return httpx.Response(409, json={})
        if method == "POST" and path == "/api/v1/orgs/devcake-repos/repos":
            return httpx.Response(409 if self.repo_exists else 201, json={})
        if "/git/trees/" in path:
            if self.existing_paths is None:
                return httpx.Response(self.empty_status,
                                      json={"message": "sha not found [main]"
                                            if self.empty_status == 400
                                            else "no tree"})
            return _tree_response(self.existing_paths)
        if method == "POST" and path.endswith("/skill-store/contents"):
            self.contents_batches.append(json.loads(request.content))
            return httpx.Response(201, json={})
        if method == "GET" and "/contents/" in path:
            return httpx.Response(200, json={
                "encoding": "base64",
                "content": base64.b64encode(b"file body").decode()})
        return httpx.Response(200, json={})


def test_skill_store_fresh_repo_seeds_everything(tmp_path, monkeypatch):
    rec = _StoreRecorder(existing_paths=None, repo_exists=False)
    prov = _prov(rec, tmp_path, monkeypatch)
    run_coro(prov.ensure_skill_store(_seed_files()))
    assert len(rec.contents_batches) == 1
    body = rec.contents_batches[0]
    assert {f["path"] for f in body["files"]} == {
        "README.md", "tdd/SKILL.md", "tdd/reference.md"}
    assert all(f["operation"] == "create" for f in body["files"])
    # the store repo is operator-pushable: no protection, no users, no tokens
    assert not any("branch_protections" in p for _, p in rec.calls)
    assert not any("collaborators" in p for _, p in rec.calls)
    assert not any(p.endswith("/tokens") for _, p in rec.calls)


def test_skill_store_fully_seeded_writes_nothing(tmp_path, monkeypatch):
    rec = _StoreRecorder(existing_paths=[
        "README.md", "tdd/SKILL.md", "tdd/reference.md"])
    prov = _prov(rec, tmp_path, monkeypatch)
    run_coro(prov.ensure_skill_store(_seed_files()))
    assert rec.contents_batches == []


def test_skill_store_partial_tree_seeds_only_missing(tmp_path, monkeypatch):
    rec = _StoreRecorder(existing_paths=["README.md", "tdd/SKILL.md"])
    prov = _prov(rec, tmp_path, monkeypatch)
    run_coro(prov.ensure_skill_store(_seed_files()))
    assert len(rec.contents_batches) == 1
    assert {f["path"] for f in rec.contents_batches[0]["files"]} == {
        "tdd/reference.md"}


def test_skill_store_paths_and_file_reads(tmp_path, monkeypatch):
    rec = _StoreRecorder(existing_paths=["README.md", "tdd/SKILL.md"])
    prov = _prov(rec, tmp_path, monkeypatch)
    assert run_coro(prov.skill_store_paths()) == ["README.md", "tdd/SKILL.md"]
    assert run_coro(prov.skill_store_file("tdd/SKILL.md")) == b"file body"
    assert prov.skill_store_url().endswith("/devcake-repos/skill-store")


def test_skill_store_tree_sizes_and_truncation_warning(tmp_path, monkeypatch, caplog):
    """Sizes ride the tree listing (SkillService pre-fetch cap checks); a
    truncated tree (~1000-entry Gitea cap) must be loud, not silent."""
    import logging

    def handler(request):
        return _tree_response(["README.md", "tdd/SKILL.md"], truncated=True)

    prov = _prov(handler, tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="devcake.internal_forge"):
        tree = run_coro(prov.skill_store_tree())
    assert tree == [{"path": "README.md", "size": 42},
                    {"path": "tdd/SKILL.md", "size": 42}]
    assert any("TRUNCATED" in r.message for r in caplog.records)


def test_skill_store_paths_empty_repo_returns_empty(tmp_path, monkeypatch):
    rec = _StoreRecorder(existing_paths=None)     # trees → 404 (no commits yet)
    prov = _prov(rec, tmp_path, monkeypatch)
    assert run_coro(prov.skill_store_paths()) == []


def test_skill_store_seeds_no_commit_repo_gitea_400(tmp_path, monkeypatch):
    """Live Gitea 1.24 answers 400 'sha not found [main]' (not 404) for the
    tree of a freshly created no-auto_init repo — the pre-fix code raised and
    aborted the very first seed."""
    rec = _StoreRecorder(existing_paths=None, repo_exists=False,
                         empty_status=400)
    prov = _prov(rec, tmp_path, monkeypatch)
    run_coro(prov.ensure_skill_store(_seed_files()))
    assert len(rec.contents_batches) == 1
    assert {f["path"] for f in rec.contents_batches[0]["files"]} == {
        "README.md", "tdd/SKILL.md", "tdd/reference.md"}


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
    assert out["adopted"] is False
    assert len(minted) == 3                       # write + ro + reviewer
    assert any("devcake-repos/newrepo/branch_protections" in p for _, p in calls)
    stored = _json.loads(
        (tmp_path / "secrets" / "connections" / "repo-newrepo.json").read_text())
    assert set(stored) == {"token", "token_ro", "reviewer_token"}
    assert all(v.startswith("tok-") for v in stored.values())


def test_create_operator_repo_adopts_existing(tmp_path, monkeypatch):
    """Founder report 2026-07-15: removing a repo card deletes its stored
    tokens while the Gitea repo lives on — and the old 409 ("pick another
    name") made re-adding it impossible: the card could never receive keys
    again. An existing devcake-repos repo is now ADOPTED: it is NOT
    re-created, but guardrails (protection, collaborators) are re-ensured
    and a FRESH token set is minted and stored under the card name."""
    import json as _json
    calls, minted = [], []

    def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "GET" and "/repos/devcake-repos/taken" in path:
            return httpx.Response(200, json={"name": "taken"})
        if request.method == "POST" and path.endswith("/tokens"):
            minted.append(path)
            return httpx.Response(201, json={"sha1": f"tok-{len(minted)}-1234"})
        return httpx.Response(201, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    out = run_coro(prov.create_operator_repo("taken"))
    assert out["adopted"] is True
    assert out["clone_url"] == "http://gitea:3000/devcake-repos/taken.git"
    assert not any(m == "POST" and p.endswith("/orgs/devcake-repos/repos")
                   for m, p in calls)             # existing repo NOT re-created
    assert any("devcake-repos/taken/branch_protections" in p for _, p in calls)
    assert len(minted) == 3
    stored = _json.loads(
        (tmp_path / "secrets" / "connections" / "repo-taken.json").read_text())
    assert set(stored) == {"token", "token_ro", "reviewer_token"}


# ── approvals whitelist must survive Gitea's silent drop (2026-07-15) ─────────
# Gitea silently REMOVES non-collaborator names from
# approvals_whitelist_username at protection-create time. Both provisioning
# flows posted the protection BEFORE adding devcake-reviewer as collaborator,
# so every repo shipped with an EMPTY enabled whitelist — required_approvals
# could never be satisfied and merges 405'd forever (live-verified on all
# three existing repos). The fix: collaborators first, protection after, and
# the protection helper PATCHes an existing protection whose whitelist lost
# the reviewer (repair path for adopted/legacy repos).

def _order(calls, needle):
    return next(i for i, (m, p) in enumerate(calls) if needle in p)


def test_operator_repo_collaborators_precede_protection(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "GET" and path.endswith("branch_protections/main"):
            return httpx.Response(404, json={})
        if request.method == "GET" and "/repos/devcake-repos/newrepo" in path:
            return httpx.Response(404, json={})
        if request.method == "POST" and path.endswith("/tokens"):
            return httpx.Response(201, json={"sha1": "tok-1234"})
        return httpx.Response(201, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    run_coro(prov.create_operator_repo("newrepo"))
    reviewer_collab = _order(
        calls, "/repos/devcake-repos/newrepo/collaborators/devcake-reviewer")
    protection = _order(calls, "/repos/devcake-repos/newrepo/branch_protections")
    assert reviewer_collab < protection


def test_mission_repo_collaborators_precede_protection(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "GET" and path.endswith("branch_protections/main"):
            return httpx.Response(404, json={})
        if request.method == "POST" and path.endswith("/tokens"):
            return httpx.Response(201, json={"sha1": "tok-1234"})
        return httpx.Response(201, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    run_coro(prov.ensure_mission_repo("linear", "T-9"))
    repo = "devcake-internal/linear-t-9"
    reviewer_collab = _order(calls, f"/repos/{repo}/collaborators/devcake-reviewer")
    protection = _order(calls, f"/repos/{repo}/branch_protections")
    assert reviewer_collab < protection


def test_adopt_repairs_stripped_whitelist(tmp_path, monkeypatch):
    """An adopted (or legacy) repo whose protection exists with an empty
    whitelist gets PATCHed back to [devcake-reviewer]."""
    patched = []

    def handler(request):
        path = request.url.path
        if request.method == "GET" and "/repos/devcake-repos/taken" in path \
                and "branch_protections" not in path:
            return httpx.Response(200, json={"name": "taken"})
        if path.endswith("branch_protections/main"):
            if request.method == "PATCH":
                patched.append(json.loads(request.content))
                return httpx.Response(200, json={})
            return httpx.Response(200, json={
                "branch_name": "main", "required_approvals": 1,
                "enable_approvals_whitelist": True,
                "approvals_whitelist_username": []})     # the stripped state
        if request.method == "POST" and path.endswith("/tokens"):
            return httpx.Response(201, json={"sha1": "tok-1234"})
        return httpx.Response(201, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    run_coro(prov.create_operator_repo("taken"))
    assert patched and patched[0]["approvals_whitelist_username"] == ["devcake-reviewer"]
