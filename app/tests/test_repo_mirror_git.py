"""RepoCache against REAL git (ADR-0024) — the mechanics the fake runner
cannot vouch for: init/fetch/prune behavior, bare-HEAD, and the depth
contract over file:// (plain-path clones IGNORE --depth — the footgun the
entrypoint's mirror_clone_argv exists to avoid).

Runs wherever git exists — the app-test image ships it (app/Dockerfile),
so CI covers this; skipped gracefully elsewhere.
"""
import asyncio
import shutil
import subprocess

import pytest

from devcake.config import AppConfig, RepoInstance
from devcake.domain.repo_mirror import RepoCache

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="no git binary")

# House loop convention: the legacy suites drive coroutines via
# asyncio.get_event_loop().run_until_complete — asyncio.run() would CLOSE and
# UNSET the loop and poison the policy for every later test file. One shared
# loop, left set and open, keeps the whole suite compatible.
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run_coro(c):
    return _LOOP.run_until_complete(c)


def sh(*args, cwd=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       env={"PATH": "/usr/local/bin:/usr/bin:/bin",
                            "HOME": str(cwd or "/tmp"),
                            "GIT_TERMINAL_PROMPT": "0",
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                            "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@x"})
    assert r.returncode == 0, f"{args}: {r.stderr}"
    return r.stdout


class LocalRepo(RepoInstance):
    """A config card whose URL is a local origin (file path) and whose
    tokens are empty — sync goes unauthenticated, like a public repo."""

    @property
    def token(self):  # type: ignore[override]
        return ""

    @property
    def token_ro(self):  # type: ignore[override]
        return ""


class Forges:
    class _F:
        class descriptor:
            clone_user = ""      # empty ⇒ URL unchanged (local path origin)

    def __init__(self, repos):
        self.instances = {r.name: r for r in repos}
        self.internal = set()
        self.breakers = {}
        self.breaker_fields = {}

    def instance(self, name):
        return self.instances.get(name)

    def get(self, name):
        return self._F() if name in self.instances else None

    def latch(self, name, reason, *, credential_field=None):
        self.breakers[name] = reason
        if credential_field is not None:
            self.breaker_fields[name] = credential_field


@pytest.fixture
def rig(tmp_path):
    """origin repo (branch main + side branch + tag) → RepoCache over it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=origin)
    (origin / "a.txt").write_text("one\n")
    sh("git", "add", "-A", cwd=origin)
    sh("git", "commit", "-qm", "c1", cwd=origin)
    (origin / "a.txt").write_text("two\n")
    sh("git", "commit", "-aqm", "c2", cwd=origin)
    sh("git", "branch", "side", cwd=origin)
    sh("git", "tag", "v1", cwd=origin)
    # model_construct: local-path origins fail the URL-shape validator by
    # design — same bypass the internal-repo synthesis uses (dispatch.py)
    inst = LocalRepo.model_construct(name="alpha", url=str(origin),
                                     default_branch="main")
    cfg = AppConfig(pmos=[], repos=[])
    cache = RepoCache(cfg, Forges([inst]), root=tmp_path / "mirrors")
    return origin, cache, tmp_path


def test_sync_builds_a_servable_bare_mirror(rig):
    origin, cache, tmp = rig
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok, st.detail
    m = cache.mirror_path("alpha")
    refs = sh("git", "-C", str(m), "for-each-ref", "--format=%(refname)")
    assert "refs/heads/main" in refs and "refs/heads/side" in refs
    assert "refs/tags/v1" in refs
    assert sh("git", "-C", str(m), "config", "gc.auto").strip() == "0"
    # bare-HEAD contract: file:// clones check out the DEFAULT branch
    assert sh("git", "-C", str(m), "symbolic-ref", "HEAD").strip() \
        == "refs/heads/main"
    work = tmp / "work"
    sh("git", "clone", "-q", f"file://{m}", str(work))
    assert (work / "a.txt").read_text() == "two\n"


def test_resync_picks_up_commits_and_prunes_deleted_branches(rig):
    origin, cache, _ = rig
    assert run_coro(cache.sync_one("alpha")).ok
    (origin / "a.txt").write_text("three\n")
    sh("git", "commit", "-aqm", "c3", cwd=origin)
    sh("git", "branch", "-D", "side", cwd=origin)
    assert run_coro(cache.sync_one("alpha")).ok
    m = cache.mirror_path("alpha")
    refs = sh("git", "-C", str(m), "for-each-ref", "--format=%(refname)")
    assert "refs/heads/side" not in refs               # --prune worked
    assert sh("git", "-C", str(m), "log", "-1", "--format=%s",
              "main").strip() == "c3"


def test_url_change_reinitializes_against_the_new_origin(rig, tmp_path):
    origin, cache, _ = rig
    assert run_coro(cache.sync_one("alpha")).ok
    other = tmp_path / "other"
    other.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=other)
    (other / "b.txt").write_text("other\n")
    sh("git", "add", "-A", cwd=other)
    sh("git", "commit", "-qm", "o1", cwd=other)
    cache.forges.instance("alpha").url = str(other)    # operator edited the card
    assert run_coro(cache.sync_one("alpha")).ok
    m = cache.mirror_path("alpha")
    assert "o1" in sh("git", "-C", str(m), "log", "-1", "--format=%s", "main")


def test_depth_contract_file_url_honors_depth_plain_path_does_not(rig):
    """THE footgun: `git clone --depth 1 /path` silently ignores depth
    (local-transport hardlink copy); file:// forces the smart transport.
    mirror_clone_argv() must therefore always emit file:// for mirrors."""
    origin, cache, tmp = rig
    assert run_coro(cache.sync_one("alpha")).ok
    m = cache.mirror_path("alpha")
    shallow = tmp / "shallow"
    sh("git", "clone", "-q", "--depth", "1", f"file://{m}", str(shallow))
    assert sh("git", "-C", str(shallow), "rev-list",
              "--count", "HEAD").strip() == "1"
    full = tmp / "full"
    sh("git", "clone", "-q", f"file://{m}", str(full))
    assert sh("git", "-C", str(full), "rev-list",
              "--count", "HEAD").strip() == "2"


