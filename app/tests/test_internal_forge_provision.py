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
        "tree": [{"path": p, "type": "blob", "size": 42, "sha": f"sha-{p}"}
                 for p in paths],
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
    assert tree == [
        {"path": "README.md", "size": 42, "sha": "sha-README.md"},
        {"path": "tdd/SKILL.md", "size": 42, "sha": "sha-tdd/SKILL.md"}]
    assert any("TRUNCATED" in r.message for r in caplog.records)


def test_write_skill_files_create_vs_update(tmp_path, monkeypatch):
    """The batch contents API needs the blob sha for updates but must NOT
    send one for creates — Gitea 422s on either mismatch."""
    rec = _StoreRecorder(existing_paths=["custom/SKILL.md"])
    prov = _prov(rec, tmp_path, monkeypatch)
    run_coro(prov.write_skill_files([
        {"path": "custom/SKILL.md", "content_b64": "QQ=="},
        {"path": "custom/new.md", "content_b64": "Qg=="},
    ], "devcake admin: save skill custom"))
    body = rec.contents_batches[0]
    ops = {f["path"]: f for f in body["files"]}
    assert ops["custom/SKILL.md"]["operation"] == "update"
    assert ops["custom/SKILL.md"]["sha"] == "sha-custom/SKILL.md"
    assert ops["custom/new.md"]["operation"] == "create"
    assert "sha" not in ops["custom/new.md"]
    assert body["message"] == "devcake admin: save skill custom"


def test_delete_skill_paths_sends_shas(tmp_path, monkeypatch):
    rec = _StoreRecorder(existing_paths=["custom/SKILL.md", "custom/x.md"])
    prov = _prov(rec, tmp_path, monkeypatch)
    run_coro(prov.delete_skill_paths(
        ["custom/SKILL.md", "custom/x.md"], "devcake admin: delete skill custom"))
    body = rec.contents_batches[0]
    assert all(f["operation"] == "delete" for f in body["files"])
    assert {f["sha"] for f in body["files"]} == {
        "sha-custom/SKILL.md", "sha-custom/x.md"}
    assert "content" not in body["files"][0]


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


def test_ensure_protection_post_403_fails_loud(tmp_path, monkeypatch):
    """Branch-protection create must not swallow 403 — provisioning that
    silently ships without protection leaves merges forever-broken."""

    def handler(request):
        path = request.url.path
        if request.method == "GET" and path.endswith("branch_protections/main"):
            return httpx.Response(404, json={})
        if request.method == "POST" and path.endswith("/branch_protections"):
            return httpx.Response(403, json={"message": "forbidden"})
        if request.method == "POST" and path.endswith("/tokens"):
            return httpx.Response(201, json={"sha1": "tok-1234"})
        return httpx.Response(201, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError) as e:
        run_coro(prov.ensure_mission_repo("linear", "T-prot"))
    assert "403" in str(e.value)
    assert "branch_protections" in str(e.value)


def test_ensure_protection_patch_422_fails_loud(tmp_path, monkeypatch):
    """Repair PATCH of a stripped whitelist must fail loud on 422."""

    def handler(request):
        path = request.url.path
        if request.method == "GET" and "/repos/devcake-repos/taken" in path \
                and "branch_protections" not in path:
            return httpx.Response(200, json={"name": "taken"})
        if path.endswith("branch_protections/main"):
            if request.method == "PATCH":
                return httpx.Response(422, json={"message": "invalid"})
            return httpx.Response(200, json={
                "branch_name": "main", "required_approvals": 1,
                "enable_approvals_whitelist": True,
                "approvals_whitelist_username": []})
        if request.method == "POST" and path.endswith("/tokens"):
            return httpx.Response(201, json={"sha1": "tok-1234"})
        return httpx.Response(201, json={})

    prov = _prov(handler, tmp_path, monkeypatch)
    with pytest.raises(RuntimeError) as e:
        run_coro(prov.create_operator_repo("taken"))
    assert "422" in str(e.value)
    assert "branch_protections" in str(e.value)


# ── activity repos (ADR-0014 D4) ─────────────────────────────────────────────

import hashlib

import pytest


