"""ONBOARD decomposition finalization (docs/03 §1.3, ADR-0006/0007/0012)."""

from __future__ import annotations

import hashlib
import json
import logging

from ...security import redact
from ..model import (LABEL_CREATED, LABEL_NEEDS_HUMAN, LABEL_OPTIN, LABEL_SKIP,
                     LABEL_TRACKING, MissionRef)
from ..run import Run
from .markers import (COMMENT_SENTINEL, at_decomposition_limit,
                      decomposition_depth, decomposition_marker)

log = logging.getLogger("devcake.missions")


async def _finalize_decomposition(self, run: Run, result: dict) -> None:
    pmo_id = run.mission_pmo_id
    live = await self.pmo.get(MissionRef(pmo_id, run.pmo_kind))
    # depth is PMO state — the mission's own label + marker (ADR-0012);
    # unknown (label without a readable marker) counts as at-limit, fail-safe.
    # Replay-stable: once any child checkpoint exists the decision was taken
    # under the limit of that moment — a config change mid-resume must finish
    # the wiring, not strand live children behind a SKIP park.
    limit = self.config.max_decomposition_depth
    committed = any(s.startswith("decomp:child:") for s in run.finalized_steps)
    if not committed and at_decomposition_limit(live, limit):
        depth = decomposition_depth(live)
        async def _depth_limit():
            await self.pmo.swap_labels(MissionRef(pmo_id, run.pmo_kind),
                                   remove=set(), add={LABEL_SKIP})
            if live.status == "in_progress":
                await self.pmo.set_status(
                    MissionRef(pmo_id, run.pmo_kind), "backlog")
            await self._feed(
                pmo_id, run.pmo_kind,
                f"⛔ Depth limit: this mission is at decomposition depth "
                f"{depth if depth is not None else 'unknown'} of the "
                f"configured limit {limit} and may not be decomposed "
                f"again. Parked with `DEVCAKE-SKIP` for a human to "
                f"re-scope.")
            self._audit(pmo_id, "depth_limit_rejected", run.run_id)
        await self._checkpoint(run, "decomp:depth_limit", _depth_limit)
        run.verdict = "handed off: decomposition depth limit"
        return
    # children of an unknown-depth parent (unlimited mode) record depth 2 —
    # "at least one generation down" — so a later finite limit still gates
    parent_depth = decomposition_depth(live)
    child_depth = (parent_depth if parent_depth is not None else 1) + 1
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
    # redelivery and secrets scrubbing stay consistent (ISSUES #12). Marker
    # syntax in the untrusted body is defanged (opening backtick stripped)
    # BEFORE hashing: a Dev quoting a decomposition marker would otherwise
    # shadow the child's own footer and confuse the replay child-scan.
    normalized = [{
        "title": redact(str(d.get("title") or f"part {i}")),
        "description": redact(str(d.get("description") or "")).replace(
            "`devcake:decomposition:v1", "devcake:decomposition:v1"),
        "priority": str(d.get("priority") or "medium"),
        "blocked_by": list(d.get("blocked_by") or []),
    } for i, d in enumerate(drafts, start=1)]
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    manifest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    is_project = live.pmo_kind == "project"
    from ..repo_routing import marker_repo
    parent_repo_marker = marker_repo(live.description)

    existing: dict[int, str] = {}
    existing_keys: dict[int, str] = {}
    conflicts: list[str] = []
    for mission in await self.pmo.list_all(self.instance.team_key):
        if LABEL_CREATED not in mission.labels:
            continue
        marker = decomposition_marker(mission.description)
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
            existing_keys[part] = mission.key

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
    child_key_by_part: dict[int, str] = {}                    # part index → key

    async def _resolve_existing_child(part: int) -> tuple[str, str] | None:
        if part in existing:
            return existing[part], existing_keys[part]
        for mission in await self.pmo.list_all(self.instance.team_key):
            marker = decomposition_marker(mission.description)
            if marker and marker.group(1) == pmo_id \
                    and int(marker.group(3)) == part:
                return mission.pmo_id, mission.key
        return None

    for i, d in enumerate(normalized, start=1):
        title = d["title"]
        key_child = f"decomp:child:{i}"
        if i in existing:
            child_id, key = existing[i], existing_keys[i]
        elif key_child in run.finalized_steps:
            found = await _resolve_existing_child(i)
            if found is None:
                raise ValueError(
                    f"decomposition part {i} checkpointed but child missing")
            child_id, key = found
        else:
            footer = (f"\n\n---\n_Created by DevCake from {live.key} — "
                      f"part {i}/{len(normalized)}_\n"
                      f"`devcake:decomposition:v1 parent={pmo_id} "
                      f"manifest={manifest} part={i}/{len(normalized)} "
                      f"depth={child_depth}`")
            if parent_repo_marker:
                # children inherit the parent's repo marker (audit A24,
                # founder decision 2026-07-14) — the family stays on one
                # repo instead of splitting to the default / internal repos.
                # Appended AFTER the manifest hash is computed, like the
                # rest of the footer, so replay idempotency is unaffected.
                footer += f"\n`devcake-repo:{parent_repo_marker}`"
            key, child_id = await self.pmo.create_mission(
                self.instance.team_key, title,
                d["description"] + footer,
                d["priority"], labels,
                # an issue's children stay in its containing project so the
                # tracking sweep waits for them (ADR-0012)
                parent_ref=pmo_id if is_project else live.parent_ref)
            created.append(key)
            run.finalized_steps.append(key_child)
            self.runs.store.save(run)
        child_ids[i] = child_id
        child_key_by_part[i] = key
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
    # all children in part order, reused parts included — key sources are
    # authoritative (create_mission return / marker scan), never a snapshot,
    # so the lineage note below renders identically on every replay
    child_keys = [child_key_by_part[i] for i in sorted(child_key_by_part)]

    if not is_project:
        # Edge inheritance (ADR-0012, fail-closed): replicate the original's
        # dependency topology onto the children BEFORE the cancel in
        # decomp:tracking. Every inherited edge maps 1:1 onto an edge the
        # original already had, so no new cycle can appear. Edges are STRICT,
        # exactly like sibling edges: a failure aborts finalization (its
        # checkpoint records only successes) and the retry re-enters with a
        # fresh snapshot — a transient error heals, a deleted endpoint drops
        # out of the recomputed lists, and a persistent error parks the
        # mission with the original still OPEN and gating its dependents.
        # Canceling past a missing edge would silently release downstream
        # work early — the exact hole this block exists to close.
        # The snapshot is deliberately taken AFTER child creation (a second
        # team read): relations a human added while the children were being
        # created are honored on both sides.
        snapshot = await self.pmo.list_all(self.instance.team_key)
        by_id = {sm.pmo_id: sm for sm in snapshot}
        child_id_set = set(child_ids.values())

        async def _inherit(blocker_id: str, blocked_id: str) -> None:
            try:
                await self.pmo.create_relation(blocker_id, blocked_id)
            except Exception:
                self._audit(blocked_id, "relation_inherit_failed",
                            f"{blocker_id} blocks {blocked_id}")
                raise
            self._audit(blocked_id, "relation_inherited",
                        f"{blocker_id} blocks {blocked_id}")

        # the original's blockers re-read from the post-creation snapshot;
        # off-snapshot blockers count as open (additive fail-safe)
        blocked_by_now = (by_id[pmo_id].blocked_by if pmo_id in by_id
                          else live.blocked_by)
        open_blockers = [
            b for b in blocked_by_now
            if b not in child_id_set
            and (b not in by_id
                 or by_id[b].status not in ("done", "canceled"))]
        dependents = [
            sm.pmo_id for sm in snapshot
            if pmo_id in sm.blocked_by and sm.pmo_id != pmo_id
            and sm.pmo_id not in child_id_set
            and sm.status not in ("done", "canceled")]
        for i in sorted(child_ids):
            child_id = child_ids[i]
            for b in open_blockers:
                async def _in(b=b, child_id=child_id):
                    await _inherit(b, child_id)
                await self._checkpoint(run, f"decomp:inherit:in:{b}->{i}", _in)
            for dep in dependents:
                async def _out(child_id=child_id, dep=dep):
                    await _inherit(child_id, dep)
                await self._checkpoint(run, f"decomp:inherit:out:{i}->{dep}",
                                       _out)

        # lineage note (ADR-0012 hygiene): best-effort BY DESIGN — the note
        # is provenance, not ordering safety, so a failing append (issue at
        # the vendor's description cap, archived mid-finalize) must never
        # block the cancel or burn attempts. Note text is built from stable
        # child keys, so the replay guard matches across deliveries.
        note = ("\n\n---\n_Decomposed by DevCake into "
                + ", ".join(child_keys) + "_")
        async def _parent_note():
            if note in (live.description or ""):
                return
            try:
                await self.pmo.append_description(
                    MissionRef(pmo_id, "issue"), note)
            except Exception as e:
                self._audit(pmo_id, "lineage_note_failed", str(e)[:200])
        await self._checkpoint(run, "decomp:parent_note", _parent_note)

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
            await self.pmo.cancel_mission(MissionRef(pmo_id, "issue"))
            self._audit(pmo_id, "decomposed_canceled", links)
    await self._checkpoint(run, "decomp:tracking", _tracking)