def test_fetch_during_a_concurrent_clone_smoke(rig):
    """Bare-repo readers survive a racing fetch (append-only packs,
    gc.auto=0). Smoke-scale: one clone while a fetch lands."""
    origin, cache, tmp = rig
    assert run_coro(cache.sync_one("alpha")).ok
    (origin / "a.txt").write_text("four\n")
    sh("git", "commit", "-aqm", "c4", cwd=origin)

    async def race():
        m = cache.mirror_path("alpha")
        clone = asyncio.create_subprocess_exec(
            "git", "clone", "-q", f"file://{m}", str(tmp / "race"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp)})
        proc, st = await asyncio.gather(clone, cache.sync_one("alpha"))
        rc = await proc.wait()
        return rc, st

    rc, st = run_coro(race())
    assert rc == 0 and st.ok


# ── skill-tree reads off the bare mirror (ADR-0016 addendum) ─────────────────

def _skills_origin(rig):
    """Grow the rig's origin a skills tree: valid skill, nested file, an
    in-repo symlink, a hostile dir name, junk without SKILL.md."""
    origin, cache, tmp = rig
    d = origin / "skills"
    (d / "tdd" / "sub").mkdir(parents=True)
    (d / "tdd" / "SKILL.md").write_text(
        "---\nname: tdd\ndescription: Ext\n---\nbody\n")
    (d / "tdd" / "sub" / "deep.md").write_text("deep\n")
    (d / "tdd" / "link.md").symlink_to("SKILL.md")        # mode 120000
    (d / "UPPER-Bad!" ).mkdir()
    (d / "UPPER-Bad!" / "SKILL.md").write_text("hostile dir name\n")
    (d / "junk").mkdir()
    (d / "junk" / "notes.txt").write_text("no SKILL.md\n")
    sh("git", "add", "-A", cwd=origin)
    sh("git", "commit", "-qm", "skills", cwd=origin)
    return origin, cache, tmp


def test_skill_tree_reads_pin_a_sha_and_filter_hostile_content(rig):
    origin, cache, _ = _skills_origin(rig)
    assert run_coro(cache.sync_one("alpha")).ok
    sha = run_coro(cache.tree_head("alpha"))
    assert sha and len(sha) == 40
    tree = run_coro(cache.read_skill_tree("alpha", "skills", sha))
    # symlink entries (mode 120000) and non-regex dir names never surface
    assert set(tree) == {"tdd"}
    assert set(tree["tdd"]) == {"SKILL.md", "sub/deep.md"}
    data = run_coro(cache.read_skill_file(
        "alpha", "skills", sha, "tdd", "SKILL.md"))
    assert data is not None and b"description: Ext" in data
    assert run_coro(cache.read_skill_file(
        "alpha", "skills", sha, "tdd", "missing.md")) is None
    # repo root as subdir="" lists nothing here (skills live under skills/)
    assert run_coro(cache.read_skill_tree("alpha", "", sha)) == {}


def test_skill_reads_follow_upstream_commits_after_resync(rig):
    origin, cache, _ = _skills_origin(rig)
    assert run_coro(cache.sync_one("alpha")).ok
    sha1 = run_coro(cache.tree_head("alpha"))
    (origin / "skills" / "tdd" / "SKILL.md").write_text(
        "---\nname: tdd\n---\nv2\n")
    sh("git", "commit", "-aqm", "bump skill", cwd=origin)
    # the OLD sha still serves the OLD content (pinned reads are stable)
    assert run_coro(cache.sync_one("alpha")).ok
    sha2 = run_coro(cache.tree_head("alpha"))
    assert sha2 != sha1
    old = run_coro(cache.read_skill_file("alpha", "skills", sha1,
                                         "tdd", "SKILL.md"))
    new = run_coro(cache.read_skill_file("alpha", "skills", sha2,
                                         "tdd", "SKILL.md"))
    assert b"body" in old and b"v2" in new
