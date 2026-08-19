"""SkillService — the skill store's domain seam (docs/16 skill store v1).

Skills are standard Claude Code skills (`<name>/SKILL.md` + optional
supporting files). Three sources, store-first for flat names:
- the operator-editable `skill-store` repo on the bundled Gitea (read via
  the InternalForgePort — F1: domain never imports adapters),
- the bundled copies under app/devcake/skills/ (the seed content), which
  keep built-in skills working when the internal forge is disabled or down,
- and external `<source>/<skill>` names from dedicated `skill_sources`
  connections (ADR-0016 addendum 2; read via RepoCache, never repo cards).

Skills are additive: every failure here degrades to a warning, never a
refused run. Size caps are enforced from the tree listing's blob sizes
BEFORE any download — an oversized store file must never be pulled into
the shared control-plane process (review finding, 2026-07-17). A skill
whose SKILL.md is file-cap dropped is skipped entirely (no helpers-only
payload — ADR-0016 Decision 4 / Required soft-force honesty).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel

log = logging.getLogger("devcake.skills")

BUILTIN_DIR = Path(__file__).parents[1] / "skills"      # app/devcake/skills/
MAX_FILE_BYTES = 200 * 1024    # oversized supporting file → skipped + warning
MAX_TOTAL_BYTES = 1024 * 1024  # per-run payload cap → later files dropped


def _over_file_cap(size) -> bool:
    """THE per-file cap comparison — the payload pipe (`_collect`) and the
    catalog's SKILL.md reads share it, so no second cap arithmetic exists."""
    return int(size) > MAX_FILE_BYTES
# One poll interval: dispatches within a sweep share one store read instead
# of re-fetching per mission. Operator edits reach new runs within ~TTL.
CACHE_TTL_SECONDS = 30.0
_CACHE_BYTES_MAX = 4 * MAX_TOTAL_BYTES   # bound on cached file bytes
# same shape as the DevType.skills validator in config.py (kept in sync by
# test_skills — a name that saves here must be selectable there)
SKILL_NAME_RE = r"[a-z0-9][a-z0-9_-]{0,63}"


