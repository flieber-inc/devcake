"""ClaimsNotebooks adapter (PLAN_MEMORY §5.3).

Forge-neutral: clone + commit + push through `adapters.git` (the app's
only subprocess site) using the card's **write** token. Never a mission
Dev token, never GITEA_ADMIN_*. `reference_only` cards are skipped.

Bundled Gitea operator notebooks use the same path — they already have a
minted write token. One algorithm for every registered forge.
"""

from __future__ import annotations

import json
import logging
import shutil
import stat
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..adapters.git import run_git
from ..ports.claims import ClaimsNotebooks

log = logging.getLogger("devcake.claims")

_ASKPASS = Path("/tmp/devcake-claims-askpass.sh")
_ASKPASS_BODY = "#!/bin/sh\necho \"$DEVCAKE_CLAIMS_TOKEN\"\n"

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "DevCake",
    "GIT_AUTHOR_EMAIL": "claims@devcake.local",
    "GIT_COMMITTER_NAME": "DevCake",
    "GIT_COMMITTER_EMAIL": "claims@devcake.local",
}


class ClaimsWriter:
    """One writer for every configured card that stores a write token."""

    def __init__(self, config, git=run_git):
        self.config = config
        self.git = git

    def _inst(self, card):
        return next((r for r in self.config.repos if r.name == card), None)

    def can_write(self, card: str) -> bool:
        inst = self._inst(card)
        if inst is None or not (inst.url or "").strip():
            return False
        if inst.reference_only or not inst.token:
            return False
        return True

    def _clone_user(self, inst) -> str:
        from .registry import forges
        desc = forges().get(inst.forge)
        return (getattr(desc, "clone_user", None) or "") if desc else ""

    def _remote_url(self, inst) -> str:
        url = inst.url.strip()
        if url.startswith("file:") or "://" not in url:
            return url
        user = self._clone_user(inst)
        if not user:
            return url
        parts = urlsplit(url)
        if parts.username:
            return url
        netloc = f"{user}@{parts.netloc}"
        return urlunsplit((parts.scheme, netloc, parts.path,
                           parts.query, parts.fragment))

    def _git_env(self, inst, *, write: bool) -> dict[str, str]:
        token = inst.token if write else (inst.token_ro or inst.token)
        env = dict(_GIT_IDENTITY)
        if not token:
            return env
        url = inst.url.strip()
        if url.startswith("file:") or "://" not in url:
            return env
        if not _ASKPASS.exists():
            _ASKPASS.write_text(_ASKPASS_BODY)
            _ASKPASS.chmod(stat.S_IRWXU)
        env["GIT_ASKPASS"] = str(_ASKPASS)
        env["DEVCAKE_CLAIMS_TOKEN"] = token
        return env

    def _refuse_paths(self, paths) -> None:
        for path in paths:
            if (not path.startswith(".claims/") or ".." in path.split("/")
                    or path.endswith("/")):
                raise ValueError(
                    f"claims writer refuses path {path!r} — only .claims/")

    async def _run(self, args, *, cwd=None, env=None, ok=True):
        r = await self.git(args, cwd=cwd, env=env)
        if ok and r.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args[:3])} failed: "
                f"{(r.stderr or r.stdout or '')[-400:]}")
        return r

    async def _checkout(self, inst, *, write: bool) -> Path:
        dest = Path(tempfile.mkdtemp(prefix="devcake-claims-"))
        env = self._git_env(inst, write=write)
        url = self._remote_url(inst)
        # blank card = the repository's default: a plain clone checks out
        # the remote's HEAD, no name needed here (docs/02, RepoInstance)
        branch = (inst.default_branch or "").strip()
        argv = ["clone", "--depth", "1"]
        if branch:
            argv += ["--branch", branch]
        r = await self.git(argv + [url, str(dest)], env=env)
        if r.returncode != 0:
            await self._empty_or_unlistable(dest, url=url, branch=branch,
                                            env=env, clone=r)
        return dest

    async def _current_branch(self, dest: Path, env: dict) -> str:
        r = await self.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest,
                           env=env)
        out = (r.stdout or "").strip()
        return "" if r.returncode != 0 or out == "HEAD" else out

    async def _empty_or_unlistable(self, dest: Path, *, url: str,
                                   branch: str, env: dict,
                                   clone) -> None:
        """After a failed `clone --branch`, distinguish genuine empty
        remotes (empty-init) from outages (raise → unlistable)."""
        probe = await self.git(["ls-remote", "--heads", url], env=env)
        shutil.rmtree(dest, ignore_errors=True)
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout or clone.stderr
                      or clone.stdout or "")[-400:]
            raise RuntimeError(
                f"claims notebook unlistable (clone+ls-remote failed): "
                f"{detail}")
        heads = {
            line.split("\t", 1)[-1].strip()
            for line in (probe.stdout or "").splitlines()
            if line.strip()
        }
        want = f"refs/heads/{branch}" if branch else "HEAD"
        if branch and want in heads:
            detail = (clone.stderr or clone.stdout or "")[-400:]
            raise RuntimeError(
                f"claims notebook clone failed though {want} exists: "
                f"{detail}")
        if heads and not branch:
            # a populated remote whose default checkout failed is an
            # outage, never a confidently empty notebook
            detail = (clone.stderr or clone.stdout or "")[-400:]
            raise RuntimeError(
                f"claims notebook unlistable (clone of the repository's "
                f"default failed on a populated remote): {detail}")
        if heads:
            # Populated remote without the configured branch: almost
            # certainly a misconfigured default_branch — reading it as a
            # confidently empty notebook is the exact failure this helper
            # exists to prevent. Bootstrap is the no-heads case below.
            have = ", ".join(sorted(h.removeprefix("refs/heads/")
                                    for h in heads)[:5])
            raise RuntimeError(
                f"claims notebook unlistable: remote has no {want} "
                f"(branches: {have}) — check the card's default_branch")
        # Reachable remote with no heads at all (fresh bare repo) —
        # empty-init so the first commit can push the branch.
        dest.mkdir(parents=True)
        # a blank card on an EMPTY remote has no default to inherit: the
        # bootstrap branch is the literal `main` the first push creates
        for args in (["init", "-b", branch or "main"],
                     ["remote", "add", "origin", url]):
            await self._run(args, cwd=dest, env=env)

    async def _with_tree(self, card, *, write: bool):
        inst = self._inst(card)
        if inst is None:
            return None, None
        try:
            dest = await self._checkout(inst, write=write)
        except Exception:  # noqa: BLE001 — unlistable
            log.exception("claims: checkout failed for %s", card)
            return inst, None
        return inst, dest

    def _cleanup(self, dest: Path | None) -> None:
        if dest is not None:
            shutil.rmtree(dest, ignore_errors=True)

    async def list_json_names(self, card: str) -> list[str] | None:
        inst, dest = await self._with_tree(card, write=False)
        if dest is None:
            return None
        try:
            claims = dest / ".claims"
            if not claims.is_dir():
                return []
            return sorted(p.name for p in claims.glob("*.json") if p.is_file())
        finally:
            self._cleanup(dest)

    async def has_readme(self, card: str) -> bool | None:
        inst, dest = await self._with_tree(card, write=False)
        if dest is None:
            return None
        try:
            return (dest / ".claims" / "README.md").is_file()
        finally:
            self._cleanup(dest)

    async def snapshot(self, card: str) -> dict | None:
        """Listing + README presence in ONE checkout (the append path used
        to burn two)."""
        inst, dest = await self._with_tree(card, write=False)
        if dest is None:
            return None
        try:
            claims = dest / ".claims"
            names = (sorted(p.name for p in claims.glob("*.json")
                            if p.is_file()) if claims.is_dir() else [])
            return {"json_names": names,
                    "has_readme": (claims / "README.md").is_file()}
        finally:
            self._cleanup(dest)

    async def list_claim_meta(self, card: str) -> list[dict] | None:
        inst, dest = await self._with_tree(card, write=False)
        if dest is None:
            return None
        try:
            claims = dest / ".claims"
            if not claims.is_dir():
                return []
            out = []
            for p in sorted(claims.glob("*.json")):
                try:
                    rec = json.loads(p.read_text())
                except Exception:  # noqa: BLE001 — corrupt claim: id from name
                    rec = {}
                out.append({
                    "id": rec.get("id") or p.stem,
                    "source_instance": rec.get("source_instance", ""),
                    "source_pmo_id": rec.get("source_pmo_id", ""),
                })
            return out
        finally:
            self._cleanup(dest)

    async def commit(self, card: str, *, creates: dict[str, str],
                     deletes: list[str], message: str) -> None:
        if not self.can_write(card):
            raise ValueError(f"cannot write claims on {card!r}")
        self._refuse_paths(list(creates) + list(deletes))
        try:
            await self._commit_once(card, creates=creates, deletes=deletes,
                                    message=message)
        except Exception:  # noqa: BLE001 — push race: replay once onto the fresh head
            # creates and deletes are idempotent (same bytes / same paths),
            # so a second checkout+apply+push is safe; a second failure
            # propagates to the caller's audit.
            log.warning("claims commit on %s failed — retrying once", card,
                        exc_info=True)
            await self._commit_once(card, creates=creates, deletes=deletes,
                                    message=message)

    async def _commit_once(self, card: str, *, creates: dict[str, str],
                           deletes: list[str], message: str) -> None:
        inst, dest = await self._with_tree(card, write=True)
        if dest is None:
            raise RuntimeError(f"cannot checkout {card!r} to write claims")
        env = self._git_env(inst, write=True)
        try:
            for path, text in creates.items():
                p = dest / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
            for path in deletes:
                p = dest / path
                if p.is_file():
                    p.unlink()
            await self._run(["add", "-A", "--", ".claims"], cwd=dest, env=env)
            staged = await self.git(["diff", "--cached", "--quiet"],
                                    cwd=dest, env=env)
            if staged.returncode == 0:
                return
            await self._run(["commit", "-m", message], cwd=dest, env=env)
            branch = (inst.default_branch or "").strip()
            # pinned: push to the pin; blank: HEAD is whatever the clone (or
            # the bootstrap init) checked out — push it by name so an
            # empty-remote bootstrap creates the branch the init named
            target = branch or (await self._current_branch(dest, env)) or "main"
            await self._run(
                ["push", "origin", f"HEAD:refs/heads/{target}"],
                cwd=dest, env=env)
        finally:
            self._cleanup(dest)


def make_claims_writer(config) -> ClaimsNotebooks:
    """Always a real writer. Operator notebooks already have a write
    token, same as external cards."""
    return ClaimsWriter(config)
