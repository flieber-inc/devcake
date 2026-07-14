"""ONBOARD decomposition finalization (docs/03 §1.3, ADR-0006/0007)."""

from __future__ import annotations

import hashlib
import json
import logging

from ...security import redact
from ..model import (LABEL_CREATED, LABEL_NEEDS_HUMAN, LABEL_OPTIN, LABEL_SKIP,
                     LABEL_TRACKING, MissionRef)
from ..run import Run
from .markers import COMMENT_SENTINEL, DECOMPOSITION_MARKER_RE

log = logging.getLogger("devcake.missions")


async def _finalize_decomposition(self, run: Run, result: dict) -> None:
    pmo_id = run.mission_pmo_id
    live = await self.pmo.get(MissionRef(pmo_id, run.pmo_kind))
    if LABEL_CREATED in live.labels:                          # depth limit = 1
        async def _depth_limit():
            await self.pmo.swap_labels(MissionRef(pmo_id, run.pmo_kind),
                                   remove=set(), add={LABEL_SKIP})
            await self._feed(
                pmo_id, run.pmo_kind,
                "⛔ Depth limit: this mission was itself created by "
                "decomposition (`DEVCAKE-CREATED`) and may not be "
                "decomposed again. Parked with `DEVCAKE-SKIP` for a "
                "human to re-scope.")
            self._audit(pmo_id, "depth_limit_rejected", run.run_id)
        await self._checkpoint(run, "decomp:depth_limit", _depth_limit)
        return
    drafts = result.get("decomposition") or []
    if not drafts:
        raise ValueError("decomposed outcome without decomposition list")
    for i, d in enumerate(drafts, start=1):
        deps = d.get("blocked_by") or []
        if not all(isinstance(j, int) and not isinstance(j, bool) and 1 <= j < i
                   for j in deps):
            raise ValueError(
                f"decomposition part {i}: blocked_by must be 1-based indexes "
                f"of EARLIER parts, got {deps!r}")
    # Redact agent-generated fields before hashing and create_mission so
    # redelivery and secrets scrubbing stay consistent (ISSUES #12).
    normalized = [{
        "title": redact(str(d.get("title") or f"part {i}")),
        "description": redact(str(d.get("description") or "")),
        "priority": str(d.get("priority") or "medium"),
        "blocked_by": list(d.get("blocked_by") or []),
    } for i, d in enumerate(drafts, start=1)]
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    manifest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    is_project = live.pmo_kind == "project"

    existing: dict[int, str] = {}
    conflicts: list[str] = []
    for mission in await self.pmo.list_all(self.config.pmo.team_key):
        if LABEL_CREATED not in mission.labels:
            continue
        marker = DECOMPOSITION_MARKER_RE.search(mission.description or "")
        if marker and marker.group(1) == pmo_id:
            prior_manifest = marker.group(2)
            part, total = int(marker.group(3)), int(marker.group(4))
            if prior_manifest != manifest:
                conflicts.append(
                    f"{mission.key} records a different manifest {prior_manifest[:12]}"
                )
                continue
            if total != len(normalized) or part not in range(1, len(normalized) + 1):
                conflicts.append(f"{mission.key} has invalid part marker {part}/{total}")
                continue
            if part in existing:
                conflicts.append(f"part {part} has multiple existing missions")
                continue
            if mission.title != normalized[part - 1]["title"]:
                conflicts.append(f"{mission.key} title disagrees with part {part}")
                continue
            existing[part] = mission.pmo_id

    if conflicts:
        detail = "; ".join(conflicts[:8])
        async def _decomp_conflict():
            await self.pmo.swap_labels(
                MissionRef(pmo_id, live.pmo_kind), remove=set(),
                add={LABEL_NEEDS_HUMAN})
            if live.status == "in_progress":
                await self.pmo.set_status(
                    MissionRef(pmo_id, live.pmo_kind), "backlog")
            baton = (
                "Decomposition replay conflict: no children were created. "
                + detail
                + ". Reconcile the existing `DEVCAKE-CREATED` missions, "
                  "then remove `DEVCAKE-NEEDS-HUMAN` to retry."
            )
            await self._feed(pmo_id, live.pmo_kind, baton)
            if live.pmo_kind == "project":
                await self.pmo.post_feed(
                    MissionRef(pmo_id, "project"),
                    redact(baton) + "\n\n" + COMMENT_SENTINEL)
            self._audit(pmo_id, "decomposition_conflict", detail)
        await self._checkpoint(run, "decomp:conflict", _decomp_conflict)
        run.verdict = "handed off: decomposition replay conflict"
        return

    labels = {LABEL_CREATED}
    if self.config.adoption_mode == "opt_in":
        labels.add(LABEL_OPTIN)
    created = []
    child_ids: dict[int, str] = {}                            # part index → issue id

    async def _resolve_existing_child(part: int) -> str | None:
        if part in existing:
            return existing[part]
        for mission in await self.pmo.list_all(self.config.pmo.team_key):
            marker = DECOMPOSITION_MARKER_RE.search(mission.description or "")
            if marker and marker.group(1) == pmo_id \
                    and int(marker.group(3)) == part:
                return mission.pmo_id
        return None

    for i, d in enumerate(normalized, start=1):
        title = d["title"]
        key_child = f"decomp:child:{i}"
        if i in existing:
            child_id = existing[i]
        elif key_child in run.finalized_steps:
            child_id = await _resolve_existing_child(i)
            if child_id is None:
                raise ValueError(
                    f"decomposition part {i} checkpointed but child missing")
        else:
            footer = (f"\n\n---\n_Created by DevCake from {live.key} — "
                      f"part {i}/{len(normalized)}_\n"
                      f"`devcake:decomposition:v1 parent={pmo_id} "
                      f"manifest={manifest} part={i}/{len(normalized)}`")
            key, child_id = await self.pmo.create_mission(
                self.config.pmo.team_key, title,
                d["description"] + footer,
                d["priority"], labels,
                parent_ref=pmo_id if is_project else None)
            created.append(key)
            run.finalized_steps.append(key_child)
            self.runs.store.save(run)
        child_ids[i] = child_id
        # edges wired immediately per child (crash-safe resume; duplicate
        # relations are tolerated by the adapter) — ADR-0007
        for j in d["blocked_by"]:
            blocker_id = child_ids.get(j)
            if blocker_id:
                rel_key = f"decomp:rel:{j}->{i}"
                async def _rel(blocker_id=blocker_id, child_id=child_id, j=j):
                    await self.pmo.create_relation(blocker_id, child_id)
                    self._audit(child_id, "relation_created",
                                f"blocked by part {j} ({blocker_id})")
                await self._checkpoint(run, rel_key, _rel)
    links = ", ".join(created) or "(all already existed)"
    async def _tracking():
        if is_project:
            await self.pmo.swap_labels(MissionRef(pmo_id, "project"),
                                       remove=set(), add={LABEL_TRACKING})
            self._audit(pmo_id, "decomposed_project", links)
        else:
            await self._feed(
                pmo_id, "issue",
                f"🧩 Decomposed into {len(normalized)} standalone issues: "
                f"{links}. This issue is canceled in their favor.")
            await self.pmo.set_status(MissionRef(pmo_id, "issue"), "canceled")
            self._audit(pmo_id, "decomposed_canceled", links)
    await self._checkpoint(run, "decomp:tracking", _tracking)

