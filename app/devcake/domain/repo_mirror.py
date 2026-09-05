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
# A remote HEAD/ref probe is one round trip: a human pressed a button (the
# connection test, Discover) or the sync is about to fetch anyway. Far
# below the fetch timeout so a black-holed host cannot hold a bulk probe.
PROBE_TIMEOUT_S = 20
# A `ls-remote` is a ref listing, not a fetch: the bulk Discover may ask
# more repositories at once than the sync fetches, so a large fleet fits
# inside the admin proxy's window instead of a deterministic tail always
# reading "timed out".
PROBE_CONCURRENCY = 12
# A blank card on an EMPTY repository has no HEAD to inherit: the mirror's
# HEAD (and so the first push, by the Dev or the claims writer) takes this
# name — the same literal the claims bootstrap uses.
BOOTSTRAP_BRANCH = "main"

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
    remote has no HEAD symref — an empty repository, a detached HEAD, or a
    server that omits the capability). Only the line whose TARGET field is
    exactly HEAD counts — a clone-shaped remote also advertises
    `refs/remotes/origin/HEAD`, whose line merely ENDS with HEAD — and only
    a `refs/heads/*` symref can seed a mirror's HEAD."""
    for line in (stdout or "").splitlines():
        ref_part, _, target = line.partition("\t")
        if target.strip() != "HEAD":
            continue
        if ref_part.startswith("ref: refs/heads/"):
            return ref_part[len("ref: refs/heads/"):].strip()
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
        # when DevCake last wrote to each mirror's repository (monotonic):
        # a sync that STARTED before that moment advertised refs which may
        # predate the write, so its completion must not stamp freshness
        self._invalidated_at: dict[str, float] = {}

    # ── identity / eligibility (pure) ────────────────────────────────────────

    def mirror_path(self, name: str) -> Path:
        return self.root / f"{name}.git"

    def _skill_source(self, name: str):
        for x in getattr(self.config, "skill_sources", None) or []:
            if x.name == name:
                return x
        return None

    def mirror_name_of(self, name: str) -> str:
        """The card whose PHYSICAL mirror serves `name` — a repo-backed
        skill source (ADR-0039) reads its backing repository card's mirror;
        every other name is itself. Callers that sync or read by card name
        resolve through this ONE place so the backed pair can never fetch
        the same bare repo under two locks."""
        src = self._skill_source(name)
        backed = (getattr(src, "backed_by", "") or "").strip() \
            if src is not None else ""
        return backed or name

    def eligible(self, name: str) -> bool:
        """A configured repo card (registered in the runtime and NOT an
        internal synthesized repo — token-scope isolation, module
        docstring) — or a dedicated skill source (2026-08-14 ruling:
        skill sources always ride the mirror; they are never cloned into
        workspaces)."""
        return ((name in self.forges.instances
                 and name not in self.forges.internal)
                or self._skill_source(name) is not None)

    @staticmethod
    def _head_branch(mirror: Path) -> str:
        """The branch a bare mirror's HEAD names ("" when detached, unborn
        file, or unreadable). Disk truth — survives process restart."""
        try:
            text = (mirror / "HEAD").read_text(errors="replace").strip()
        except OSError:
            return ""
        prefix = "ref: refs/heads/"
        return text[len(prefix):].strip() if text.startswith(prefix) else ""

    @staticmethod
    def _ref_exists(mirror: Path, branch: str) -> bool:
        """``refs/heads/<branch>`` present as a loose ref or a packed-refs
        line (after offline ``git gc``, ADR-0024, heads may live only in
        packed-refs). No subprocess: callers run on the request path."""
        if not branch:
            return False
        try:
            if (mirror / "refs" / "heads" / branch).is_file():
                return True
        except OSError:
            return False
        packed = mirror / "packed-refs"
        want = f"refs/heads/{branch}"
        try:
            if not packed.is_file():
                return False
            for line in packed.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("^"):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[1] == want:
                    return True
        except OSError:
            return False
        return False

    def has_last_good(self, name: str) -> bool:
        """Open-mode stale_cache precondition (ADR-0035 / PLAN_MEMORY §3.5).

        True only when the on-disk bare mirror's HEAD names a branch that
        is actually there — the one predicate that decides whether a
        ``file://`` clone checks anything out. A bare `git init` dir (fetch
        never succeeded), a mirror whose HEAD dangles (the card pinned a
        branch the repository lacks), or a branch pruned since the last
        good sync are all False, so callers omit rather than serving an
        empty tree as "last known good". Do NOT use ``mirror_path.is_dir``
        or the in-process ledger as a stand-in: both are true after a
        sync that left nothing servable.
        """
        mirror = self.mirror_path(self.mirror_name_of(name))
        return self._ref_exists(mirror, self._head_branch(mirror))

    @staticmethod
    def _has_any_head(mirror: Path) -> bool:
        """Any ``refs/heads/*`` at all — loose or packed."""
        heads = mirror / "refs" / "heads"
        try:
            # git leaves empty directories behind after pruning nested
            # branches — only FILES are heads; then fall through to packed
            if heads.is_dir() and any(p.is_file() for p in heads.rglob("*")):
                return True
        except OSError:
            return False
        packed = mirror / "packed-refs"
        try:
            if not packed.is_file():
                return False
            for line in packed.read_text(errors="replace").splitlines():
                parts = line.strip().split()
                if (len(parts) >= 2 and not line.startswith(("#", "^"))
                        and parts[1].startswith("refs/heads/")):
                    return True
        except OSError:
            return False
        return False

    def resolved_branch(self, name: str) -> str:
        """The branch a Dev, a PR, a protection probe or a claims push must
        use for this card: the card's pin when it has one, else the branch
        the mirror's HEAD names once a sync resolved it from the remote —
        never a bare-init HEAD whose target does not exist. The one HEAD
        without a target that counts is a bootstrapped EMPTY repository
        (a green sync over zero branches): its name is what the Dev's first
        commit creates. "" = unresolved (blank card, no successful sync
        yet); consumers that need a NAME must defer or say "unknown until
        the first sync", never render "". Sync and subprocess-free."""
        inst = self.forges.instance(name) or self._skill_source(name)
        pin = ((getattr(inst, "default_branch", "") or "").strip()
               if inst is not None else "")
        if pin:
            return pin
        physical = self.mirror_name_of(name)
        mirror = self.mirror_path(physical)
        head = self._head_branch(mirror)
        if self._ref_exists(mirror, head):
            return head
        st = self.ledger.get(physical)
        if (head and st is not None and st.ok
                and not self._has_any_head(mirror)):
            return head                      # bootstrapped empty repository
        return ""

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
        inst = self.forges.instance(name) or self._skill_source(name)
        pin = ((getattr(inst, "default_branch", "") or "").strip()
               if inst is not None else "")
        if pin and self._head_branch(self.mirror_path(name)) != pin:
            # the card's pin changed under a freshness window: the env and
            # playbook would carry the new pin while the mirror still serves
            # the old branch — resync, never serve a stale HEAD
            return False
        if max_age == 0:
            # a sync that COMPLETED after the caller asked satisfies "sync
            # before every dispatch" — this is what lets concurrent waiters
            # coalesce onto one fetch instead of queueing N identical ones
            return mono >= t_entry
        return (self._monotonic() - mono) <= max_age

    def invalidate(self, name: str) -> None:
        """DevCake itself just changed this repository (a run finished on
        it, the app merged its PR, the claims writer pushed a notebook):
        drop its freshness so the next dispatch resyncs regardless of
        `sync_max_age_seconds`. The window covers PASSIVE staleness only —
        an own write is known, so it is never served stale (the feed memo's
        rule — our own posts bump a generation — applied to mirrors). The
        ledger row (last-good, health) is untouched. A sync in flight right
        now is covered too: the moment is recorded, and `sync_one` refuses
        to stamp a fetch that started before it (the fetch may have
        advertised refs from before the write)."""
        physical = self.mirror_name_of(name)
        self._invalidated_at[physical] = self._monotonic()
        if physical in self._synced_mono:
            self._synced_mono.pop(physical, None)
            log.debug("mirror %s: freshness dropped after an own write", physical)

    async def ensure_fresh(self, names) -> tuple[bool, dict[str, str]]:
        """Sync every named mirror unless already fresh. (all_ok, {name:
        reason}) — reasons only for failures. NEVER raises; the caller's
        poll segment must survive anything this does."""
        # Backed skill sources resolve to their backing card here as well as
        # at the callers — the physical mirror must only ever sync under ITS
        # name's lock (two locks on one bare repo = racing fetches).
        names = sorted({self.mirror_name_of(n) for n in names})
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
        t_start = self._monotonic()
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
        pin = (inst.default_branch or "").strip()
        branch = pin
        probed = not branch
        if probed:
            # The card contract — repo cards and skill sources alike — is
            # "empty branch = the repository's default": resolve it from the
            # remote's HEAD symref. Never write the empty ref: git refuses
            # `refs/heads/` and the sync would fail every cycle. Probed
            # EVERY sync on purpose (founder ruling 2026-09-05): caching the
            # last answer would quietly turn "the repository's default" into
            # "the default as of some earlier sync"; the RTT rides alongside
            # the fetch's, and an operator who wants to skip it pins the
            # discovered branch on the card (Discover default branch).
            r = await self.git(["ls-remote", "--symref", expected_url, "HEAD"],
                               env=env, timeout=PROBE_TIMEOUT_S)
            if r.returncode != 0:
                # a probe ERROR keeps its own stderr — an auth failure must
                # latch the breaker, not read as "set Branch on the card"
                return await fail(f"default-branch probe: "
                                  f"{r.stderr or r.stdout}"
                                  + (" (timeout)" if r.timed_out else ""))
            branch = _symref_head_branch(r.stdout)
            if not branch:
                heads = await self._local_heads(p)
                if heads is None:
                    return await fail("could not list the mirror's branches")
                if heads:
                    return await fail(
                        "default branch: the card's branch is empty and the "
                        "remote's HEAD does not name one (detached HEAD, or "
                        "a server that omits the symref) — set Branch on "
                        "the card")
                # an EMPTY repository (no HEAD symref, no branches): there
                # is no default to inherit yet — the mirror's HEAD takes the
                # bootstrap name so the first push creates that branch
                branch = BOOTSTRAP_BRANCH
        # Verify BEFORE the HEAD move: symbolic-ref succeeds on a DANGLING
        # ref, and a green ledger over a dangling HEAD is exactly the silent
        # empty checkout this guards against. A failed verification leaves
        # the previous (valid) HEAD in place for any clone already in flight.
        # A pin on an EMPTY repository (zero heads) is legitimate — the first
        # commit is still to come — and keeps its HEAD; a pin the repository
        # does not have, or a probed default the fetch never brought, fails.
        r = await self.git(["-C", str(p), "rev-parse", "--verify",
                            "--quiet", f"refs/heads/{branch}^{{commit}}"])
        if r.returncode != 0:
            heads = await self._local_heads(p)
            if heads is None:
                return await fail("could not list the mirror's branches")
            if heads:
                shown = ", ".join(heads[:5]) + (", …" if len(heads) > 5 else "")
                if pin:
                    return await fail(
                        f"default branch: the card pins {pin!r} but the "
                        f"repository has no such branch (branches: "
                        f"{shown or 'none'}) — blank the card's Branch to "
                        f"follow the repository's default, or pin one of "
                        f"its branches")
                return await fail(
                    f"default branch: the remote's HEAD names {branch!r} "
                    f"but the fetch brought no such branch (branches: "
                    f"{shown or 'none'}; unborn HEAD, or it changed "
                    f"mid-sync) — retried next cycle; a Branch on the "
                    f"card pins it")
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
        if self._invalidated_at.get(name, float("-inf")) >= t_start:
            # an own write landed while this fetch was in flight: the refs
            # it advertised may predate the write, so this sync is NOT
            # fresh — the next gate fetches again (one extra fetch, never
            # a stale serve; `>=` errs on that side)
            self._invalidated_at.pop(name, None)
            self._synced_mono.pop(name, None)
        else:
            self._synced_mono[name] = self._monotonic()
        return st

    async def _local_heads(self, mirror: Path) -> list[str] | None:
        """Branch names the bare mirror holds; None when git could not list
        them (never silently "zero heads"). Local — no network on the
        failure path."""
        r = await self.git(["-C", str(mirror), "for-each-ref",
                            "--format=%(refname)", "refs/heads"])
        if r.returncode != 0:
            return None
        return [line.strip().removeprefix("refs/heads/")
                for line in (r.stdout or "").splitlines() if line.strip()]

    def _remote_probe_target(self, name: str):
        """(inst, credentialed url, source pin) for a remote probe of `name`
        — the backing card supplies remote, clone user and token for a
        repo-backed skill source (ADR-0039); None when unconfigured."""
        pin = ""
        backing = self.mirror_name_of(name)
        if backing != name:
            src = self._skill_source(name)
            pin = (getattr(src, "default_branch", "") or "").strip()
            name = backing
        inst = self.forges.instance(name)
        forge = self.forges.get(name)
        if inst is None or forge is None:
            inst, forge = self._skill_source(name), None
        if inst is None or not (inst.url or "").strip():
            return None
        clone_user = (getattr(forge.descriptor, "clone_user", "") or ""
                      if forge is not None else self.clone_user_of(inst.forge))
        return inst, self._origin_url(inst.url.strip(), clone_user), pin

    async def remote_default_branch(self, name: str) -> str:
        """The branch the repository's HEAD names on the forge, straight
        from the remote (`ls-remote --symref … HEAD`, the card's read token
        via askpass) — what a blank card resolves to at sync, and what the
        Discover default branch actions fill in. "" when the remote has no
        HEAD symref (empty repository / detached HEAD). Raises RuntimeError
        with the redacted git error when the remote cannot be reached."""
        target = self._remote_probe_target(name)
        if target is None:
            raise RuntimeError("repository URL is empty")
        inst, url, _pin = target
        r = await self.git(["ls-remote", "--symref", url, "HEAD"],
                           env=self._git_env(inst), timeout=PROBE_TIMEOUT_S)
        if r.returncode != 0:
            detail = redact(" ".join((r.stderr or r.stdout or "").split()))
            raise RuntimeError((detail[-300:] or "ls-remote failed")
                               + (" (timeout)" if r.timed_out else ""))
        return _symref_head_branch(r.stdout)

    async def remote_head(self, name: str) -> str | None:
        """Best-effort branch head straight from the forge — provenance for
        mirror-INELIGIBLE cards (bundled Gitea), whose `tree_head` has no
        mirror to read (PLAN_MEMORY §3.6). One `ls-remote` with the card's
        read token via askpass; None on any failure."""
        # a repo-backed skill source's OWN pin (when set) still names the
        # ref, so a bad pin fails THIS probe instead of riding the backing
        # card's default to a false green (_remote_probe_target returns it)
        target = self._remote_probe_target(name)
        if target is None:
            return None
        inst, url, pin = target
        branch = pin or (inst.default_branch or "").strip()
        ref = f"refs/heads/{branch}" if branch else "HEAD"
        r = await self.git(["ls-remote", url, ref], env=self._git_env(inst),
                           timeout=PROBE_TIMEOUT_S)
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
        # the card's OWN branch pin applies even when the physical mirror is
        # a backing repo card's (a backed source may track e.g. `stable`
        # while work happens on the backing card's default branch)
        physical = self.mirror_name_of(name)
        p = self.mirror_path(physical)
        branch = ((inst.default_branch or "").strip()
                  if inst is not None else "")
        if physical != name and branch:
            # a backed source's explicit pin fails LOUD when the branch is
            # missing — the shared mirror's HEAD is the BACKING card's
            # branch, so falling back would silently serve the wrong one
            # (an own-remote mirror's HEAD is its own branch: harmless)
            refs = [f"refs/heads/{branch}"]
        else:
            refs = ([f"refs/heads/{branch}"] if branch else []) + ["HEAD"]
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
        p = self.mirror_path(self.mirror_name_of(name))
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
        p = self.mirror_path(self.mirror_name_of(name))
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
                    # backed sources have no mirror of their own — their
                    # physical mirror warms with the backing repo card
                    if x.configured and self.mirror_name_of(x.name) == x.name])
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
        first so an in-flight Dev clone keeps its inode; then remove.
        Bookkeeping pops run even with NO on-disk dir: a card whose first
        sync failed before init holds a ledger row too, and a removed card
        must never keep a ghost /health entry until restart."""
        self.ledger.pop(name, None)
        self._synced_mono.pop(name, None)
        self._invalidated_at.pop(name, None)
        self._locks.pop(name, None)
        p = self.mirror_path(name)
        if not p.exists():
            return
        stale = p.with_name(f"{p.name}.stale-{int(time.time())}")
        try:
            p.rename(stale)
            shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            log.exception("could not remove mirror %s", name)

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
        if old in self._invalidated_at:
            self._invalidated_at[new] = self._invalidated_at.pop(old)
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

    def mirror_name_of(self, name: str) -> str:
        return name

    def eligible(self, name: str) -> bool:
        return False

    def has_last_good(self, name: str) -> bool:
        return False

    def invalidate(self, name: str) -> None:
        return None

    def resolved_branch(self, name: str) -> str:
        return ""

    async def remote_default_branch(self, name: str) -> str:
        return ""

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
