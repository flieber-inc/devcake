"""Repo source mirror (ADR-0024) — bare git mirrors of every configured
repo on the shared `/mirrors` volume, maintained by the app, served
read-only to Dev containers.

MANDATORY, no toggle (founder decision 2026-08-03): a run whose repos are
configured cards clones from the mirror, full stop. A successful sync is a
FAIL-CLOSED dispatch precondition — a mission whose mirrors cannot be
freshened does not dispatch this cycle (no container, no attempt burned;
reason on the missions row + /health) and retries next poll. Principled
exceptions, not operator choices: internal-forge synthesized repos and
activity repos stay direct — their isolation lives in per-mission token
scope (ADR-0010, docs/14 §2 Zone B) and mirroring them onto a
deployment-shared volume would dissolve exactly that boundary.

Zone A domain: async, no HTTP; git rides the single subprocess seam
(adapters/git.py). No vendor imports (F1).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from ..adapters.git import GIT_TIMEOUT_SECONDS, run_git
from ..security import redact

log = logging.getLogger("devcake.mirror")

SYNC_CONCURRENCY = 4     # git subprocesses, not HTTP probes — lower than the
#                          forge sweep's PROBE_CONCURRENCY on purpose

# Auth wording that latches the per-repo forge breaker. Pinned mirror of the
# Dev-side clone_error_class markers (workspace/clone.py) MINUS
# "repository not found": on the sync path a 404 is a deleted/renamed repo —
# an operator config problem — and the breaker's remediation copy says
# "update the token", which would mislead. Everything unrecognized is
# transient (fail toward retry; the breaker is the sharp edge — docs/15 §4
# asymmetry). Guard: app/tests/test_mirror_auth_markers_pin.py (ADR-0034).
_AUTH_MARKERS = ("authentication failed", "could not read username",
                 "could not read password", "invalid credentials",
                 "returned error: 403", "returned error: 401",
                 "write access to repository not granted")

_ASKPASS = Path("/tmp/devcake-mirror-askpass.sh")
# App-container-local ON PURPOSE (security audit 2026-08-03): /mirrors is
# Dev-readable, and app operational files must not ride the shared mount.
_ASKPASS_BODY = "#!/bin/sh\necho \"$DEVCAKE_MIRROR_TOKEN\"\n"


def sync_error_class(stderr: str) -> str:
    """"auth" latches the repo breaker; anything else is "transient"."""
    lowered = (stderr or "").lower()
    return "auth" if any(m in lowered for m in _AUTH_MARKERS) else "transient"


def _symref_head_branch(stdout: str) -> str:
    """Branch name from `ls-remote --symref <url> HEAD` output ("" when the
    remote has no HEAD symref — an empty repository, or a server that omits
    the capability). Only `refs/heads/*` counts: a detached or non-branch
    HEAD cannot seed a mirror's HEAD."""
    for line in (stdout or "").splitlines():
        if line.startswith("ref: refs/heads/") and line.rstrip().endswith("HEAD"):
            return line[len("ref: refs/heads/"):].split("\t", 1)[0].strip()
    return ""


class MirrorStatus:
    """Ledger row for one mirror. Plain object — /health serializes as_dict."""
    __slots__ = ("ok", "synced_at", "attempted_at", "detail", "auth")

    def __init__(self, ok=False, synced_at=None, attempted_at=None,
                 detail="", auth=False):
        self.ok, self.synced_at, self.attempted_at = ok, synced_at, attempted_at
        self.detail, self.auth = detail, auth

    def as_dict(self) -> dict:
        return {"ok": self.ok,
                "synced_at": self.synced_at.isoformat() if self.synced_at else None,
                "attempted_at": (self.attempted_at.isoformat()
                                 if self.attempted_at else None),
                "detail": self.detail, "auth": self.auth}


