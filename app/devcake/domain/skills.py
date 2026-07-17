"""SkillService — the skill store's domain seam (docs/16 skill store v1).

Skills are standard Claude Code skills (`<name>/SKILL.md` + optional
supporting files). Two sources, store-first:
- the operator-editable `skill-store` repo on the bundled Gitea (read via
  the InternalForgePort — F1: domain never imports adapters), and
- the bundled copies under app/devcake/skills/ (the seed content), which
  keep built-in skills working when the internal forge is disabled or down.

Skills are additive: every failure here degrades to a warning, never a
refused run. Size caps are enforced from the tree listing's blob sizes
BEFORE any download — an oversized store file must never be pulled into
the shared control-plane process (review finding, 2026-07-17).
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

log = logging.getLogger("devcake.skills")

BUILTIN_DIR = Path(__file__).parents[1] / "skills"      # app/devcake/skills/
MAX_FILE_BYTES = 200 * 1024    # oversized supporting file → skipped + warning
MAX_TOTAL_BYTES = 1024 * 1024  # per-run payload cap → later files dropped
# One poll interval: dispatches within a sweep share one store read instead
# of re-fetching per mission. Operator edits reach new runs within ~TTL.
CACHE_TTL_SECONDS = 30.0
_CACHE_BYTES_MAX = 4 * MAX_TOTAL_BYTES   # bound on cached file bytes


def parse_frontmatter(text: str) -> dict:
    """The YAML mapping between the leading `---` fences, leniently: any
    broken shape (no fences, bad YAML, non-mapping) yields {} — a malformed
    SKILL.md must never take the listing down."""
    if not text.startswith("---"):
        return {}
    try:
        end = text.index("\n---", 3)
        data = yaml.safe_load(text[3:end])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class SkillInfo(BaseModel):
    name: str
    description: str = ""
    source: Literal["store", "builtin"]
    files: int = 1


class SkillService:
    def __init__(self, internal_forge=None, builtin_dir: Path | None = None):
        self.forge = internal_forge
        self.builtin_dir = builtin_dir if builtin_dir is not None else BUILTIN_DIR
        self._tree_at = 0.0                       # monotonic stamp
        self._tree: list[dict] | None = None      # [{path, size}]
        self._file_bytes: dict[str, bytes] = {}

    # ── store reads (TTL-cached — a sweep dispatches many runs) ─────────────

    async def _store_tree(self) -> list[dict]:
        now = time.monotonic()
        if self._tree is not None and now - self._tree_at < CACHE_TTL_SECONDS:
            return self._tree
        tree = await self.forge.skill_store_tree()
        self._tree, self._tree_at = tree, now
        self._file_bytes.clear()                  # content follows the tree
        return tree

    async def _store_file(self, path: str) -> bytes:
        cached = self._file_bytes.get(path)
        if cached is not None:
            return cached
        data = await self.forge.skill_store_file(path)
        if sum(map(len, self._file_bytes.values())) + len(data) <= _CACHE_BYTES_MAX:
            self._file_bytes[path] = data
        return data

    # ── bundled copies ───────────────────────────────────────────────────────

    def builtin_seed(self) -> list[dict]:
        """[{path, content_b64}] for every bundled file — the seed input for
        InternalForgePort.ensure_skill_store. Paths are repo-relative POSIX
        (skill dir included)."""
        out = []
        if not self.builtin_dir.is_dir():
            return out
        for p in sorted(self.builtin_dir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self.builtin_dir).as_posix()
                out.append({"path": rel,
                            "content_b64": base64.b64encode(p.read_bytes()).decode()})
        return out

    def _builtin_skills(self) -> dict[str, list[Path]]:
        """{skill-name: [files]} for every bundled dir holding a SKILL.md."""
        found: dict[str, list[Path]] = {}
        if not self.builtin_dir.is_dir():
            return found
        for d in sorted(self.builtin_dir.iterdir()):
            if d.is_dir() and (d / "SKILL.md").is_file():
                found[d.name] = sorted(f for f in d.rglob("*") if f.is_file())
        return found

    # ── listing (admin surface) ──────────────────────────────────────────────

    async def list_skills(self) -> tuple[list[SkillInfo], dict]:
        """(skills, store_status). Store listing when the forge is up;
        bundled fallback otherwise — the admin Skills section renders both."""
        if self.forge is None:
            return self._builtin_listing(), {
                "enabled": False, "ok": False,
                "detail": "internal forge disabled (GITEA_ADMIN_PASSWORD unset)",
                "html_url": ""}
        try:
            paths = {t["path"] for t in await self._store_tree()}
            skills = []
            for name in sorted({p.split("/", 1)[0] for p in paths if "/" in p}):
                if f"{name}/SKILL.md" not in paths:
                    continue
                text = (await self._store_file(f"{name}/SKILL.md")
                        ).decode("utf-8", errors="replace")
                skills.append(SkillInfo(
                    name=name,
                    description=str(parse_frontmatter(text).get("description", "")),
                    source="store",
                    files=sum(1 for p in paths if p.startswith(f"{name}/"))))
            return skills, {"enabled": True, "ok": True, "detail": "",
                            "html_url": self.forge.skill_store_url()}
        except Exception as e:
            log.warning("skill store unreachable — serving bundled list: %s", e)
            return self._builtin_listing(), {
                "enabled": True, "ok": False, "detail": str(e),
                "html_url": self.forge.skill_store_url()}

    def _builtin_listing(self) -> list[SkillInfo]:
        out = []
        for name, files in self._builtin_skills().items():
            text = next(f for f in files if f.name == "SKILL.md").read_text(
                encoding="utf-8", errors="replace")
            out.append(SkillInfo(
                name=name,
                description=str(parse_frontmatter(text).get("description", "")),
                source="builtin", files=len(files)))
        return out

    # ── dispatch attach ──────────────────────────────────────────────────────

    async def payload_for(self, names: list[str]) -> tuple[list[dict], list[str]]:
        """([{name, files: [{path, content_b64}]}], warnings) for the runspec.
        Store-first per skill; bundled fallback; a skill missing from both is
        skipped with a warning — never a refused run. Paths are repo-relative
        POSIX including the skill dir (entrypoint writes ~/.claude/skills/<path>).

        Caps run against sizes from the tree/stat BEFORE any content read;
        once MAX_TOTAL_BYTES is exhausted the remaining skills are dropped
        without a single fetch."""
        payload: list[dict] = []
        warnings: list[str] = []
        store_index: dict[str, int] | None = None
        if self.forge is not None:
            try:
                store_index = {t["path"]: int(t.get("size") or 0)
                               for t in await self._store_tree()}
            except Exception as e:
                warnings.append(f"skill store unreachable ({e}) — "
                                "using bundled copies")
        builtin = self._builtin_skills()
        total = 0
        for idx, name in enumerate(names):
            in_store = bool(store_index) and f"{name}/SKILL.md" in store_index
            entry, size_used = [], 0
            if in_store:
                sized = [(p, store_index[p])
                         for p in sorted(pp for pp in store_index
                                         if pp.startswith(f"{name}/"))]
                try:
                    entry, size_used, warns = await self._collect(
                        name, sized, total, self._store_file)
                except Exception as e:
                    warnings.append(f"skill {name!r}: store read failed ({e}) "
                                    "— trying the bundled copy")
                    entry, size_used, warns = [], 0, []
                warnings.extend(warns)
            if not entry and name in builtin:
                sized = [(f.relative_to(self.builtin_dir).as_posix(),
                          f.stat().st_size) for f in builtin[name]]

                async def _read_builtin(path: str) -> bytes:
                    return (self.builtin_dir / path).read_bytes()

                entry, size_used, warns = await self._collect(
                    name, sized, total, _read_builtin)
                warnings.extend(warns)
            if not entry and not in_store and name not in builtin:
                warnings.append(f"skill {name!r} not found in the store or "
                                "bundled copies — skipped")
                continue
            total += size_used
            if entry:
                payload.append({"name": name, "files": entry})
            if total >= MAX_TOTAL_BYTES and idx + 1 < len(names):
                warnings.append(
                    f"skills payload cap ({MAX_TOTAL_BYTES} bytes) reached — "
                    f"dropped without fetching: {', '.join(names[idx + 1:])}")
                break
        return payload, warnings

    async def _collect(self, name: str, sized: list[tuple[str, int]],
                       total: int, fetch) -> tuple[list[dict], int, list[str]]:
        """One skill's files within the caps: sizes are checked BEFORE
        fetch; a store exception propagates (payload_for falls back)."""
        entry: list[dict] = []
        used = 0
        warns: list[str] = []
        for path, size in sized:
            if size > MAX_FILE_BYTES:
                warns.append(f"skill {name!r}: {path} exceeds "
                             f"{MAX_FILE_BYTES} bytes — skipped")
                continue
            if total + used + size > MAX_TOTAL_BYTES:
                warns.append(f"skills payload cap ({MAX_TOTAL_BYTES} bytes) "
                             f"reached — {path} and later files dropped")
                break
            data = await fetch(path)
            used += len(data)
            entry.append({"path": path,
                          "content_b64": base64.b64encode(data).decode()})
        return entry, used, warns
