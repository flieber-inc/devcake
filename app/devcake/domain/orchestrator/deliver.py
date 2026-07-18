"""Deliverable packaging for internal-forge missions (docs/16 M11, F4).

The internal fallback forge may be invisible to the end-user, so a mission
that completes there must still deliver: on the REVIEW-approved merge, the
changed files are zipped and attached to the PMO activity feed — the PMO
stays the one place the user looks (attachment-first policy, docs/05 §4).

Fired AFTER the Done checkpoint from all three merge sites, gated on the
mission's repo being internal, guarded by its own idempotency key so
redelivery never double-posts. A packaging failure NEVER un-Dones the
mission — it posts a warning pointing at the internal PR instead.
"""

from __future__ import annotations

import io
import logging
import zipfile

from ...ports.forge import PRFile, run_branch

log = logging.getLogger("devcake.deliver")

_DELIVER_STEP = "deliver:zip"
# leave headroom under the PMO attachment cap for the zip container overhead
_SAFETY = 64 * 1024


async def deliver_internal_zip(self, run, pr) -> None:
    """Review-path delivery (auto-merge ON): idempotent on run.finalized_steps."""
    if _DELIVER_STEP in run.finalized_steps:
        return
    delivered = await _deliver_core(self, run.repo_ref, run.mission_key,
                                    run.mission_pmo_id, run.pmo_kind, pr)
    if delivered:
        run.finalized_steps.append(_DELIVER_STEP)
        self.runs.store.save(run)


async def deliver_internal_zip_for_mission(self, m, pr) -> None:
    """Merge-sweep-path delivery (auto-merge OFF, human merged in Gitea). The
    Done label-swap that precedes this call normally removes the mission from
    future sweep candidates, so it fires once — but a crash between the swap
    and this call, or a redelivery, could re-enter. Guard durably against a
    double-attach by checking the feed for the deliverable's own filename
    (review finding #9). Scans _unquoted bodies only (ADR-0014 D2): a quoted
    mention of the zip name must never suppress a real delivery."""
    if m.repo not in self.forges.internal:
        return
    marker = f"{m.key}-deliverable.zip"
    try:
        act = await self.pmo.get_activity(m.ref)
        if any(marker in self._unquoted(e.body) for e in act.entries):
            return                               # already delivered
    except Exception:
        log.warning("deliverable idempotency check failed for %s — proceeding",
                    m.key)
    await _deliver_core(self, m.repo, m.key, m.pmo_id, m.pmo_kind, pr)


async def _deliver_core(self, repo_ref, mission_key, pmo_id, pmo_kind, pr
                        ) -> bool:
    """Zip the merged change set → PMO feed attachment. Returns True on a
    successful delivery (caller may then record its idempotency marker).
    Best-effort: a failure NEVER un-Dones the mission."""
    if repo_ref not in self.forges.internal:
        return False                             # external repo — nothing to do
    try:
        forge = self.forges.get(repo_ref)
        state = await forge.pr_state(pr.number)
        merge_sha = getattr(pr, "merge_commit_sha", None) or await _merge_sha(
            forge, pr.number)
        files = [f for f in await forge.pr_files(pr.number)
                 if f.status != "removed"]
        cap = self._attachment_cap()
        zip_bytes, omitted = await _build_zip(forge, files, merge_sha, cap)
        name = f"{mission_key}-deliverable.zip"
        url = await self.pmo.upload_attachment(pmo_id, name, zip_bytes)
        note = (f"📦 Deliverable attached: {len(files) - len(omitted)} file(s) "
                f"from the approved merge — [{name}]({url}).")
        if omitted:
            note += (f" {len(omitted)} file(s) omitted (too large, or a fetch "
                     f"error — see MANIFEST.txt in the zip); the full change "
                     f"set is in the internal PR {state.url}.")
        await self._feed(pmo_id, pmo_kind, note)
        log.info("delivered %s: %s (%d files)", mission_key, name, len(files))
        return True
    except Exception:
        # NEVER un-Done the mission — the merge already landed
        log.exception("deliverable packaging failed for %s", mission_key)
        try:
            await self._feed(
                pmo_id, pmo_kind,
                "⚠️ Deliverable packaging failed — the merged files remain "
                f"available in the internal repository ({repo_ref}).")
        except Exception:
            log.exception("could not post the delivery-failure notice")
        return False


async def _merge_sha(forge, pr_number: int) -> str:
    """Best-effort merge commit sha via a direct PR read (adapters that don't
    surface it on the DTO expose it on the raw payload)."""
    try:
        raw = await forge._req("GET", f"/pulls/{pr_number}")
        return raw.get("merge_commit_sha") or "main"
    except Exception:
        return "main"


async def _build_zip(forge, files: list[PRFile], ref: str,
                     cap: int) -> tuple[bytes, list[str]]:
    """Zip files at `ref`, largest-last until the cap; omitted files are
    listed in MANIFEST.txt. Returns (zip_bytes, omitted_paths)."""
    fetched: list[tuple[str, bytes]] = []
    failed_fetch: list[str] = []
    over_cap: list[str] = []
    for f in files:
        try:
            fetched.append((f.path, await forge.file_content(f.path, ref)))
        except Exception:
            # a fetch failure is a REAL omission — count it (review finding
            # #4: else the feed note over-claims the delivered file count and
            # MANIFEST.txt never mentions it → silent data loss)
            log.warning("could not fetch %s@%s for the deliverable", f.path, ref)
            failed_fetch.append(f.path)
    fetched.sort(key=lambda pb: len(pb[1]))       # small first → fit the most
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        used = 0
        for path, content in fetched:
            if used + len(content) + _SAFETY > cap:
                over_cap.append(path)
                continue
            z.writestr(path, content)
            used += len(content)
        # MANIFEST attributes each omission honestly (audit A16: everything
        # used to be blamed on the size cap)
        if failed_fetch or over_cap:
            manifest = ""
            if failed_fetch:
                manifest += ("Files omitted (could not be fetched from the "
                             "forge):\n" + "\n".join(failed_fetch) + "\n")
            if over_cap:
                manifest += ("Files omitted (exceeded the attachment "
                             "size cap):\n" + "\n".join(over_cap) + "\n")
            z.writestr("MANIFEST.txt", manifest)
    return buf.getvalue(), failed_fetch + over_cap