class RepoCache:
    """ONE instance per app process (built in api/main, injected into every
    manager like ForgeRuntime). All decisions here; the git seam is injected
    for fake-runner tests."""

    def __init__(self, config, forges,
                 root: Path | None = None, git=run_git,
                 clone_user_of=None):
        self.config = config       # shared identity — hot reload mutates in place
        self.forges = forges
        # skill sources are NOT repo cards (2026-08-14 ruling): they have
        # no live forge adapter, so the clone user comes from the forge
        # DESCRIPTOR via this injected resolver (composition root wires
        # the adapter registry; domain stays adapter-free)
        self.clone_user_of = clone_user_of or (lambda forge_id: "")
        self.root = root if root is not None else Path(
            os.environ.get("DEVCAKE_MIRRORS_DIR", "/mirrors"))
        self.git = git
        self.ledger: dict[str, MirrorStatus] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.volume_error: str | None = None
        self._monotonic = time.monotonic   # injectable for freshness tests
        # freshness bookkeeping rides monotonic time; synced_at (wall) is for
        # humans on /health only
        self._synced_mono: dict[str, float] = {}

    # ── identity / eligibility (pure) ────────────────────────────────────────

    def mirror_path(self, name: str) -> Path:
        return self.root / f"{name}.git"

    def _skill_source(self, name: str):
        for x in getattr(self.config, "skill_sources", None) or []:
            if x.name == name:
                return x
        return None

    def eligible(self, name: str) -> bool:
        """A configured repo card (registered in the runtime and NOT an
        internal synthesized repo — token-scope isolation, module
        docstring) — or a dedicated skill source (2026-08-14 ruling:
        skill sources always ride the mirror; they are never cloned into
        workspaces)."""
        return ((name in self.forges.instances
                 and name not in self.forges.internal)
                or self._skill_source(name) is not None)

    def has_last_good(self, name: str) -> bool:
        """Open-mode stale_cache precondition (ADR-0035 / PLAN_MEMORY §3.5).

        True only when a prior successful sync left branch content — the
        in-process ledger recorded a success, or the on-disk bare mirror
        still has heads (loose ``refs/heads/*`` or a ``packed-refs``
        heads line — survives process restart and ADR-0024 offline
        ``git gc``). A bare `git init` dir alone (fetch never succeeded)
        is False so callers omit rather than proceeding on empty heads.
        Do NOT use ``mirror_path.is_dir`` as a stand-in: that is true
        after a failed first sync too.
        """
        st = self.ledger.get(name)
        if st is not None and st.synced_at is not None:
            return True
        if name in self._synced_mono:
            return True
        mirror = self.mirror_path(name)
        heads = mirror / "refs" / "heads"
        try:
            if heads.is_dir() and any(heads.iterdir()):
                return True
        except OSError:
            return False
        # After offline `git gc` (ADR-0024), heads may live only in packed-refs.
        packed = mirror / "packed-refs"
        try:
            if not packed.is_file():
                return False
            for line in packed.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("^"):
                    continue
                # "<sha> <ref>" — any refs/heads/* counts as branch content
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                    return True
        except OSError:
            return False
        return False

    def needed_for(self, *, work_repo: str, mission_type: str, instance,
                   blocker_entries: list[dict],
                   dev_type=None, config=None) -> list[str]:
        """The mirror-eligible repo set one run must have fresh (docs/07 §5a).
        Sourcing comes from THE shared rule (repo_sourcing.sourced_repo_names,
        ADR-0034) — the gate and the runspec structurally cannot disagree
        about which repos ride the mirror; this method only applies the
        mirror-eligibility filter."""
        from .repo_sourcing import sourced_repo_names
        return [name for name in sourced_repo_names(
                    work_repo=work_repo, mission_type=mission_type,
                    instance=instance, blocker_entries=blocker_entries,
                    dev_type=dev_type, config=config)
                if self.eligible(name)]

    # ── the gate primitive ───────────────────────────────────────────────────

    def _fresh(self, name: str, t_entry: float) -> bool:
        st = self.ledger.get(name)
        if st is None or not st.ok:
            return False
        mono = self._synced_mono.get(name)
        if mono is None:
            return False
        max_age = self.config.repo_mirror.sync_max_age_seconds
        if max_age == 0:
            # a sync that COMPLETED after the caller asked satisfies "sync
            # before every dispatch" — this is what lets concurrent waiters
            # coalesce onto one fetch instead of queueing N identical ones
            return mono >= t_entry
        return (self._monotonic() - mono) <= max_age

    async def ensure_fresh(self, names) -> tuple[bool, dict[str, str]]:
        """Sync every named mirror unless already fresh. (all_ok, {name:
        reason}) — reasons only for failures. NEVER raises; the caller's
        poll segment must survive anything this does."""
        names = list(names)
        if not names:
            return True, {}
        t_entry = self._monotonic()
        failures: dict[str, str] = {}
        if self.volume_error:
            return False, {n: f"mirror volume: {self.volume_error}"
                           for n in names}
        sem = asyncio.Semaphore(SYNC_CONCURRENCY)

        async def _one(name: str) -> None:
            try:
                async with sem:
                    lock = self._locks.setdefault(name, asyncio.Lock())
                    async with lock:
                        if self._fresh(name, t_entry):
                            return                    # coalesced onto a peer's sync
                        st = await self.sync_one(name)
                    if not st.ok:
                        failures[name] = st.detail or "sync failed"
            except Exception as e:  # noqa: BLE001 — the gate reports, never raises
                log.exception("mirror sync of %r raised", name)
                failures[name] = f"internal: {type(e).__name__}: {str(e)[:120]}"

        await asyncio.gather(*(_one(n) for n in names))
        return (not failures), failures

    # ── sync ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _origin_url(url: str, clone_user: str) -> str:
        """Embed ``clone_user@`` after the scheme for http(s) remotes that
        lack an authority user. Shared by ``sync_one`` and ``remote_head``
        so scheme coverage cannot drift (ADR-0034)."""
        if not clone_user or "://" not in url:
            return url
        authority = url.split("://", 1)[1].split("/", 1)[0]
        if "@" in authority:
            return url
        return url.replace("://", f"://{clone_user}@", 1)

    async def sync_one(self, name: str) -> MirrorStatus:
        """One init-or-fetch. Caller holds the name's lock."""
        inst = self.forges.instance(name)
        forge = self.forges.get(name)
        now = datetime.now(timezone.utc)
        if inst is None or forge is None:
            # second namespace: a dedicated skill source (no adapter)
            inst, forge = self._skill_source(name), None
            if inst is None or not inst.configured:
                st = MirrorStatus(ok=False, attempted_at=now,
                                  detail="repo is no longer configured")
                self.ledger[name] = st
                return st
        clone_user = (forge.descriptor.clone_user if forge is not None
                      else self.clone_user_of(inst.forge))
        expected_url = self._origin_url(inst.url, clone_user)
        p = self.mirror_path(name)
        env = self._git_env(inst)

        async def fail(detail: str) -> MirrorStatus:
            clean = redact(" ".join(detail.split()))[-500:]
            auth = sync_error_class(clean) == "auth"
            st = MirrorStatus(ok=False, attempted_at=now, detail=clean,
                              auth=auth,
                              synced_at=self.ledger.get(name).synced_at
                              if name in self.ledger else None)
            self.ledger[name] = st
            if auth:
                # Mirror sync is read-preferred (token_ro or token) — same
                # rule as memory mounts. Key the latch so a write-token
                # probe cannot clear a dead read credential (CAKE-118).
                field = ("token_ro" if getattr(inst, "token_ro", None)
                         else "token")
                self.forges.latch(
                    name, f"mirror sync: {clean[:200]}",
                    credential_field=field)
            log.warning("mirror sync failed for %s: %s", name, clean[:200])
            return st

        # init-or-verify
        if not p.is_dir():
            for args in (["init", "--bare", str(p)],
                         ["-C", str(p), "remote", "add", "origin", expected_url],
                         ["-C", str(p), "config", "gc.auto", "0"]):
                r = await self.git(args, env=env)
                if r.returncode != 0:
                    return await fail(f"{' '.join(args[:3])}: {r.stderr}")
        else:
            r = await self.git(["-C", str(p), "remote", "get-url", "origin"],
                               env=env)
            if r.returncode != 0 or r.stdout.strip() != expected_url:
                # URL changed (or corrupt config): the mirror is for a
                # DIFFERENT repo now — rebuild rather than fetch into it
                log.info("mirror %s: remote changed — re-initializing", name)
                self.delete_mirror(name)
                if p.is_dir():
                    # AUD-013: delete_mirror is best-effort (rename/rmtree can
                    # fail on a locked/permission-odd dir). Recursing while the
                    # stale dir persists would loop forever on the same
                    # mismatch — fail loud once instead; the ledger + /health
                    # name it and dispatch stays gated on this repo.
                    return await fail(
                        f"mirror rebuild blocked: could not remove stale "
                        f"mirror dir for {name} (its origin URL changed)")
                return await self.sync_one(name)

        # fetch — heads+tags ONLY (never +refs/*: GitHub's refs/pull/* would
        # double disk for content no Dev clone ever takes)
        r = await self.git(["-C", str(p), "fetch", "--prune", "origin",
                            "+refs/heads/*:refs/heads/*",
                            "+refs/tags/*:refs/tags/*"], env=env)
        if r.returncode != 0:
            return await fail(f"fetch: {r.stderr or r.stdout}"
                              + (" (timeout)" if r.timed_out else ""))

        # HEAD — load-bearing: a bare init defaults HEAD to main/master; a
        # wrong HEAD makes `git clone file://…` check out nothing
        branch = (inst.default_branch or "").strip()
        if not branch:
            # The card contract is "empty branch = the repository's default"
            # (skill sources default to empty; SPA hint says so) — resolve it
            # from the remote's HEAD symref. Never write the empty ref: git
            # refuses `refs/heads/` and the sync would fail every cycle.
            r = await self.git(["ls-remote", "--symref", expected_url, "HEAD"],
                               env=env)
            if r.returncode != 0:
                # a probe ERROR keeps its own stderr — an auth failure must
                # latch the breaker, not read as "set Branch on the card"
                return await fail(f"default-branch probe: "
                                  f"{r.stderr or r.stdout}"
                                  + (" (timeout)" if r.timed_out else ""))
            branch = _symref_head_branch(r.stdout)
            if not branch:
                return await fail(
                    "default branch: the card's branch is empty and the "
                    "remote's HEAD does not name one (empty repository or "
                    "detached HEAD) — set Branch on the card")
        r = await self.git(["-C", str(p), "symbolic-ref", "HEAD",
                            f"refs/heads/{branch}"], env=env)
        if r.returncode != 0:
            return await fail(f"symbolic-ref: {r.stderr}")

        if self.config.repo_mirror.lfs:
            r = await self.git(["-C", str(p), "lfs", "fetch", "origin",
                                branch], env=env)
            if r.returncode != 0:
                return await fail(f"lfs fetch: {r.stderr or r.stdout}")

        st = MirrorStatus(ok=True, synced_at=now, attempted_at=now)
        self.ledger[name] = st
        self._synced_mono[name] = self._monotonic()
        return st

    async def remote_head(self, name: str) -> str | None:
        """Best-effort branch head straight from the forge — provenance for
        mirror-INELIGIBLE cards (bundled Gitea), whose `tree_head` has no
        mirror to read (PLAN_MEMORY §3.6). One `ls-remote` with the card's
        read token via askpass; None on any failure."""
        inst = self.forges.instance(name)
        forge = self.forges.get(name)
        if inst is None or forge is None:
            inst, forge = self._skill_source(name), None
        if inst is None or not (inst.url or "").strip():
            return None
        url = inst.url.strip()
        clone_user = (getattr(forge.descriptor, "clone_user", "") or ""
                      if forge is not None else self.clone_user_of(inst.forge))
        url = self._origin_url(url, clone_user)
        ref = (f"refs/heads/{inst.default_branch}"
               if inst.default_branch else "HEAD")
        r = await self.git(["ls-remote", url, ref], env=self._git_env(inst))
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None
        return r.stdout.split()[0]

    def _git_env(self, inst) -> dict[str, str]:
        token = inst.token_ro or inst.token   # read-preferred, same rule as
        #                                       runspec stage scoping
        if not token:
            return {}                          # public repo: unauthenticated
        self._ensure_askpass()
        return {"GIT_ASKPASS": str(_ASKPASS), "DEVCAKE_MIRROR_TOKEN": token}

    def _ensure_askpass(self) -> None:
        if not _ASKPASS.exists():
            _ASKPASS.write_text(_ASKPASS_BODY)
            _ASKPASS.chmod(stat.S_IRWXU)

    # ── skill-tree reads (ADR-0016 addendum) ─────────────────────────────────
    # External skills ride THIS mirror read-side: no second cache, no
    # workspace clone. Reads pin a sha first, then read from it — atomic vs
    # a concurrent fetch. Freshness is the DISPATCH GATE's job (needed-set
    # union); these reads never sync.

    async def tree_head(self, name: str) -> str | None:
        """The commit skill reads pin: refs/heads/<default_branch> in the
        bare mirror, falling back to the mirror's HEAD. None = unresolvable
        (mirror absent / branch missing) — the caller warns and skips."""
        inst = self.forges.instance(name) or self._skill_source(name)
        p = self.mirror_path(name)
        refs = ([f"refs/heads/{inst.default_branch}"]
                if inst is not None and inst.default_branch else []) + ["HEAD"]
        for ref in refs:
            r = await self.git(["-C", str(p), "rev-parse", "--verify",
                                f"{ref}^{{commit}}"])
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        return None

    async def read_skill_tree(self, name: str, subdir: str,
                              sha: str) -> dict[str, dict[str, object]]:
        """{skill_name: {rel_path: size}} for every skill dir under `subdir`
        at `sha` — a skill dir is a direct child matching SKILL_NAME_RE that
        contains SKILL.md (the store rule). Only blob modes 100644/100755
        count: mode-120000 symlink entries are SKIPPED (third-party content;
        a symlink target must never be followed) and dir names outside the
        skill regex never surface. Sizes come from `ls-tree -r -l` so the
        caller can apply payload caps BEFORE reading content."""
        import re as _re
        from .skills import SKILL_NAME_RE
        skill_re = _re.compile(rf"{SKILL_NAME_RE}$")
        p = self.mirror_path(name)
        spec = f"{sha}:{subdir}" if subdir else sha
        r = await self.git(["-C", str(p), "ls-tree", "-r", "-l", spec])
        if r.returncode != 0:
            return {}
        out: dict[str, dict[str, object]] = {}
        for line in r.stdout.splitlines():
            # "<mode> blob <sha>\t<size>\t<path>" (ls-tree -l pads size)
            try:
                meta, path = line.split("\t", 1) if "\t" in line else (line, "")
                parts = meta.split()
                mode, otype, size = parts[0], parts[1], int(parts[-1])
            except (ValueError, IndexError):
                continue
            if otype != "blob" or mode not in ("100644", "100755"):
                continue
            top, _, rest = path.partition("/")
            if not rest or not skill_re.fullmatch(top):
                continue
            out.setdefault(top, {})[rest] = size
        return {k: v for k, v in out.items() if "SKILL.md" in v}

    async def read_skill_file(self, name: str, subdir: str, sha: str,
                              skill: str, rel_path: str) -> bytes | None:
        """One blob, verbatim bytes (`git show sha:path`). None on any
        failure — the payload builder warns and drops the file, additive
        doctrine (the gate already proved the mirror fresh)."""
        p = self.mirror_path(name)
        prefix = f"{subdir}/" if subdir else ""
        r = await self.git(["-C", str(p), "show",
                            f"{sha}:{prefix}{skill}/{rel_path}"])
        return r.raw_stdout if r.returncode == 0 else None

    # ── background warm-up ───────────────────────────────────────────────────

    async def warm_all(self) -> None:
        """ensure_fresh over every eligible card; results live in the ledger.
        Background task only (poll loop start) — NEVER awaited by boot; a
        dispatch needing repo X coalesces onto the in-flight sync."""
        names = ([r.name for r in self.config.repos
                  if r.configured and self.eligible(r.name)]
                 + [x.name for x in
                    getattr(self.config, "skill_sources", None) or []
                    if x.configured])
        if names:
            await self.ensure_fresh(names)
            log.info("mirror warm-up finished: %d ok / %d total",
                     sum(1 for n in names
                         if self.ledger.get(n) and self.ledger[n].ok),
                     len(names))

    # ── hygiene / surfaces ───────────────────────────────────────────────────

    def verify_writable(self) -> None:
        """Boot probe, no network: is /mirrors there and app-writable? A
        first-touched-by-the-wrong-container volume is root-owned and every
        sync would fail confusingly — /health names the one-line fix."""
        probe = self.root / ".devcake-writable"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe.write_text("")
            probe.unlink()
            self.volume_error = None
        except OSError as e:
            self.volume_error = (
                f"{type(e).__name__}: {e} — the mirrors volume is not "
                f"writable by the app (uid 1000). Fix: stop the stack, "
                f"`docker volume rm devcake_mirrors`, start the stack.")
            log.error("mirror volume unusable: %s", self.volume_error)

    def delete_mirror(self, name: str) -> None:
        """Best-effort removal (repo card deleted / re-init). Rename-aside
        first so an in-flight Dev clone keeps its inode; then remove."""
        p = self.mirror_path(name)
        if not p.exists():
            return
        stale = p.with_name(f"{p.name}.stale-{int(time.time())}")
        try:
            p.rename(stale)
            shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            log.exception("could not remove mirror %s", name)
        self.ledger.pop(name, None)
        self._synced_mono.pop(name, None)
        self._locks.pop(name, None)

    def rename_mirror(self, old: str, new: str) -> None:
        """Migrate on-disk mirror + ledger key with a repo card rename.

        Best-effort: missing source is a no-op; an existing destination is
        removed first so the renamed card owns that identity. In-flight
        Dev clones keep their inode via rename (same volume).
        """
        if old == new:
            return
        src = self.mirror_path(old)
        dst = self.mirror_path(new)
        if dst.exists():
            self.delete_mirror(new)
        if src.exists():
            try:
                src.rename(dst)
            except OSError:
                log.exception("could not rename mirror %s → %s", old, new)
                return
        if old in self.ledger:
            self.ledger[new] = self.ledger.pop(old)
        if old in self._synced_mono:
            self._synced_mono[new] = self._synced_mono.pop(old)
        lock = self._locks.pop(old, None)
        if lock is not None:
            self._locks[new] = lock

    def health_map(self) -> dict:
        return {name: st.as_dict() for name, st in sorted(self.ledger.items())}

    def disk_stats(self) -> dict | None:
        try:
            sv = os.statvfs(self.root)
            return {"total_bytes": sv.f_frsize * sv.f_blocks,
                    "free_bytes": sv.f_frsize * sv.f_bavail}
        except OSError:
            return None


class NullRepoCache:
    """Test/default stand-in: everything is fresh, nothing is mirrored
    beyond a deterministic path. Keeps the existing suite untouched."""

    def __init__(self):
        self.volume_error = None
        self.ledger = {}

    def mirror_path(self, name: str) -> Path:
        return Path(f"/mirrors/{name}.git")

    def eligible(self, name: str) -> bool:
        return False

    def has_last_good(self, name: str) -> bool:
        return False

    def needed_for(self, **_kw) -> list[str]:
        return []

    async def ensure_fresh(self, names) -> tuple[bool, dict[str, str]]:
        return True, {}

    async def tree_head(self, name: str) -> str | None:
        return None

    async def remote_head(self, name: str) -> str | None:
        return None

    async def read_skill_tree(self, name, subdir, sha) -> dict:
        return {}

    async def read_skill_file(self, name, subdir, sha, skill, rel_path):
        return None

    async def warm_all(self) -> None:
        return None

    def verify_writable(self) -> None:
        return None

    def delete_mirror(self, name: str) -> None:
        return None

    def rename_mirror(self, old: str, new: str) -> None:
        return None

    def health_map(self) -> dict:
        return {}

    def disk_stats(self) -> dict | None:
        return None