class _ActivityRecorder:
    """Route the Gitea calls the activity-repo surface makes; record writes."""

    def __init__(self, tree_entries=(), org_pages=None, tokens=None,
                 repo_create_status=201):
        self.calls = []
        self.contents_batches = []
        self.repo_creates = []
        self.token_posts = []
        self.collab_puts = []
        self.tree_entries = list(tree_entries)
        self.org_pages = org_pages or []
        self.tokens = tokens if tokens is not None else []
        self.repo_create_status = repo_create_status

    def __call__(self, request):
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if method == "POST" and path == "/api/v1/orgs":
            return httpx.Response(409, json={})
        if method == "POST" and path == "/api/v1/orgs/devcake-repos/repos":
            self.repo_creates.append(json.loads(request.content))
            return httpx.Response(self.repo_create_status, json={})
        if method == "PUT" and "/collaborators/" in path:
            self.collab_puts.append((path, json.loads(request.content)))
            return httpx.Response(204)
        if method == "GET" and "/git/trees/" in path:
            return httpx.Response(200, json={
                "tree": [{"path": t["path"], "type": "blob", "size": 1,
                          "sha": t["sha"]} for t in self.tree_entries],
                "truncated": False})
        if method == "POST" and "/contents" in path:
            self.contents_batches.append((path, json.loads(request.content)))
            return httpx.Response(201, json={})
        if method == "GET" and path == "/api/v1/orgs/devcake-repos/repos":
            page = int(request.url.params.get("page", "1"))
            repos = (self.org_pages[page - 1]
                     if page <= len(self.org_pages) else [])
            return httpx.Response(200, json=repos)
        if method == "DELETE" and path.startswith("/api/v1/repos/devcake-repos/"):
            return httpx.Response(204)
        if method == "GET" and path.endswith("/tokens"):
            return httpx.Response(200, json=self.tokens)
        if method == "POST" and path.endswith("/tokens"):
            self.token_posts.append((path, json.loads(request.content)))
            return httpx.Response(201, json={"sha1": "minted-tok"})
        if method == "DELETE" and "/tokens/" in path:
            return httpx.Response(404, json={})
        if method == "POST" and path == "/api/v1/admin/users":
            return httpx.Response(201, json={})
        if method == "GET" and path == "/api/v1/orgs/devcake-internal/teams":
            return httpx.Response(200, json=[{"id": 1, "name": "Owners"}])
        if method == "PUT" and "/members/" in path:
            return httpx.Response(204)
        return httpx.Response(200, json={})


def test_ensure_service_accounts_mints_activity_ro(tmp_path, monkeypatch):
    rec = _ActivityRecorder()
    prov = _prov(rec, tmp_path, monkeypatch)
    run_coro(prov.ensure_service_accounts())
    users = [c for c in rec.calls if c == ("POST", "/api/v1/admin/users")]
    assert len(users) == 3                    # app, reviewer, activity-ro
    # the shared Dev-held account must NEVER join Owners — that would grant
    # its token read on every devcake-internal work repo (review 3.x pin)
    assert not any("/members/devcake-activity-ro" in p for _, p in rec.calls)
    ro_mints = [(p, b) for p, b in rec.token_posts
                if p == "/api/v1/users/devcake-activity-ro/tokens"]
    assert len(ro_mints) == 1
    assert ro_mints[0][1]["scopes"] == ["read:repository"]
    svc = json.loads(
        (tmp_path / "secrets" / "internal_forge" / "service.json").read_text())
    assert svc["activity_ro_token"] == "minted-tok"


def test_ensure_service_accounts_reuses_live_activity_token(tmp_path,
                                                            monkeypatch):
    d = tmp_path / "secrets" / "internal_forge"
    d.mkdir(parents=True)
    (d / "service.json").write_text(json.dumps({
        "app_token": "app-tok-aaaaaaaa", "reviewer_token": "rev-tok-bbbbbbbb",
        "activity_ro_token": "act-tok-cccccccc"}))
    rec = _ActivityRecorder(tokens=[
        {"name": "devcake-app", "token_last_eight": "aaaaaaaa"},
        {"name": "devcake-reviewer", "token_last_eight": "bbbbbbbb"},
        {"name": "devcake-activity-ro", "token_last_eight": "cccccccc"}])
    prov = _prov(rec, tmp_path, monkeypatch)
    run_coro(prov.ensure_service_accounts())
    assert rec.token_posts == []              # everything live — nothing minted


def test_ensure_activity_repo_unprotected_with_ro_collaborator(tmp_path,
                                                               monkeypatch):
    for status in (201, 409):                 # fresh create AND adopt paths
        rec = _ActivityRecorder(repo_create_status=status)
        prov = _prov(rec, tmp_path, monkeypatch)
        name = run_coro(prov.ensure_activity_repo("linear", "T-1"))
        assert name == "activity-linear-t-1"
        assert ("POST", "/api/v1/orgs") in rec.calls    # org ensured first
        assert rec.repo_creates[0]["auto_init"] is False
        assert rec.repo_creates[0]["private"] is True
        assert ("PUT", "/api/v1/repos/devcake-repos/activity-linear-t-1"
                       "/collaborators/devcake-activity-ro") in rec.calls
        # READ, never write — the shared token must stay a pure reader
        assert rec.collab_puts[-1][1] == {"permission": "read"}
        assert not any("branch_protections" in p for _, p in rec.calls)
        assert ("POST", "/api/v1/admin/users") not in rec.calls  # no svc user


