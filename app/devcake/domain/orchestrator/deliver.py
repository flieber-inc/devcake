"""Deliverable packaging: zip the merged PR change set onto the PMO feed.

Internal/zero-repo missions ALWAYS deliver (docs/16 M11, F4, ADR-0010) —
the internal forge may be invisible, so the PMO is the one place the user
looks. Configured (external) work repos deliver only when the operator
enables AppConfig.attach_merged_changeset_to_pmo (default off: the forge
PR is the canonical eng artifact).

Fired AFTER the Done checkpoint from all three merge sites, guarded by
its own idempotency key so redelivery never double-posts. A packaging
failure NEVER un-Dones the mission — it posts a warning instead.
"""

from __future__ import annotations

import io
import logging
import zipfile

from ...ports.forge import PRFile, run_branch
from . import feed, steps
from .markers import DELIVERABLE_MARKER

log = logging.getLogger("devcake.deliver")

_DELIVER_STEP = steps.DELIVER_ZIP
# leave headroom under the PMO attachment cap for the zip container overhead
_SAFETY = 64 * 1024


async def deliver_internal_zip(mgr, run, pr) -> None:
    """Review-path delivery (auto-merge ON): idempotent on run.finalized_steps."""
    if _DELIVER_STEP in run.finalized_steps:
        return
    delivered = await _deliver_core(mgr, run.repo_ref, run.mission_key,
                                    run.mission_pmo_id, run.pmo_kind, pr)
    if delivered:
        run.finalized_steps.append(_DELIVER_STEP)
        mgr.runs.store.save(run)


def _should_deliver_zip(mgr, repo_ref: str | None) -> bool:
    """Internal repos always; configured repos only when the operator opt-in
    is on (AppConfig.attach_merged_changeset_to_pmo). None/empty never
    delivers — merge sweep only calls this with a resolved m.repo."""
    if not repo_ref:
        return False
    if repo_ref in mgr.forges.internal:
        return True
    return bool(getattr(mgr.config, "attach_merged_changeset_to_pmo", False))


async def deliver_internal_zip_for_mission(mgr, m, pr) -> None:
    """Merge-sweep-path delivery (auto-merge OFF, human merged). The
    Done label-swap that precedes this call normally removes the mission from
    future sweep candidates, so it fires once — but a crash between the swap
    and this call, or a redelivery, could re-enter. Guard durably against a
    double-attach by checking the feed for the deliverable's own filename
    (review finding #9). Scans unquoted bodies only (ADR-0014 D2): a quoted
    mention of the zip name must never suppress a real delivery."""
    if not _should_deliver_zip(mgr, m.repo):
        return
    marker = f"{m.key}-deliverable.zip"
    try:
        act = await mgr.pmo.get_activity(m.ref)
        if any(marker in feed.unquoted(e.body) for e in act.entries):
            return                               # already delivered
    except Exception:  # noqa: BLE001 — idempotency probe is advisory; failure logged and delivery proceeds (a double attach beats a lost deliverable)
        log.warning("deliverable idempotency check failed for %s — proceeding",
                    m.key)
    await _deliver_core(mgr, m.repo, m.key, m.pmo_id, m.pmo_kind, pr)


async def _deliver_core(mgr, repo_ref, mission_key, pmo_id, pmo_kind, pr
                        ) -> bool:
    """Zip the merged change set → PMO feed attachment. Returns True on a
    successful delivery (caller may then record its idempotency marker).
    Best-effort: a failure NEVER un-Dones the mission."""
    if not _should_deliver_zip(mgr, repo_ref):
        return False
    if not feed._attachments_supported(mgr):
        return False
    try:
        forge = mgr.forges.get(repo_ref)
        if forge is None:
            return False
        state = await forge.pr_state(pr.number)
        # Fresh pr_state is the sole SHA source (all adapters normalize
        # merge_commit_sha onto the DTO). Missing SHA → default branch name.
        merge_sha = (
            state.merge_commit_sha
            or getattr(pr, "merge_commit_sha", None)
            or "main"
        )
        listing = await forge.pr_files(pr.number)
        files = [f for f in listing.files if f.status != "removed"]
        truncated = bool(listing.truncated)
        cap = mgr._attachment_cap()
        zip_bytes, omitted = await _build_zip(
            forge, files, merge_sha, cap,
            truncated=truncated, pr_url=state.url)
        name = f"{mission_key}-deliverable.zip"
        url = await mgr.pmo.upload_attachment(pmo_id, name, zip_bytes)
        is_internal = repo_ref in mgr.forges.internal
        note = (f"{DELIVERABLE_MARKER}\n"
                f"📦 For the record: archived the merged change set — "
                f"{len(files) - len(omitted)} file(s) in [{name}]({url}), "
                f"attached to this issue. This zip is the audit copy, not "
                f"the answer.")
        if not is_internal:
            note += (f" Snapshot of the merged change set; the forge PR is "
                     f"canonical: {state.url}.")
        if truncated:
            note += (f" The forge truncated the changed-file list "
                     f"({len(files)} path(s) returned; additional paths "
                     f"unknown) — the complete change set is in the PR "
                     f"{state.url}.")
        elif omitted:
            note += (f" {len(omitted)} file(s) omitted (too large, or a fetch "
                     f"error — see MANIFEST.txt in the zip); the full change "
                     f"set is in the PR {state.url}.")
        await mgr._feed(pmo_id, pmo_kind, note)
        log.info("delivered %s: %s (%d files)", mission_key, name, len(files))
        return True
    except Exception:
        # NEVER un-Done the mission — the merge already landed
        log.exception("deliverable packaging failed for %s", mission_key)
        try:
            await mgr._feed(
                pmo_id, pmo_kind,
                f"{DELIVERABLE_MARKER}\n"
                "⚠️ Deliverable packaging failed — the merged files remain "
                f"available in the repository ({repo_ref}).")
        except Exception:
            log.exception("could not post the delivery-failure notice")
        return False


async def _build_zip(forge, files: list[PRFile], ref: str,
                     cap: int, *, truncated: bool = False,
                     pr_url: str = "") -> tuple[bytes, list[str]]:
    """Zip files at `ref`, largest-last until the cap; omitted files are
    listed in MANIFEST.txt. Returns (zip_bytes, omitted_paths).

    ``truncated`` means the forge withheld some changed paths (names
    unknown) — a distinct MANIFEST arm, never inventing dropped filenames.
    """
    fetched: list[tuple[str, bytes]] = []
    failed_fetch: list[str] = []
    over_cap: list[str] = []
    for f in files:
        try:
            fetched.append((f.path, await forge.file_content(f.path, ref)))
        except Exception:  # noqa: BLE001 — fetch failure is logged and recorded as a real omission (MANIFEST.txt); the zip build continues
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
        # used to be blamed on the size cap). Truncation is a third arm —
        # GitLab overflow names no withheld paths.
        if failed_fetch or over_cap or truncated:
            manifest = ""
            if truncated:
                forge_ref = pr_url or "the forge PR/MR"
                manifest += (
                    "Files omitted (forge truncated the changed-file list; "
                    "additional paths unknown):\n"
                    f"{len(files)} path(s) returned; see {forge_ref} "
                    "for the complete change set.\n")
            if failed_fetch:
                manifest += ("Files omitted (could not be fetched from the "
                             "forge):\n" + "\n".join(failed_fetch) + "\n")
            if over_cap:
                manifest += ("Files omitted (exceeded the attachment "
                             "size cap):\n" + "\n".join(over_cap) + "\n")
            z.writestr("MANIFEST.txt", manifest)
    return buf.getvalue(), failed_fetch + over_cap
