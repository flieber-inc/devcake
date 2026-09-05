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

from devcake.config import AppConfig, RepoInstance, SkillSource
from devcake.domain.repo_mirror import RepoCache, _symref_head_branch

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


class _EmptyTokens:
    """Secrets-store bypass shared by the Local* cards — sync goes
    unauthenticated, like a public repo. ONE copy: the two card kinds must
    not drift when the token seam changes."""

    @property
    def token(self):  # type: ignore[override]
        return ""

    @property
    def token_ro(self):  # type: ignore[override]
        return ""


class LocalRepo(_EmptyTokens, RepoInstance):
    """A config card whose URL is a local origin (file path)."""


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


# ── empty default_branch = "the repository's default" (the card contract) ────

class LocalSkill(_EmptyTokens, SkillSource):
    """A skill-source card over a local origin."""


def _skill_rig(rig):
    origin, cache, _ = rig
    src = LocalSkill.model_construct(name="shelf", url=str(origin),
                                     default_branch="", subdir="")
    cache.config.skill_sources = [src]
    return origin, cache


def test_skill_source_empty_branch_resolves_the_remotes_head(rig):
    """SPA hint 'Empty = the repository's default': HEAD must come from the
    remote's symref — trunk here, NOT a git init default and NEVER the
    invalid `refs/heads/` (which failed every sync after a good fetch)."""
    origin, cache = _skill_rig(rig)
    sh("git", "checkout", "-qb", "trunk", cwd=origin)
    st = run_coro(cache.sync_one("shelf"))
    assert st.ok, st.detail
    head = sh("git", "-C", str(cache.mirror_path("shelf")),
              "symbolic-ref", "HEAD").strip()
    assert head == "refs/heads/trunk"
    assert run_coro(cache.tree_head("shelf"))   # reads pin via mirror HEAD


def test_skill_source_empty_branch_detached_remote_fails_actionably(rig):
    """A remote with no HEAD symref (detached) cannot seed the mirror's
    HEAD — the failure must name the card's Branch field, not surface
    git's bare symbolic-ref refusal."""
    origin, cache = _skill_rig(rig)
    sh("git", "checkout", "-q", "--detach", cwd=origin)
    st = run_coro(cache.sync_one("shelf"))
    assert not st.ok
    assert "default branch" in st.detail and "Branch" in st.detail


def test_symref_head_branch_parser():
    good = "ref: refs/heads/main\tHEAD\n0123abc\tHEAD\n"
    assert _symref_head_branch(good) == "main"
    assert _symref_head_branch("0123abc\tHEAD\n") == ""      # detached
    assert _symref_head_branch("") == ""                     # empty remote
    assert _symref_head_branch("ref: refs/tags/v1\tHEAD\n") == ""
    # branch names may contain slashes
    assert _symref_head_branch(
        "ref: refs/heads/release/2.0\tHEAD\n") == "release/2.0"
    # a clone-shaped remote also advertises refs/remotes/origin/HEAD — its
    # line merely ENDS with HEAD and must never seed the branch
    assert _symref_head_branch(
        "ref: refs/heads/master\trefs/remotes/origin/HEAD\n"
        "0adc0adc\tHEAD\n") == ""




def test_repo_backed_skill_source_reads_the_backing_mirror(rig):
    """ADR-0039: a backed source has NO mirror of its own — reads resolve
    to the backing repo card's bare mirror, and syncing the backed name
    syncs the backing card under ITS lock and ledger entry (never two
    locks over one bare repo)."""
    origin, cache, tmp = _skills_origin(rig)
    src = LocalSkill.model_construct(name="shelf", url="", default_branch="",
                                     subdir="skills", backed_by="alpha")
    cache.config.skill_sources = [src]
    assert cache.mirror_name_of("shelf") == "alpha"
    ok, why = run_coro(cache.ensure_fresh(["shelf"]))
    assert ok, why
    assert not cache.mirror_path("shelf").exists()   # one physical mirror
    assert cache.mirror_path("alpha").is_dir()
    sha = run_coro(cache.tree_head("shelf"))
    assert sha and sha == run_coro(cache.tree_head("alpha"))
    tree = run_coro(cache.read_skill_tree("shelf", "skills", sha))
    assert set(tree) == {"tdd"}
    assert run_coro(cache.read_skill_file(
        "shelf", "skills", sha, "tdd", "SKILL.md"))
    assert cache.has_last_good("shelf")   # resolves to the backing ledger


def test_backed_source_branch_pin_is_honored_and_fails_loud(rig):
    """ADR-0039: a backed source may pin a branch other than the backing
    card's. An EXISTING pin serves that branch from the shared mirror and
    probes it on the remote; a MISSING pin fails both surfaces loudly —
    never a silent fallback onto the backing card's branch."""
    origin, cache, tmp = _skills_origin(rig)
    pinned = LocalSkill.model_construct(
        name="pinned", url="", default_branch="side", subdir="",
        backed_by="alpha")
    broken = LocalSkill.model_construct(
        name="broken", url="", default_branch="missing", subdir="",
        backed_by="alpha")
    cache.config.skill_sources = [pinned, broken]
    assert run_coro(cache.sync_one("alpha")).ok
    side_sha = sh("git", "-C", str(origin), "rev-parse", "side").strip()
    assert run_coro(cache.tree_head("pinned")) == side_sha
    assert run_coro(cache.remote_head("pinned")) == side_sha
    # missing pinned branch: loud None on BOTH surfaces (an own-remote
    # source with the same bad pin also fails; a HEAD fallback would have
    # silently served alpha's main)
    assert run_coro(cache.tree_head("broken")) is None
    assert run_coro(cache.remote_head("broken")) is None