def test_push_activity_snapshot_upserts_prunes_and_skips_unchanged(
        tmp_path, monkeypatch):
    same = b"unchanged bytes"
    same_sha = hashlib.sha1(b"blob %d\x00" % len(same) + same).hexdigest()
    rec = _ActivityRecorder(tree_entries=[
        {"path": "ACTIVITY.md", "sha": "a1"},
        {"path": "old-entry.md", "sha": "b2"},
        {"path": "same.md", "sha": same_sha}])
    prov = _prov(rec, tmp_path, monkeypatch)
    files = [{"path": "ACTIVITY.md",
              "content_b64": base64.b64encode(b"new feed").decode()},
             {"path": "photo.png",
              "content_b64": base64.b64encode(b"\x89PNG").decode()},
             {"path": "same.md",
              "content_b64": base64.b64encode(same).decode()}]
    run_coro(prov.push_activity_snapshot("activity-linear-t-1", files,
                                         "step 2 EXECUTE dispatch"))
    assert len(rec.contents_batches) == 1     # ONE commit
    path, body = rec.contents_batches[0]
    assert path == "/api/v1/repos/devcake-repos/activity-linear-t-1/contents"
    assert body["message"] == "step 2 EXECUTE dispatch"
    ops = {e["path"]: e for e in body["files"]}
    assert ops["ACTIVITY.md"]["operation"] == "update"
    assert ops["ACTIVITY.md"]["sha"] == "a1"
    assert ops["photo.png"]["operation"] == "create"
    assert ops["old-entry.md"]["operation"] == "delete"      # stale = pruned
    assert "same.md" not in ops               # blob-sha no-op = omitted


def test_push_activity_snapshot_identical_is_a_noop(tmp_path, monkeypatch):
    same = b"steady"
    same_sha = hashlib.sha1(b"blob %d\x00" % len(same) + same).hexdigest()
    rec = _ActivityRecorder(tree_entries=[{"path": "ACTIVITY.md",
                                           "sha": same_sha}])
    prov = _prov(rec, tmp_path, monkeypatch)
    run_coro(prov.push_activity_snapshot(
        "activity-linear-t-1",
        [{"path": "ACTIVITY.md",
          "content_b64": base64.b64encode(same).decode()}], "step 3"))
    assert rec.contents_batches == []         # nothing changed → no commit


def test_activity_credentials_from_service_json(tmp_path, monkeypatch):
    d = tmp_path / "secrets" / "internal_forge"
    d.mkdir(parents=True)
    (d / "service.json").write_text(json.dumps({"activity_ro_token": "ro-tok"}))
    prov = _prov(lambda r: httpx.Response(500), tmp_path, monkeypatch)
    creds = prov.activity_credentials("activity-linear-t-1")
    assert creds.username == "devcake-activity-ro"
    assert creds.token == "ro-tok"
    assert creds.clone_url == \
        "http://gitea:3000/devcake-repos/activity-linear-t-1.git"
    (d / "service.json").write_text(json.dumps({}))
    assert prov.activity_credentials("activity-x") is None


def test_list_activity_repos_prefix_filter_paginated(tmp_path, monkeypatch):
    page1 = ([{"name": "myapp"}, {"name": "skill-store"},
              {"name": "activity-linear-t-1", "size": 12,
               "updated_at": "2026-07-18"}]
             + [{"name": f"op{i}"} for i in range(47)])     # full page of 50
    page2 = [{"name": "activity-linear-t-2"}]
    rec = _ActivityRecorder(org_pages=[page1, page2])
    prov = _prov(rec, tmp_path, monkeypatch)
    repos = run_coro(prov.list_activity_repos())
    assert [r.name for r in repos] == ["activity-linear-t-1",
                                      "activity-linear-t-2"]
    assert repos[0].mission_key == "t-1"


