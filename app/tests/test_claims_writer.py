"""Production ClaimsWriter — forge-neutral (PLAN_MEMORY §5.3).

Public seam: make_claims_writer / ClaimsWriter. A card with a write token
is writable on github, gitlab, and gitea. reference_only is not. The
writer must not require the bundled Gitea provisioner.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
import pytest

from devcake.config import AppConfig, RepoInstance
from devcake.adapters.claims_writer import ClaimsWriter, make_claims_writer
from devcake.adapters.git import GitResult


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def _git_result(rc=0, stdout="", stderr=""):
    return GitResult(rc, stdout, stderr)


@pytest.fixture
def tokens(monkeypatch):
    """GUI secret store stand-in: {(card, field): value}."""
    store: dict[tuple[str, str], str] = {}

    def read(scope, name, field):
        return store.get((name, field), "")

    monkeypatch.setattr("devcake.secrets.read_connection_secret", read)
    return store


def _cfg(*cards):
    return AppConfig(repos=list(cards))


def test_can_write_every_forge_when_the_card_has_a_write_token(tokens):
    tokens[("gh", "token")] = "ghp_write"
    tokens[("gl", "token")] = "glpat-write"
    tokens[("gt", "token")] = "40hexwrite"
    cards = [
        RepoInstance(name="gh", forge="github",
                     url="https://github.com/acme/notes"),
        RepoInstance(name="gl", forge="gitlab",
                     url="https://gitlab.com/acme/notes"),
        RepoInstance(name="gt", forge="gitea",
                     url="https://git.example.com/acme/notes"),
    ]
    w = make_claims_writer(_cfg(*cards))
    for c in cards:
        assert w.can_write(c.name) is True, c.forge


def test_can_write_false_for_reference_only_missing_and_empty(tokens):
    tokens[("ro", "token_ro")] = "ghp_ro"
    w = make_claims_writer(_cfg(
        RepoInstance(name="ro", forge="github",
                     url="https://github.com/acme/docs"),
        RepoInstance(name="bare", forge="github",
                     url="https://github.com/acme/bare"),
    ))
    assert w.can_write("ro") is False
    assert w.can_write("bare") is False
    assert w.can_write("nosuch") is False


def test_make_claims_writer_does_not_need_bundled_gitea():
    w = make_claims_writer(AppConfig())
    assert w.can_write("x") is False
    # not the Null that raises a "not configured" blanket
    with pytest.raises(Exception) as ei:
        run_coro(w.commit("nosuch", creates={".claims/a.json": "{}"},
                          deletes=[], message="x"))
    assert "not configured" not in str(ei.value).lower() or "nosuch" in str(ei.value)


def test_commit_refuses_paths_outside_claims(tokens):
    tokens[("nb", "token")] = "tok"
    w = make_claims_writer(_cfg(
        RepoInstance(name="nb", forge="github",
                     url="https://github.com/acme/nb")))
    with pytest.raises(ValueError, match=r"\.claims"):
        run_coro(w.commit("nb", creates={"notes/secret.md": "no"},
                          deletes=[], message="x"))
    with pytest.raises(ValueError, match=r"\.claims"):
        run_coro(w.commit("nb", creates={},
                          deletes=["README.md"], message="x"))


@pytest.mark.skipif(shutil.which("git") is None, reason="no git binary")
def test_round_trip_create_list_prune_on_local_origin(tmp_path, tokens):
    """Any forge card whose URL is a real git remote: write, list meta,
    prune — no Gitea provisioner, no Contents API."""
    work = tmp_path / "work"
    work.mkdir()
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
           "HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}

    def sh(*args, cwd=work):
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        return r.stdout

    sh("git", "init", "-b", "main")
    (work / "README").write_text("notebook\n")
    sh("git", "add", "README")
    sh("git", "commit", "-m", "init")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(origin)],
                   check=True, capture_output=True, env=env)
    # file://localhost/… satisfies RepoInstance URL shape (host + owner/repo)
    url = "file://localhost" + str(origin)

    tokens[("nb", "token")] = "unused-for-local"
    card = RepoInstance(name="nb", forge="github", url=url,
                        default_branch="main")
    w = make_claims_writer(_cfg(card))
    assert w.can_write("nb") is True

    rec = {"id": "abc123456789abcd", "source_instance": "eng",
           "source_pmo_id": "pmo-1", "finding": "must not be listed as body"}
    run_coro(w.commit(
        "nb",
        creates={".claims/README.md": "leads\n",
                 ".claims/abc123456789abcd.json": json.dumps(rec) + "\n"},
        deletes=[],
        message="devcake:claims:v1 run=r1 n=1"))

    names = run_coro(w.list_json_names("nb"))
    assert names == ["abc123456789abcd.json"]
    assert run_coro(w.has_readme("nb")) is True
    meta = run_coro(w.list_claim_meta("nb"))
    assert meta == [{"id": "abc123456789abcd",
                     "source_instance": "eng",
                     "source_pmo_id": "pmo-1"}]
    # listing must not expose finding text
    assert all("finding" not in m for m in meta)

    run_coro(w.commit(
        "nb", creates={},
        deletes=[".claims/abc123456789abcd.json"],
        message="devcake:claims:v1 prune source=eng"))
    assert run_coro(w.list_json_names("nb")) == []
    assert run_coro(w.has_readme("nb")) is True
    shown = subprocess.run(
        ["git", "--git-dir", str(origin), "show", "HEAD:.claims/README.md"],
        capture_output=True, text=True, env=env)
    assert shown.returncode == 0 and shown.stdout == "leads\n"
    gone = subprocess.run(
        ["git", "--git-dir", str(origin), "show",
         "HEAD:.claims/abc123456789abcd.json"],
        capture_output=True, text=True, env=env)
    assert gone.returncode != 0


@pytest.mark.skipif(shutil.which("git") is None, reason="no git binary")
def test_commit_retries_once_after_push_race(tmp_path, tokens):
    """R5: a commit landing between our checkout and push rejects the
    push — the writer replays the whole cycle once onto the fresh head
    (creates/deletes are idempotent), so the batch is not lost."""
    from devcake.adapters.claims_writer import ClaimsWriter
    from devcake.adapters.git import run_git
    work = tmp_path / "work"
    work.mkdir()
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
           "HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    subprocess.run(["git", "init", "-b", "main"], cwd=work, check=True,
                   capture_output=True, env=env)
    (work / "README").write_text("nb\n")
    subprocess.run(["git", "add", "README"], cwd=work, check=True,
                   capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=work, check=True,
                   capture_output=True, env=env)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(origin)],
                   check=True, capture_output=True, env=env)

    tokens[("nb", "token")] = "unused-for-local"
    card = RepoInstance(name="nb", forge="github",
                        url="file://localhost" + str(origin),
                        default_branch="main")

    pushes = {"n": 0}

    class Fail:
        returncode, stdout, stderr = 1, "", "rejected (fetch first)"

    async def flaky_git(args, cwd=None, env=None):
        if args and args[0] == "push":
            pushes["n"] += 1
            if pushes["n"] == 1:
                return Fail()          # the race: another commit landed
        return await run_git(args, cwd=cwd, env=env)

    w = ClaimsWriter(_cfg(card), git=flaky_git)
    run_coro(w.commit(
        "nb", creates={".claims/aa11bb22cc33dd44.json": "{}\n"},
        deletes=[], message="devcake:claims:v1 run=r1 n=1"))
    assert pushes["n"] == 2
    assert run_coro(w.list_json_names("nb")) == ["aa11bb22cc33dd44.json"]
    snap = run_coro(w.snapshot("nb"))
    assert snap == {"json_names": ["aa11bb22cc33dd44.json"],
                    "has_readme": False}


def test_clone_failure_returns_unlistable_not_empty(tokens):
    """Auth/network/missing-repo clone failure must surface as None
    (unlistable), never definite-empty from a local empty-init tree."""
    tokens[("nb", "token")] = "bad-token"
    card = RepoInstance(name="nb", forge="github",
                        url="https://github.com/acme/notes",
                        default_branch="main")

    async def failing_git(args, cwd=None, env=None):
        # clone + ls-remote fail (outage). init/remote succeed so that
        # today's buggy empty-init path would still produce a tree —
        # without the fix, reads would return []/False instead of None.
        if args[:1] == ["clone"]:
            return _git_result(128, stderr="Authentication failed")
        if args[:2] == ["ls-remote", "--heads"]:
            return _git_result(128, stderr="Could not read from remote")
        if args[:1] == ["init"] or args[:2] == ["remote", "add"]:
            return _git_result(0)
        return _git_result(1, stderr=f"unexpected git {args!r}")

    w = ClaimsWriter(_cfg(card), git=failing_git)
    assert run_coro(w.list_json_names("nb")) is None
    assert run_coro(w.snapshot("nb")) is None
    assert run_coro(w.has_readme("nb")) is None
    assert run_coro(w.list_claim_meta("nb")) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="no git binary")
def test_genuine_empty_remote_empty_inits_and_first_commit_works(
        tmp_path, tokens):
    """Clone --branch fails on a reachable remote with no heads, but
    ls-remote succeeds empty → definite-empty reads + first push OK."""
    origin = tmp_path / "origin.git"
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
           "HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True, env=env)
    # No commits → no heads. Clone --branch fails; ls-remote --heads is empty.
    url = "file://localhost" + str(origin)
    tokens[("nb", "token")] = "unused-for-local"
    card = RepoInstance(name="nb", forge="github", url=url,
                        default_branch="main")
    w = make_claims_writer(_cfg(card))

    assert run_coro(w.list_json_names("nb")) == []
    assert run_coro(w.has_readme("nb")) is False
    assert run_coro(w.snapshot("nb")) == {"json_names": [], "has_readme": False}
    assert run_coro(w.list_claim_meta("nb")) == []

    run_coro(w.commit(
        "nb",
        creates={".claims/aabbccddeeff0011.json": "{}\n"},
        deletes=[],
        message="devcake:claims:v1 run=r1 n=1"))
    assert run_coro(w.list_json_names("nb")) == ["aabbccddeeff0011.json"]


def test_default_branch_exists_but_clone_fails_is_unlistable(tokens):
    """ls-remote shows refs/heads/<default> yet clone failed → None.
    Must not empty-init over a remote that already has the branch."""
    tokens[("nb", "token")] = "tok"
    card = RepoInstance(name="nb", forge="github",
                        url="https://github.com/acme/notes",
                        default_branch="main")

    async def flaky_clone_git(args, cwd=None, env=None):
        if args[:1] == ["clone"]:
            return _git_result(128, stderr="early EOF / transient")
        if args[:2] == ["ls-remote", "--heads"]:
            return _git_result(
                0, stdout="abc123\trefs/heads/main\ndef456\trefs/heads/dev\n")
        if args[:1] == ["init"] or args[:2] == ["remote", "add"]:
            return _git_result(0)
        return _git_result(1, stderr=f"unexpected git {args!r}")

    w = ClaimsWriter(_cfg(card), git=flaky_clone_git)
    assert run_coro(w.list_json_names("nb")) is None
    assert run_coro(w.snapshot("nb")) is None
    assert run_coro(w.has_readme("nb")) is None
    assert run_coro(w.list_claim_meta("nb")) is None


def test_branch_missing_on_populated_remote_is_unlistable(tokens):
    """ls-remote shows other heads but not the configured branch → None.
    A typo'd default_branch must never read as a confidently empty
    notebook; empty-init is reserved for a remote with no heads at all."""
    tokens[("nb", "token")] = "tok"
    card = RepoInstance(name="nb", forge="github",
                        url="https://github.com/acme/notes",
                        default_branch="main")

    async def wrong_branch_git(args, cwd=None, env=None):
        if args[:1] == ["clone"]:
            return _git_result(128, stderr="Remote branch main not found")
        if args[:2] == ["ls-remote", "--heads"]:
            return _git_result(
                0, stdout="abc123\trefs/heads/master\ndef456\trefs/heads/dev\n")
        if args[:1] == ["init"] or args[:2] == ["remote", "add"]:
            return _git_result(0)
        return _git_result(1, stderr=f"unexpected git {args!r}")

    w = ClaimsWriter(_cfg(card), git=wrong_branch_git)
    assert run_coro(w.list_json_names("nb")) is None
    assert run_coro(w.snapshot("nb")) is None
    assert run_coro(w.has_readme("nb")) is None
    assert run_coro(w.list_claim_meta("nb")) is None