# ── real git: the repository's HEAD is the truth (ADR-0024 addendum) ──────────

def _dev_container_on_path():
    """The provision belt lives in the dev container's package (images/
    common); host checkout and the app container mount it differently."""
    import sys
    from pathlib import Path
    for p in (Path(__file__).parents[2] / "images" / "common",
              Path(__file__).parents[1] / "images" / "common"):
        if p.is_dir():
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
            return
    pytest.skip("images/common not mounted")


def _clone_from_mirror(cache, name, dest):
    sh("git", "clone", "-q", f"file://{cache.mirror_path(name)}", str(dest))
    return dest


def test_blank_card_serves_the_remotes_default_branch(tmp_path):
    """A repository whose default is `trunk` and a blank card: the mirror's
    HEAD follows the remote's HEAD and a file:// clone has content."""
    origin = tmp_path / "origin"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "trunk", cwd=origin)
    (origin / "a.txt").write_text("one\n")
    sh("git", "add", "-A", cwd=origin)
    sh("git", "commit", "-qm", "c1", cwd=origin)
    inst = LocalRepo.model_construct(name="alpha", url=str(origin),
                                     default_branch="")
    cache = RepoCache(AppConfig(pmos=[], repos=[]), Forges([inst]),
                      root=tmp_path / "mirrors")
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok, st.detail
    head = (cache.mirror_path("alpha") / "HEAD").read_text().strip()
    assert head == "ref: refs/heads/trunk"
    assert cache.resolved_branch("alpha") == "trunk"
    assert cache.has_last_good("alpha")
    dest = _clone_from_mirror(cache, "alpha", tmp_path / "ws")
    assert (dest / "a.txt").read_text() == "one\n"
    assert run_coro(cache.remote_default_branch("alpha")) == "trunk"


def test_pin_the_remote_lacks_fails_and_keeps_the_previous_head(rig):
    """After a good sync on `main`, pinning `nope` fails loud, the mirror's
    HEAD still names `main`, and a clone still has content."""
    origin, cache, tmp = rig
    assert run_coro(cache.sync_one("alpha")).ok
    inst = cache.forges.instance("alpha")
    object.__setattr__(inst, "default_branch", "nope")
    st = run_coro(cache.sync_one("alpha"))
    assert not st.ok
    assert "pins 'nope'" in st.detail and "main" in st.detail
    head = (cache.mirror_path("alpha") / "HEAD").read_text().strip()
    assert head == "ref: refs/heads/main"
    assert cache.has_last_good("alpha")          # last-good content survives
    assert cache.resolved_branch("alpha") == "nope"   # the pin is the answer
    dest = _clone_from_mirror(cache, "alpha", tmp / "ws2")
    assert (dest / "a.txt").read_text() == "two\n"


def test_pinned_card_on_an_empty_remote_syncs_green(tmp_path):
    """Zero heads is a legitimate state (a repository awaiting its first
    commit): the pinned sync is green and the clone is empty by
    construction — the provision belt must not fire either."""
    _dev_container_on_path()
    from devcake_dev.workspace.clone import empty_checkout
    origin = tmp_path / "origin"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=origin)
    inst = LocalRepo.model_construct(name="alpha", url=str(origin),
                                     default_branch="main")
    cache = RepoCache(AppConfig(pmos=[], repos=[]), Forges([inst]),
                      root=tmp_path / "mirrors")
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok, st.detail
    assert cache.resolved_branch("alpha") == "main"
    dest = _clone_from_mirror(cache, "alpha", tmp_path / "ws")
    assert dest.is_dir() and not (dest / "a.txt").exists()
    assert empty_checkout(str(cache.mirror_path("alpha")), str(dest)) is False


def test_empty_checkout_detects_a_dangling_head_clone(rig):
    """A mirror with branches whose HEAD names a missing one clones an empty
    tree with exit 0 — the belt says so; a healthy clone reads False."""
    _dev_container_on_path()
    from devcake_dev.workspace.clone import empty_checkout
    origin, cache, tmp = rig
    assert run_coro(cache.sync_one("alpha")).ok
    mirror = cache.mirror_path("alpha")
    good = _clone_from_mirror(cache, "alpha", tmp / "good")
    assert empty_checkout(str(mirror), str(good)) is False
    sh("git", "--git-dir", str(mirror), "symbolic-ref", "HEAD",
       "refs/heads/nope")
    bad = tmp / "bad"
    sh("git", "clone", "-q", f"file://{mirror}", str(bad))
    assert not (bad / "a.txt").exists()
    assert empty_checkout(str(mirror), str(bad)) is True


def test_blank_card_on_an_empty_remote_bootstraps_main(tmp_path):
    """Real git: an empty origin advertises no HEAD symref; a blank card's
    mirror takes `main` and syncs green (the first commit creates it)."""
    from devcake.domain.repo_mirror import BOOTSTRAP_BRANCH
    origin = tmp_path / "origin"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "trunk", cwd=origin)          # no commits
    inst = LocalRepo.model_construct(name="alpha", url=str(origin),
                                     default_branch="")
    cache = RepoCache(AppConfig(pmos=[], repos=[]), Forges([inst]),
                      root=tmp_path / "mirrors")
    st = run_coro(cache.sync_one("alpha"))
    assert st.ok, st.detail
    head = (cache.mirror_path("alpha") / "HEAD").read_text().strip()
    assert head == f"ref: refs/heads/{BOOTSTRAP_BRANCH}"
    # the bootstrapped name is what a Dev's first commit creates: served
    assert cache.resolved_branch("alpha") == BOOTSTRAP_BRANCH
    assert not cache.has_last_good("alpha")             # nothing to serve stale