def test_list_repos_walks_past_first_page(tmp_path, monkeypatch):
    """Mission-org list_repos must not stop at limit=50 — page 2+ repos
    remain visible to the admin Repositories surface."""
    page1 = [{"name": f"linear-t-{i}", "html_url": f"http://gitea/r{i}",
              "size": 1, "open_pr_counter": 0, "updated_at": "2026-08-01"}
             for i in range(1, 51)]
    page2 = [{"name": "linear-t-51", "html_url": "http://gitea/r51",
              "size": 2, "open_pr_counter": 1, "updated_at": "2026-08-02"}]
    pages = {1: page1, 2: page2}
    seen_pages = []

    def handler(request):
        assert request.url.path == "/api/v1/orgs/devcake-internal/repos"
        page = int(request.url.params.get("page", "1"))
        seen_pages.append(page)
        return httpx.Response(200, json=pages.get(page, []))

    prov = _prov(handler, tmp_path, monkeypatch)
    repos = run_coro(prov.list_repos())
    assert [r.name for r in repos] == [f"linear-t-{i}" for i in range(1, 52)]
    assert repos[-1].mission_key == "T-51"
    assert repos[-1].open_prs == 1
    assert seen_pages == [1, 2]


def test_delete_activity_repo_guarded(tmp_path, monkeypatch):
    rec = _ActivityRecorder()
    prov = _prov(rec, tmp_path, monkeypatch)
    run_coro(prov.delete_activity_repo("activity-linear-t-1"))
    assert ("DELETE",
            "/api/v1/repos/devcake-repos/activity-linear-t-1") in rec.calls
    assert not any(p.startswith("/api/v1/admin/users")
                   for m, p in rec.calls if m == "DELETE")   # no user purge
    before = len(rec.calls)
    with pytest.raises(ValueError):
        run_coro(prov.delete_activity_repo("skill-store"))
    assert len(rec.calls) == before           # refused with ZERO HTTP


# ── machine-user naming + fail-loud creation (founder report 2026-08-05) ─────


def test_svc_user_never_emits_a_gitea_invalid_username():
    """Gitea rejects CONSECUTIVE hyphens; the 27-char cut can land on one.

    `board-devcake-pmo-missions-<n>` (the ADR-0030 default board) truncated
    to exactly `board-devcake-pmo-missions-`, so every board mission derived
    `svc-board-devcake-pmo-missions--<hash>` → 422 invalid username →
    the machine user was never created and the mission gated."""
    from devcake.adapters.gitea.provision import _svc_user
    from devcake.ports.internal_forge import internal_repo_name

    for n in range(1, 40):
        repo = internal_repo_name("board", f"devcake-pmo/missions#{n}")
        user = _svc_user(repo)
        assert "--" not in user, f"{repo} → {user}"
        assert not user.endswith("-") and len(user) <= 40
    # a stem that only DIFFERS by the stripped hyphen stays a distinct user:
    # the digest rides the full repo name, not the truncated stem
    assert _svc_user("board-devcake-pmo-missions-1") != \
        _svc_user("board-devcake-pmo-missions-10")


def test_invalid_username_fails_loud_instead_of_being_tolerated():
    """422 is BOTH "already exists" (tolerable) and "invalid username" (a
    bug). Swallowing the latter left the confusing 422 to the collaborator
    PUT two calls later ("user does not exist")."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            422, json={"message": "[Username]: invalid username"})

    prov = GiteaProvisioner(url="http://gitea:3000", admin_user="a",
                            admin_password="p",
                            transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError) as e:
        run_coro(prov._req("POST", "/admin/users",
                           tolerate=(409, 422),
                           tolerate_only_if="already exists",
                           json={"username": "svc-bad--name"}))
    assert "invalid username" in str(e.value)
    assert len(seen) == 1


def test_mission_credentials_register_tokens_for_redaction(tmp_path, monkeypatch):
    """ADR-0010 dual registration: mint/load in provision.py registers
    internal:{repo}:w|r so empty token_patterns still redact (factory
    registration alone is not the whole story)."""
    from devcake.security import MASK, redact, unregister_runtime_secret

    _seed_stored(tmp_path)
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    prov = GiteaProvisioner(url="http://gitea:3000", admin_user="a",
                            admin_password="p")
    keys = [f"internal:{REPO}:w", f"internal:{REPO}:r"]
    try:
        creds = prov.mission_credentials(REPO)
        assert creds is not None
        out = redact(f"leak {creds.token_write} and {creds.token_read} end")
        assert creds.token_write not in out and creds.token_read not in out
        assert MASK in out
    finally:
        for key in keys:
            unregister_runtime_secret(key)


def test_existing_user_is_still_tolerated():
    """The idempotent path must keep working — re-provisioning is normal."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"message": "user already exists [name: svc-x]"})

    prov = GiteaProvisioner(url="http://gitea:3000", admin_user="a",
                            admin_password="p",
                            transport=httpx.MockTransport(handler))
    assert run_coro(prov._req("POST", "/admin/users",
                              tolerate=(409, 422),
                              tolerate_only_if="already exists",
                              json={"username": "svc-x"})) is None