class SkillStoreError(Exception):
    """Authoring-surface refusal, carrying the HTTP status the API maps it
    to: 404 unknown, 409 exists, 422 invalid, 503 store disabled."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


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
    except Exception:  # noqa: BLE001 — lenient by contract (docstring): any broken shape yields {}; a malformed SKILL.md must never take the listing down
        return {}


class SkillInfo(BaseModel):
    name: str
    description: str = ""
    source: Literal["store", "builtin", "external"]
    files: int = 1
    # shipped in the app image: re-seeds at boot, so it can be edited but
    # never deleted — the UI offers delete only when this is False
    builtin: bool = False
    # external skills only (ADR-0016 addendum 2): the skill_sources
    # connection name the skill is served from — read-only by construction
    # (save/delete refuse `/`)
    origin: str = ""


class SkillService:
    def __init__(self, internal_forge=None, builtin_dir: Path | None = None,
                 repo_cache=None, config=None):
        self.forge = internal_forge
        self.builtin_dir = builtin_dir if builtin_dir is not None else BUILTIN_DIR
        # `<source>/<skill>` names read the ADR-0024 mirror (RepoCache) —
        # external skills, read-only, no second cache. config supplies the
        # dedicated skill_sources connections (2026-08-14 ruling — never
        # repo cards); both None in tests that never touch external names.
        self.repo_cache = repo_cache
        self.config = config
        self._tree_at = 0.0                       # monotonic stamp
        self._tree: list[dict] | None = None      # [{path, size}]
        self._file_bytes: dict[str, bytes] = {}

    def _skill_source(self, name: str):
        """The named skill SOURCE (dedicated `skill_sources` connection —
        2026-08-14 ruling; repo cards are never consulted for skills)."""
        for r in (self.config.skill_sources if self.config is not None else []):
            if r.name == name:
                return r
        return None

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
            sizes = {t["path"]: int(t.get("size") or 0)
                     for t in await self._store_tree()}
            paths = set(sizes)
            builtin_names = set(self._builtin_skills())
            names = sorted(
                n for n in {p.split("/", 1)[0] for p in paths if "/" in p}
                if f"{n}/SKILL.md" in paths)

            async def _one(name: str) -> SkillInfo:
                # parallel per skill: sequential Gitea file GETs were the
                # catalog's load-time cost when many skills live in the store
                n_files = sum(1 for p in paths if p.startswith(f"{name}/"))
                md_path = f"{name}/SKILL.md"
                # size gate BEFORE the read (same class as external_infos):
                # an oversized store SKILL.md must not be pulled into memory
                if _over_file_cap(sizes.get(md_path, 0)):
                    return SkillInfo(
                        name=name,
                        description=f"(SKILL.md exceeds the "
                                    f"{MAX_FILE_BYTES}-byte cap — "
                                    f"not read)",
                        source="store", files=n_files,
                        builtin=name in builtin_names)
                text = (await self._store_file(md_path)
                        ).decode("utf-8", errors="replace")
                return SkillInfo(
                    name=name,
                    description=str(parse_frontmatter(text).get("description", "")),
                    source="store",
                    files=n_files,
                    builtin=name in builtin_names)

            skills = list(await asyncio.gather(*(_one(n) for n in names)))
            return skills, {"enabled": True, "ok": True, "detail": "",
                            "html_url": self.forge.skill_store_url()}
        except Exception as e:  # noqa: BLE001 — skill store degrades to the bundled listing; failure logged + surfaced in store_status detail
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
                source="builtin", files=len(files), builtin=True))
        return out

    async def get_skill(self, name: str) -> dict:
        """Admin View: full skill tree as UTF-8 text files (store-first when
        the forge is up, bundled fallback; `<source>/<skill>` reads the
        skill-source mirror). Raises SkillStoreError 404/422. Paths are
        relative to the skill dir (SKILL.md, not name/SKILL.md)."""
        if "/" in (name or ""):
            return await self._get_external(name)
        if not re.fullmatch(SKILL_NAME_RE, name or ""):
            raise SkillStoreError(
                422, f"invalid skill name {name!r}")
        builtin = self._builtin_skills()
        if self.forge is not None:
            try:
                tree = await self._store_tree()
                sized = sorted(
                    (t["path"], int(t.get("size") or 0)) for t in tree
                    if t["path"].startswith(f"{name}/"))
                if f"{name}/SKILL.md" in {p for p, _ in sized}:
                    # ONE capped read path (same class as external View):
                    # sizes-before-fetch + MAX_FILE_BYTES/MAX_TOTAL_BYTES;
                    # skipped files surface as warnings.
                    entry, _used, warns = await self._collect(
                        name, sized, 0, self._store_file)
                    if not entry:
                        raise SkillStoreError(
                            404, warns[0] if warns
                            else f"skill {name!r} not found")
                    files = [{
                        "path": e["path"][len(name) + 1:],
                        "content": base64.b64decode(e["content_b64"]).decode(
                            "utf-8", errors="replace"),
                    } for e in entry]
                    skill_md = next(
                        (f["content"] for f in files
                         if f["path"] == "SKILL.md"), "")
                    return {
                        "name": name,
                        "description": str(
                            parse_frontmatter(skill_md).get("description", "")),
                        "source": "store",
                        "builtin": name in builtin,
                        "warnings": warns,
                        "files": files,
                    }
            except SkillStoreError:
                raise
            except Exception as e:  # noqa: BLE001 — store failure degrades to bundled copy for View, same as list_skills
                log.warning("skill store unreachable for get_skill %s — "
                            "trying bundled: %s", name, e)
        if name in builtin:
            files = []
            root = self.builtin_dir / name
            for p in builtin[name]:
                rel = p.relative_to(root).as_posix()
                files.append({
                    "path": rel,
                    "content": p.read_text(encoding="utf-8", errors="replace"),
                })
            skill_md = next(
                f["content"] for f in files if f["path"] == "SKILL.md")
            return {
                "name": name,
                "description": str(
                    parse_frontmatter(skill_md).get("description", "")),
                "source": "builtin",
                "builtin": True,
                "files": files,
            }
        raise SkillStoreError(404, f"skill {name!r} not found")

    # ── external reads (ADR-0016 addendum 2: skill-source mirror, read-only) ─

    async def _get_external(self, name: str) -> dict:
        source, _, skill = name.partition("/")
        if not (re.fullmatch(r"[a-z][a-z0-9]{0,11}", source)
                and re.fullmatch(SKILL_NAME_RE, skill)):
            raise SkillStoreError(422, f"invalid skill name {name!r}")
        if self._skill_source(source) is None or self.repo_cache is None:
            raise SkillStoreError(
                404, f"skill source {source!r} not configured")
        # ONE capped read path (audit 2026-08-13: the first cut re-read every
        # blob here UNCAPPED — a large third-party file on default_branch
        # could OOM the shared app, the exact class the store forbids). The
        # dispatch pipe's `_collect_external` does sha-pin + sizes-before-
        # fetch + MAX_FILE_BYTES/MAX_TOTAL_BYTES; the View only decodes and
        # strips the basename-dir prefix. Skipped files surface as warnings.
        entry, _used, warns = await self._collect_external(name, 0)
        if not entry:
            raise SkillStoreError(
                404, warns[0] if warns else f"skill {name!r} not found")
        files = [{"path": e["path"].partition("/")[2],
                  "content": base64.b64decode(e["content_b64"]).decode(
                      "utf-8", errors="replace")} for e in entry]
        skill_md = next((f["content"] for f in files
                         if f["path"] == "SKILL.md"), "")
        return {"name": name,
                "description": str(
                    parse_frontmatter(skill_md).get("description", "")),
                "source": "external", "builtin": False, "origin": source,
                "warnings": warns, "files": files}

    async def external_infos(self, referenced: list[str]) -> list[SkillInfo]:
        """Catalog rows for the skill sources that Dev Types reference via
        `<source>/<skill>` names — EVERY skill in those sources' trees (so
        the operator can discover what else a source offers), best-effort:
        an unsynced or unreadable mirror contributes nothing (the Dev-Type
        chips still render unknown names with the stale chip)."""
        out: list[SkillInfo] = []
        sources = sorted({n.split("/", 1)[0] for n in referenced if "/" in n})
        for source in sources:
            inst = self._skill_source(source)
            if inst is None or self.repo_cache is None:
                continue
            try:
                sha = await self.repo_cache.tree_head(source)
                if sha is None:
                    continue
                tree = await self.repo_cache.read_skill_tree(
                    source, inst.subdir, sha)
                for skill, rels in sorted(tree.items()):
                    # size gate BEFORE the read (audit 2026-08-13): the
                    # catalog renders every referenced source — an oversized
                    # third-party SKILL.md must not be pulled into memory
                    if _over_file_cap(rels.get("SKILL.md", 0)):
                        out.append(SkillInfo(
                            name=f"{source}/{skill}",
                            description=f"(SKILL.md exceeds the "
                                        f"{MAX_FILE_BYTES}-byte cap — "
                                        f"not read)",
                            source="external", files=len(rels),
                            builtin=False, origin=source))
                        continue
                    md = await self.repo_cache.read_skill_file(
                        source, inst.subdir, sha, skill, "SKILL.md")
                    desc = str(parse_frontmatter(
                        (md or b"").decode("utf-8", errors="replace")
                    ).get("description", ""))
                    out.append(SkillInfo(
                        name=f"{source}/{skill}", description=desc,
                        source="external", files=len(rels), builtin=False,
                        origin=source))
            except Exception as e:  # noqa: BLE001 — the catalog is advisory; a broken mirror never takes the Skills page down
                log.warning("external skill listing for source %r failed: %s",
                            source, e)
        return out

    # ── authoring (admin UI: create / import / delete — docs/11) ────────────

    def compose_skill(self, name: str, description: str, body: str) -> list[dict]:
        """The single-file skill the 'Add skill' form produces: frontmatter
        is GENERATED (yaml.safe_dump — a colon in the description must not
        break parsing), so the operator never touches YAML."""
        fm = yaml.safe_dump(
            {"name": name, "description": description,
             "metadata": {"source": "operator (admin panel)"}},
            sort_keys=False, allow_unicode=True).strip()
        text = f"---\n{fm}\n---\n\n{body.strip()}\n"
        return [{"path": "SKILL.md",
                 "content_b64": base64.b64encode(text.encode()).decode()}]

    def validate_import(self, files: list[dict]) -> str:
        """Import pre-flight: the uploaded files must contain a root
        SKILL.md whose frontmatter carries a valid name + description.
        Returns the skill name; raises SkillStoreError(422) otherwise."""
        skill_md = next((f for f in files or []
                         if f.get("path") == "SKILL.md"), None)
        if skill_md is None:
            raise SkillStoreError(422, "the upload needs a SKILL.md at the "
                                       "top level of the skill")
        try:
            text = base64.b64decode(skill_md.get("content_b64") or "",
                                    validate=True).decode("utf-8")
        except Exception:  # noqa: BLE001 — any decode failure maps to a typed SkillStoreError(422) refusal the API surfaces
            raise SkillStoreError(422, "SKILL.md is not valid base64/UTF-8")
        fm = parse_frontmatter(text)
        name = str(fm.get("name") or "")
        if not re.fullmatch(SKILL_NAME_RE, name):
            raise SkillStoreError(
                422, f"SKILL.md frontmatter name {name!r} is missing or "
                     "invalid — lowercase alnum with - or _, ≤64 chars")
        if not str(fm.get("description") or "").strip():
            raise SkillStoreError(
                422, "SKILL.md frontmatter needs a description — it is what "
                     "tells the agent when to use the skill")
        return name

    async def save_skill(self, name: str, files: list[dict],
                         overwrite: bool = False) -> None:
        """Write one skill into the store (paths arrive relative to the
        skill dir, are re-validated, and land under {name}/). Collisions —
        with a store skill OR a built-in name — need explicit overwrite. An
        overwrite is a full REPLACE: files the new version dropped are
        deleted, never left as orphans that keep shipping to every run."""
        if self.forge is None:
            raise SkillStoreError(503, "the skill store needs the bundled "
                                       "Gitea (GITEA_ADMIN_PASSWORD unset)")
        if not re.fullmatch(SKILL_NAME_RE, name):
            raise SkillStoreError(422, f"invalid skill name {name!r}")
        # dedupe by path, last wins — a crafted duplicate would batch two
        # create ops for one path and Gitea 422s
        deduped = {}
        for f in files or []:
            deduped[f.get("path")] = f
        files = list(deduped.values())
        if "SKILL.md" not in deduped:
            raise SkillStoreError(422, "a skill needs a SKILL.md")
        total = 0
        prefixed = []
        for f in files:
            rel = PurePosixPath(f.get("path") or "")
            if not rel.parts or rel.is_absolute() or ".." in rel.parts:
                raise SkillStoreError(422, f"unsafe path {f.get('path')!r}")
            try:
                data = base64.b64decode(f.get("content_b64") or "",
                                        validate=True)
            except Exception:  # noqa: BLE001 — any decode failure maps to a typed SkillStoreError(422) refusal the API surfaces
                raise SkillStoreError(422, f"{rel}: not valid base64")
            if len(data) > MAX_FILE_BYTES:
                raise SkillStoreError(422, f"{rel} exceeds the "
                                           f"{MAX_FILE_BYTES}-byte file cap")
            total += len(data)
            prefixed.append({"path": f"{name}/{rel.as_posix()}",
                             "content_b64": f["content_b64"]})
        if total > MAX_TOTAL_BYTES:
            raise SkillStoreError(422, f"skill exceeds the "
                                       f"{MAX_TOTAL_BYTES}-byte total cap")
        try:
            existing = {t["path"] for t in await self._store_tree()}
        except Exception as e:  # noqa: BLE001 — store I/O failure maps to a typed SkillStoreError(503) the API surfaces
            raise SkillStoreError(503, f"skill store unreachable: {e}")
        if not overwrite and (f"{name}/SKILL.md" in existing
                              or name in self._builtin_skills()):
            raise SkillStoreError(
                409, f"skill {name!r} already exists — confirm overwrite "
                     "to replace it")
        # full-replace: drop files the prior version had but this one doesn't
        orphans = ({p for p in existing if p.startswith(f"{name}/")}
                   - {f["path"] for f in prefixed})
        try:
            await self.forge.write_skill_files(
                prefixed, f"devcake admin: save skill {name}")
            if orphans:
                await self.forge.delete_skill_paths(
                    sorted(orphans), f"devcake admin: prune skill {name}")
        except Exception as e:  # noqa: BLE001 — store I/O failure maps to a typed SkillStoreError(502) the API surfaces
            raise SkillStoreError(502, f"skill store write failed: {e}")
        self._invalidate()

    async def delete_skill(self, name: str) -> None:
        """Remove an operator skill from the store. Built-ins are refused —
        they re-seed at boot; deselecting them is the retirement path."""
        if self.forge is None:
            raise SkillStoreError(503, "the skill store needs the bundled "
                                       "Gitea (GITEA_ADMIN_PASSWORD unset)")
        if name in self._builtin_skills():
            raise SkillStoreError(
                422, f"{name!r} is a built-in skill — it re-seeds at boot; "
                     "deselect it on Dev Types instead")
        try:
            paths = [t["path"] for t in await self._store_tree()
                     if t["path"].startswith(f"{name}/")]
        except Exception as e:  # noqa: BLE001 — store I/O failure maps to a typed SkillStoreError(503) the API surfaces
            raise SkillStoreError(503, f"skill store unreachable: {e}")
        if not paths:
            raise SkillStoreError(404, f"skill {name!r} is not in the store")
        try:
            await self.forge.delete_skill_paths(
                paths, f"devcake admin: delete skill {name}")
        except Exception as e:  # noqa: BLE001 — store I/O failure maps to a typed SkillStoreError(502) the API surfaces
            raise SkillStoreError(502, f"skill store delete failed: {e}")
        self._invalidate()

    def _invalidate(self) -> None:
        self._tree = None
        self._file_bytes.clear()

    # ── dispatch attach ──────────────────────────────────────────────────────

    async def payload_for(self, names: list[str]) -> tuple[list[dict], list[str]]:
        """([{name, files: [{path, content_b64}]}], warnings) for the runspec.
        Store-first per skill; bundled fallback only when the skill is absent
        from the store tree or a store *read* fails — never when the store
        listed the skill but every file was dropped by a size cap (that would
        silently re-ship an older bundled override). A skill missing from
        both is skipped with a warning — never a refused run. Paths are
        repo-relative POSIX including the skill dir (entrypoint writes them
        under $HOME/<skills_dir>/<path>, per the harness registry).

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
            except Exception as e:  # noqa: BLE001 — skills are additive: store failure degrades to the bundled copies, recorded in warnings
                warnings.append(f"skill store unreachable ({e}) — "
                                "using bundled copies")
        builtin = self._builtin_skills()
        total = 0
        for idx, name in enumerate(names):
            external = "/" in name        # <source>/<skill> — structurally
            #                               disjoint from store/builtin names
            in_store = (not external and bool(store_index)
                        and f"{name}/SKILL.md" in store_index)
            entry, size_used = [], 0
            store_read_failed = False
            if external:
                try:
                    entry, size_used, warns = await self._collect_external(
                        name, total)
                except Exception as e:  # noqa: BLE001 — skills are additive: a mirror read failure skips the skill, recorded in warnings
                    warnings.append(f"skill {name!r}: mirror read failed "
                                    f"({e}) — skipped")
                    entry, size_used, warns = [], 0, []
                warnings.extend(warns)
            elif in_store:
                sized = [(p, store_index[p])
                         for p in sorted(pp for pp in store_index
                                         if pp.startswith(f"{name}/"))]
                try:
                    entry, size_used, warns = await self._collect(
                        name, sized, total, self._store_file)
                except Exception as e:  # noqa: BLE001 — skills are additive: store read failure falls back to the bundled copy, recorded in warnings
                    warnings.append(f"skill {name!r}: store read failed ({e}) "
                                    "— trying the bundled copy")
                    entry, size_used, warns = [], 0, []
                    store_read_failed = True
                warnings.extend(warns)
            # Builtin only when the skill is not listed in the store (or the
            # store is down so in_store is False), or a store read threw.
            # Cap drops on a listed store skill stay empty — no silent
            # re-ship of the older bundled override.
            if (not entry and not external and name in builtin
                    and (not in_store or store_read_failed)):
                sized = [(f.relative_to(self.builtin_dir).as_posix(),
                          f.stat().st_size) for f in builtin[name]]

                async def _read_builtin(path: str) -> bytes:
                    return (self.builtin_dir / path).read_bytes()

                entry, size_used, warns = await self._collect(
                    name, sized, total, _read_builtin)
                warnings.extend(warns)
            if not entry and not external and not in_store \
                    and name not in builtin:
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

    async def _collect_external(self, name: str, total: int
                                ) -> tuple[list[dict], int, list[str]]:
        """One `<source>/<skill>` skill from a skill-source mirror (ADR-0016
        addendum 2): pin the sha, list sized blobs (mirror-side filters
        already dropped symlink modes and hostile dir names), rebase payload
        paths to the BASENAME dir — so `install_skills` and harness discovery
        stay untouched — and run the same `_collect` caps. Deliberately
        uncached: the dispatch gate just proved the mirror fresh, and this
        read pins one sha so a concurrent fetch cannot tear it."""
        source, _, skill = name.partition("/")
        inst = self._skill_source(source)
        if self.repo_cache is None or inst is None:
            return [], 0, [f"skill {name!r}: skill source {source!r} is not "
                           "configured — skipped"]
        sha = await self.repo_cache.tree_head(source)
        if sha is None:
            return [], 0, [f"skill {name!r}: mirror for {source!r} has no "
                           "readable head — skipped"]
        tree = await self.repo_cache.read_skill_tree(
            source, inst.subdir, sha)
        files = tree.get(skill)
        if not files:
            where = f"{source}:{inst.subdir}" if inst.subdir \
                else source
            return [], 0, [f"skill {name!r}: no {skill}/SKILL.md under "
                           f"{where} — skipped"]
        sized = [(f"{skill}/{rel}", int(size))
                 for rel, size in sorted(files.items())]

        async def _read(path: str) -> bytes:
            data = await self.repo_cache.read_skill_file(
                source, inst.subdir, sha, skill, path[len(skill) + 1:])
            if data is None:
                raise SkillStoreError(502, f"mirror read failed: {path}")
            return data

        return await self._collect(skill, sized, total, _read)

    async def _collect(self, name: str, sized: list[tuple[str, int]],
                       total: int, fetch) -> tuple[list[dict], int, list[str]]:
        """One skill's files within the caps: sizes are checked BEFORE
        fetch; a store exception propagates (payload_for falls back).

        SKILL.md is required: if it is present in the sized tree and
        over MAX_FILE_BYTES, return empty — do not ship helpers alone
        (ADR-0016 Decision 4: size-capped skills must not appear as
        shipped for Required soft-force). Total-cap mid-skill still
        keeps SKILL.md and drops later helpers.
        """
        entry: list[dict] = []
        used = 0
        warns: list[str] = []
        # SKILL.md first: when the total cap lands mid-skill, the manifest
        # must ship and supporting files drop — plain sorted() order could
        # ship an uppercase-named helper file while dropping SKILL.md,
        # leaving a dead skill dir the harness ignores
        anchor = f"{name}/SKILL.md"
        sized = sorted(sized, key=lambda ps: (ps[0] != anchor, ps[0]))
        for path, size in sized:
            if _over_file_cap(size):
                warns.append(f"skill {name!r}: {path} exceeds "
                             f"{MAX_FILE_BYTES} bytes — skipped")
                if path == anchor:
                    # Manifest unusable → whole skill unusable; stop
                    # before fetching any helpers.
                    return [], 0, warns
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
