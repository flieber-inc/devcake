"""Mission dispatch, attempt counting, credentials (docs/04 §3.1)."""

from __future__ import annotations

import logging
import os
from datetime import datetime

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind, Status, StatusCode

from ...harness import HARNESSES, missing_referenced_secret_env, resolve_image
from ...house_pins import effective_cli_version
from ...staffing import HarnessNotStaffed, app_digest, require_staffed
from ...ports.forge import mission_branch
from ...telemetry import OTEL_COLLECTOR_URL
from ...config import (AssignmentUnstaffed, DevType,
                       active_prompt_template_for, assignment_for)

MEMORY_MOUNT_SENTENCE = (
    "Memory notebooks are mounted read-only under /workspace/memory/.\n"
    "Everything outside the .claims/ folder is a curated note. Files\n"
    "under .claims/ are unvalidated leads copied from runs. Leads may\n"
    "contradict notes. Check both. Trust neither blindly."
)
from .. import failure_taxonomy
from ..model import Activity, LABEL_FAILED, Mission, MissionType, derive
from ..workspaces import WorkspaceUnavailable
from . import markers
from . import schedule
from .activity_payload import activity_payload, push_activity_repo
from ..run import Run, aware, utcnow
from .feed import is_devcake_comment, stage_of, unquoted
from .markers import STEP_MARKER

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


def resolve_repo(mgr, mission: Mission, all_runs: list | None = None):
    """(repo_name | None, gate_reason | None) — marker > instance default >
    zero-repo gate, STICKY once a run exists (domain/repo_routing.py).
    `all_runs`: pre-fetched store snapshot — the poll loop stamps every
    mission every cycle, so it reads the store ONCE per segment instead of
    once per mission; dispatch's live re-resolve reads fresh."""
    from ..repo_routing import resolve_repo
    if all_runs is None:
        all_runs = mgr.runs.store.all()
    history = sorted(
        (r for r in all_runs
         if r.mission_pmo_id == mission.pmo_id and mgr._run_is_ours(r)
         and r.mission_type != "STEWARD"),
        key=lambda r: aware(r.created_at), reverse=True)
    return resolve_repo(mission, mgr.instance,
                        set(mgr.forges.instances), history)


def steward_repo(mgr) -> str | None:
    """The repo a STEWARD run clones (the entrypoint always clones): the
    instance's default repo when configured, else any configured repo. A
    STEWARD run reads the whole team's relations, not a mission — it never
    routes to a per-mission internal repo (returns None → steward stays idle
    when only the internal forge exists)."""
    for name in (mgr.instance.repos or []):      # set order = preference
        if name in mgr.forges.instances:
            return name
    external = [n for n in mgr.forges.instances if n not in mgr.forges.internal]
    return external[0] if external else None


def _identifying_prompt(mgr, dev_type: DevType) -> str:
    """The Dev Type's identifying prompt via its ACTIVE workflow template
    (2026-07-15); falls back Development → the stored identifying_prompt
    field, warning in the log (and /health) on a broken named template."""
    from ...prompts import templates as prompt_templates
    text, warn = prompt_templates.resolve_devtype_prompt(
        dev_type.name,
        mgr.config.active_devtype_prompts.get(dev_type.name),
        fallback=dev_type.identifying_prompt)
    if warn:
        log.warning("devtype prompt fallback: %s", warn)
    return text


def plan_approval_rule(mgr, mtype) -> str:
    """The per-board {plan_approval_rule} text for a stage prompt (docs/03
    §2a): one of the three prompts.PLAN_APPROVAL_RULE_* wordings while the
    instance gates plans, "" otherwise. REVIEW never carries it — a review
    neither produces nor implements a plan."""
    if not mgr.instance.plan_approval:
        return ""
    from ... import prompts   # lazy, as decomposition_rule below
    return {
        MissionType.ONBOARD: prompts.PLAN_APPROVAL_RULE_ONBOARD,
        MissionType.PLAN: prompts.PLAN_APPROVAL_RULE_PLAN,
        MissionType.EXECUTE: prompts.PLAN_APPROVAL_RULE_EXECUTE,
    }.get(MissionType(mtype), "")


def decomposition_rule(mgr, live: Mission) -> str:
    """The per-mission {decomposition_rule} line for ONBOARD prompts
    (ADR-0012): mirrors the finalizer's gate exactly — a Dev told
    'forbidden' never wastes a run on a decomposed outcome the app would
    park. Depth comes from the mission's own PMO record (label + marker);
    unknown counts as at-limit, fail-safe."""
    from ... import prompts
    limit = mgr.config.max_decomposition_depth
    if not limit:
        return prompts.DECOMPOSITION_RULE_UNLIMITED
    if markers.at_decomposition_limit(live, limit):   # THE shared predicate
        return prompts.DECOMPOSITION_RULE_AT_LIMIT.format(limit=limit)
    return prompts.DECOMPOSITION_RULE_ALLOWED.format(
        depth=markers.decomposition_depth(live), limit=limit)


MAX_BLOCKER_WORK_EXTRAS = 8


def _blocker_mount_ok(mgr, name: str) -> bool:
    """True when a read credential exists for `name` right now — THE shared
    rule (repo_sourcing.blocker_read_credential, ADR-0034; this used to be
    a hand-mirror of `_extra_repos_for`'s blocker branch). This check keeps
    the PROMPT from listing a mount that is already known to be gone
    (cleared internal repo, removed instance)."""
    from ..repo_sourcing import blocker_read_credential
    return blocker_read_credential(mgr, name) is not None


async def resolve_blocker_work(
        mgr, mission: Mission, primary_repo: str,
        all_runs: list | None = None, *,
        max_extras: int = MAX_BLOCKER_WORK_EXTRAS,
) -> tuple[list[dict[str, str]], list[str], list[dict[str, str]]]:
    """Direct done-blockers' work repos for RO mounts, plus their handoffs.

    → (entries, skip_reasons, notes). Entries are `[{repo_ref,
    mission_key}]`, deduped by repo_ref, excluding the mission's own primary
    work repo. Only `status == "done"` blockers mount; canceled/open/
    unreadable are skipped with a reason. Repo_ref comes from the blocker's
    latest run with a non-empty repo_ref (no re-provision of cleared
    internals), and must be mountable NOW (`_blocker_mount_ok`) — extant
    pipelines can be weeks old, so a cleared work repo is a skip, not a
    phantom mount.

    Notes (ADR-0032) are `[{mission_key, title, handoff}]` for every DONE
    blocker, collected BEFORE the same-repo drop, the mountability drop, the
    repo_ref dedup, and the cap — narrative must never vanish with its
    mount. Zero extra PMO calls: the locator already fetched each blocker
    Mission whole, description included.
    """
    entries: list[dict[str, str]] = []
    skip: list[str] = []
    notes: list[dict[str, str]] = []
    if not getattr(mission, "blocked_by", None):
        return entries, skip, notes
    if all_runs is None:
        all_runs = mgr.runs.store.all()
    # Mission-only run index (mirror resolve_repo history): STEWARD/hello never
    # carry a mission work repo. Instance scope is PER BLOCKER now: the
    # locator's attribution (accepted_pmo_refs) decides whose run history may
    # serve each id — gitea_issues pmo_ids are per-repo issue NUMBERS, so a
    # bare id-keyed index would let a purely local blocker mount a peer
    # instance's tree (ADR-0009 amendment; ADR-0017).
    by_mid: dict[str, list] = {}
    for r in all_runs:
        if not getattr(r, "mission_pmo_id", None) or not getattr(r, "repo_ref", None):
            continue
        if r.mission_type in ("STEWARD", "HELLO", "OAUTH"):
            continue
        # legacy default "main" (see run.LEGACY_PMO_REFS — here it is a
        # synthetic REPO name, deliberately narrower: "" is never a repo
        # ref) is not a real resolved work repo name on the
        # sticky path (configured names are operator-chosen; internal names
        # are {instance}-{key}); skip the synthetic default so a pre-v3
        # record cannot invent a mount target.
        if r.repo_ref == "main" and r.repo_ref not in mgr.forges.instances:
            continue
        by_mid.setdefault(r.mission_pmo_id, []).append(r)

    memo: dict = {}   # fresh per dispatch — blocker status is read all-live
    for bid in mission.blocked_by:
        res = await mgr.blocker_locator.resolve(
            bid, local_mgr=mgr, memo=memo)
        if res is None:
            skip.append(f"{bid}: unreadable")
            continue
        b = res.mission
        if b.status != "done":
            if b.status == "canceled":
                skip.append(f"{b.key}: canceled — no work tree")
            else:
                skip.append(f"{b.key}: not done ({b.status})")
            continue
        handoff = markers.handoff_of(b.description)
        notes.append({"mission_key": b.key, "title": b.title,
                      "handoff": handoff[:markers.HANDOFF_EXCERPT_MAX]})
        runs = [r for r in (by_mid.get(bid) or [])
                if getattr(r, "pmo_ref", "") in res.accepted_pmo_refs]
        runs_sorted = sorted(
            runs,
            key=lambda r: aware(r.created_at),
            reverse=True)
        repo_ref = next((r.repo_ref for r in runs_sorted if r.repo_ref), None)
        if not repo_ref:
            skip.append(f"{b.key}: no prior work repo")
            continue
        if repo_ref == primary_repo:
            continue
        if not _blocker_mount_ok(mgr, repo_ref):
            skip.append(f"{b.key}: work repo {repo_ref} unavailable "
                        "(cleared, or no read credential)")
            continue
        entries.append({"repo_ref": repo_ref, "mission_key": b.key})

    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for e in entries:
        if e["repo_ref"] in seen:
            continue
        seen.add(e["repo_ref"])
        deduped.append(e)
    if len(deduped) > max_extras:
        overflow = deduped[max_extras:]
        deduped = deduped[:max_extras]
        skip.append("cap %d: skipped %s" % (
            max_extras, ", ".join(e["mission_key"] for e in overflow)))
    return deduped, skip, notes


def _blocker_repos_note(mgr, entries: list[dict[str, str]],
                        skip_reasons: list[str],
                        notes: list[dict[str, str]] | None = None) -> str:
    """Prompt section naming RO blocker work clones plus each done blocker's
    HANDOFF note (ADR-0032). Empty when there is nothing to say. Handoffs
    render for mounted AND unmounted done blockers — the narrative is
    decoupled from the mount list by design (dedup/cap/same-repo drops must
    never eat it)."""
    notes = notes or []
    handoffs = {n["mission_key"]: n for n in notes}
    if not entries and not skip_reasons \
            and not any(n["handoff"] for n in notes):
        return ""
    lines = []
    listed: set[str] = set()
    for e in entries:
        name = e["repo_ref"]
        url = None
        if mgr.internal_forge is not None:
            creds = mgr.internal_forge.mission_credentials(name)
            if creds is not None:
                url = creds.clone_url
        if url is None:
            inst = mgr.forges.instance(name)
            if inst is not None:
                url = inst.url
        slug = (url or name).rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        listed.add(e["mission_key"])
        lines.append(
            f"- `{e['mission_key']}` (`{name}`) → /workspace/repo/{slug}/")
        n = handoffs.get(e["mission_key"])
        if n and n["handoff"]:
            lines.append(f"  Handoff: {n['handoff']}")
    for n in notes:
        if n["mission_key"] in listed or not n["handoff"]:
            continue
        lines.append(f"- `{n['mission_key']}` — {n['title']} (no work-repo "
                     f"mount)")
        lines.append(f"  Handoff: {n['handoff']}")
    body = (
        "\n### Completed blocker work (read-only)\n"
        "Shallow clones of work repositories from missions that block this "
        "one and are done. Read freely; NEVER modify, commit, or open PRs "
        "here. Your write target remains the primary work repository only.\n"
        "Each blocker's Handoff line is its closing note — what changed, "
        "what was discovered, what downstream should know. Your mission "
        "description predates them: where they conflict, the handoff is "
        "newer — reconcile, and flag the drift in your summary.\n"
    )
    if lines:
        body += "\n" + "\n".join(lines) + "\n"
    if skip_reasons:
        body += "\n" + "\n".join(f"(skipped: {r})" for r in skip_reasons) + "\n"
    return body + "\n"


def _onboard_repo_options(mgr, primary: str) -> str:
    """The multi-repo triage section for ONBOARD prompts (item 2 full scope,
    founder decision 2026-07-15): empty unless the instance's repo SET has
    more than one member. Lists every set repo (primary first) — all of them
    are cloned into the triage workspace — and states the split-by-repo
    decomposition rule."""
    names = [n for n in (mgr.instance.repos or [])
             if n in mgr.forges.instances]
    if len(names) < 2:
        return ""
    ordered = [primary] + [n for n in names if n != primary]
    lines = []
    for i, n in enumerate(ordered):
        inst_x = mgr.forges.instance(n)
        slug = inst_x.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        suffix = ("  ← this mission's repository" if i == 0 else "")
        lines.append(f"- `{n}` → /workspace/repo/{slug}/ ({inst_x.url}){suffix}")
    default = mgr.instance.repos[0]
    return (
        "### This team works across several repositories\n"
        "All of them are cloned READ-ONLY in your workspace for assessment "
        "(ONBOARD writes nothing anywhere — route the work instead):\n"
        + "\n".join(lines) + "\n\n"
        "**Cross-repo work must never be one mission.** If completing this "
        "mission requires changes in more than one repository, take the "
        "high-complexity path: decompose into ONE child per repository, put "
        "a `devcake-repo:<name>` line (backticked, exactly as written here) "
        "in each child's description naming its repository, and order them "
        "with blocked_by where one repository's change depends on another's. "
        f"A child without a marker lands on the default repository "
        f"(`{default}`).\n\n")


def _reference_repos_note(mgr, primary: str) -> str:
    """The read-only reference-repos section for EVERY stage's prompt
    (founder request 2026-07-15): consultation material cloned alongside
    the mission's repository. Empty when the instance has none configured
    (or they all vanished from config)."""
    names = [n for n in (mgr.instance.reference_repos or [])
             if n != primary and n in mgr.forges.instances]
    if not names:
        return ""
    lines = []
    for n in names:
        inst_x = mgr.forges.instance(n)
        slug = inst_x.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        lines.append(f"- `{n}` → /workspace/repo/{slug}/ ({inst_x.url})")
    return (
        "\n### Reference repositories (read-only)\n"
        "Cloned alongside this mission's repository as consultation "
        "material (documentation, style guides, context). Read them freely; "
        "NEVER modify them, commit to them, or open PRs against them:\n"
        + "\n".join(lines) + "\n")


async def dispatch(mgr, mission: Mission, mtype: MissionType,
                   dev_type: DevType) -> Run | None:
    missing = missing_referenced_secret_env(dev_type)
    if missing:
        # founder decision 2026-07-16: a referenced-but-unstored secret env
        # var would exit 14 in-container with the cause buried in a warning
        # — refuse deterministically, burn no attempt, launch no container;
        # pasting the value un-gates on the next poll cycle
        mgr.blocked_reasons[mission.pmo_id] = (
            f"dev type {dev_type.name}: secret env {', '.join(missing)} is "
            "referenced by mcp_setup_commands but has no stored value — "
            "paste it on the admin Config page")
        log.warning("dispatch of %s refused — %s", mission.key,
                    mgr.blocked_reasons[mission.pmo_id])
        return None
    try:
        require_staffed(dev_type, digest=app_digest(),
                        store=getattr(mgr, "receipt_store", None))
    except HarnessNotStaffed as e:
        mgr.blocked_reasons[mission.pmo_id] = str(e)
        log.warning("dispatch of %s refused — %s", mission.key, e)
        return None
    try:
        live = await mgr.pmo.get(mission.ref)                 # live re-read
    except Exception as e:  # noqa: BLE001 — any PMO failure gates this ONE mission (reason recorded + logged); escaping would abort the whole poll segment (audit A1)
        # a PMO failure here (transient or permanent) gates the ONE mission —
        # letting it escape would abort the whole poll segment (audit A1)
        mgr.blocked_reasons[mission.pmo_id] = (
            f"PMO read failed at dispatch: {type(e).__name__}: {str(e)[:150]}")
        log.warning("dispatch of %s refused — PMO read failed: %s", mission.key, e)
        return None
    d = derive(live, mgr.config.adoption_mode)
    if d.mission_type != mtype:
        return None                                            # world moved on
    # per-mission repo resolution, re-checked LIVE at dispatch (M10; sticky —
    # a mid-mission routing change gates instead of re-routing, plan H3;
    # M11: zero-repo missions un-gate onto the internal forge). A forge
    # outage here must gate THIS mission (A1), matching poll's resolve wrap.
    try:
        repo_name, gate_reason = await resolve_repo_live(mgr, live)
    except Exception as e:  # noqa: BLE001 — any forge/PMO failure gates this ONE mission (reason recorded + logged); escaping would abort the whole poll segment (audit A1)
        mgr.blocked_reasons[live.pmo_id] = (
            f"repo resolve failed at dispatch: "
            f"{type(e).__name__}: {str(e)[:150]}")
        log.warning("dispatch of %s refused — repo resolve failed: %s",
                    live.key, e)
        return None
    if repo_name is None:
        mgr.blocked_reasons[live.pmo_id] = gate_reason
        log.info("dispatch of %s refused — %s", live.key, gate_reason)
        return None
    repo = mgr.forges.instance(repo_name)
    forge = mgr.forges.get(repo_name)
    if live.blocked_by:
        open_blockers = await schedule.open_blockers_live(mgr, live)
        if open_blockers:
            # same reason shape as schedule.gate_map so the missions row
            # names the live TOCTOU, not a silent skip
            mgr.blocked_reasons[live.pmo_id] = (
                "blocked by " + ", ".join(open_blockers))
            log.info("dispatch of %s aborted — %s",
                     live.key, mgr.blocked_reasons[live.pmo_id])
            return None

    if mission.pmo_kind == "project":
        seq = 1                       # projects only ever ONBOARD (ADR-0006)
        activity = None
    else:
        # audit A1 twin of pmo.get above: feed failure gates THIS mission,
        # never the whole poll segment (schedule/poll would poll_degrade)
        try:
            activity = await mgr.pmo.get_activity(mission.ref)
        except Exception as e:  # noqa: BLE001 — any PMO failure gates this ONE mission (reason recorded + logged); escaping would abort the whole poll segment (audit A1)
            mgr.blocked_reasons[live.pmo_id] = (
                f"PMO activity read failed at dispatch: "
                f"{type(e).__name__}: {str(e)[:150]}")
            log.warning("dispatch of %s refused — PMO activity read failed: %s",
                        live.key, e)
            return None
        seq = _derive_seq(activity)
    # attempts restart when a human removes DEVCAKE-FAILED (docs/15 §3), a
    # later step finishes, or — per `attempt_reset` (ADR-0026) — a human
    # comment (any, or only one carrying DEVCAKE-RETRY). `unlimited` never
    # gives up; it warns loudly instead.
    attempt = attempt_number(mgr, mission.pmo_id, mtype.value, activity)
    if not await _attempt_gate(mgr, live, mtype, attempt):
        return None

    try:
        assignment = assignment_for(mgr.config, mgr.instance, mtype.value)
    except AssignmentUnstaffed as e:
        mgr.blocked_reasons[live.pmo_id] = str(e)
        log.warning("dispatch of %s refused — %s", live.key, e)
        return None
    from ..ids import RunIdOverflow, make_run_id
    try:
        run_id = make_run_id(mgr.instance_name, mission.key, seq, mtype.value)
    except RunIdOverflow as e:
        # an inflated seq (forged/overflowed `N_TYPE.md` feed marker) must
        # gate this mission, never wedge the poll segment (audit A15)
        mgr.blocked_reasons[live.pmo_id] = (
            f"run id would exceed the 64-char budget (step marker seq {seq}) "
            f"— remove or fix the oversized `N_{mtype.value}.md` marker in "
            f"the activity feed")
        log.warning("dispatch of %s refused — %s", live.key, e)
        return None

    # RO mounts of done blockers' work repos (snapshot for runspec + prompt).
    # Hoisted above the activity push (ADR-0024): the mirror gate needs the
    # blocker set, and gating AFTER the push would write one activity
    # snapshot commit per gated poll cycle. resolve_blocker_work is
    # side-effect-free (store + locator reads only).
    all_runs = mgr.runs.store.all()
    blocker_entries, blocker_skips, blocker_notes = await resolve_blocker_work(
        mgr, live, repo_name, all_runs)

    # ADR-0024: fail-closed mirror precondition (docs/07 §5a). NOT a run
    # failure — no container launches, no attempt burns; the reason lands on
    # the missions row via the shared gate dict and retries next cycle.
    # ADR-0016 addendum: skill-source cards (`<card>/<skill>` in the Dev
    # Type's skills) JOIN the same needed-set — fail-closed by founder
    # ruling, as a set-union in the ONE gate, never a second gate. They are
    # deliberately NOT in sourced_repo_names: skills feed the payload, they
    # are never cloned into /workspace.
    from ..repo_sourcing import (classify_context_failures, memory_mount_names,
                                 resolved_skill_cards,
                                 unresolvable_memory_cards)
    sourced = set(mgr.repo_cache.needed_for(
        work_repo=repo_name, mission_type=mtype.value,
        instance=mgr.instance, blocker_entries=blocker_entries,
        dev_type=dev_type, config=mgr.config))
    # ADR-0039: skill cards join the union RESOLVED to physical mirror
    # names; a backing card that also rides sourcing classifies as
    # sourcing (the subtraction below) — resolved_skill_cards documents
    # the rule once for both gates (steward_service shares it).
    skill_cards = resolved_skill_cards(dev_type.skills, mgr.repo_cache)
    needed = sorted(sourced | skill_cards)
    ok, why = await mgr.repo_cache.ensure_fresh(needed)
    memory_cards = set(memory_mount_names(
        instance=mgr.instance, dev_type=dev_type, repo_ref=repo_name))
    stale_cards: set[str] = set()
    omit_cards: set[str] = set()
    if not ok:
        defer, stale_cards, omit_cards = classify_context_failures(
            why, context_cards=memory_cards | (skill_cards - sourced),
            strict=mgr.config.context_sourcing_strict,
            has_mirror=getattr(mgr.repo_cache, "has_last_good",
                               lambda n: False))
        if defer:
            # provisioning family (ADR-0025) — no container, no attempt.
            # Never DEV_BAD_OUTPUT; never a Dev-type breaker.
            mgr.blocked_reasons[live.pmo_id] = (
                "repository mirror not fresh — dispatch deferred: "
                + "; ".join(f"{n}: {r}" for n, r in sorted(defer.items())))
            log.warning("dispatch of %s deferred — %s", live.key,
                        mgr.blocked_reasons[live.pmo_id])
            return None
    # PLAN_MEMORY §3.5, second half: the mirror gate only sees
    # mirror-eligible names — dangling bindings and uncredentialed
    # internal-forge notebooks must resolve here or the run would start
    # memoryless while its prompt claims the mount.
    bad_memory = unresolvable_memory_cards(
        mgr.config, memory_cards - omit_cards,
        mirror_eligible=mgr.repo_cache.eligible)
    if bad_memory:
        if mgr.config.context_sourcing_strict:
            mgr.blocked_reasons[live.pmo_id] = (
                "memory mounts unavailable — dispatch deferred: "
                + "; ".join(f"{n}: {r}" for n, r in sorted(bad_memory.items())))
            log.warning("dispatch of %s deferred — %s", live.key,
                        mgr.blocked_reasons[live.pmo_id])
            return None
        omit_cards |= set(bad_memory)
    needed = [n for n in needed if n not in omit_cards]
    if stale_cards:
        log.warning("dispatch of %s proceeding on stale cache for %s",
                    live.key, ", ".join(sorted(stale_cards)))
    if omit_cards:
        log.warning("dispatch of %s omitting unavailable context %s",
                    live.key, ", ".join(sorted(omit_cards)))

    # ADR-0014 D4 + ADR-0036: build the activity payload (own folder +
    # upstream/{KEY}/ ancestor mirrors) BEFORE the snapshot push so a
    # strict upstream gap can fail-closed with no attempt burned. The
    # push itself still never gates (Gitea down → Redis fallback); only
    # unreadable ancestor *content* under context_sourcing_strict does.
    activity = await activity_payload(
        mgr, live.pmo_id, live.pmo_kind, blocker_notes=blocker_notes)
    upstream_gaps = activity.get("upstream_gaps") or []
    if upstream_gaps and mgr.config.context_sourcing_strict:
        mgr.blocked_reasons[live.pmo_id] = (
            "upstream activity unavailable — dispatch deferred: "
            + "; ".join(f"{g.get('key')}: {g.get('reason')}"
                        for g in upstream_gaps))
        log.warning("dispatch of %s deferred — %s", live.key,
                    mgr.blocked_reasons[live.pmo_id])
        return None
    feed_watermark = await push_activity_repo(
        mgr, live, mtype, seq, blocker_notes=blocker_notes,
        payload=activity)

    blocker_note = _blocker_repos_note(mgr, blocker_entries, blocker_skips,
                                       blocker_notes)

    with tracer.start_as_current_span("mission.dispatch", kind=SpanKind.PRODUCER) as span:
        span.set_attribute("devcake.run.id", run_id)
        span.set_attribute("devcake.mission.key", mission.key)
        span.set_attribute("devcake.mission.type", mtype.value)
        span.set_attribute("devcake.dev_type", dev_type.name)
        span.set_attribute("devcake.run.attempt", attempt)
        carrier: dict[str, str] = {}
        inject(carrier)
        traceparent = carrier.get("traceparent", "")

        from ...prompts import (execute_prompt, onboard_prompt, plan_prompt,
                              review_prompt)
        from ...prompts import templates as prompt_templates

        def _pb(mt_name: str) -> str:
            # ONLY read path for playbook selection (CAKE-150 / ADR-0037):
            # per-PMO override when present, else the global active. A
            # missing/corrupt file falls back to the built-in default —
            # dispatch never fails on template trouble (warning also in
            # /health).
            active = active_prompt_template_for(
                mgr.config, mgr.instance, mt_name)
            text, warn = prompt_templates.resolve_playbook(mt_name, active)
            if warn:
                log.warning("prompt template fallback: %s", warn)
            return text

        repo_slug = repo.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        ref_note = _reference_repos_note(mgr, repo_name)
        ident = _identifying_prompt(mgr, dev_type)
        prompt = {
            MissionType.ONBOARD: lambda: onboard_prompt(
                ident, live, playbook=_pb("ONBOARD"),
                repo_options=_onboard_repo_options(mgr, repo_name),
                reference_repos=ref_note,
                blocker_repos=blocker_note,
                decomposition_rule=decomposition_rule(mgr, live),
                plan_approval_rule=plan_approval_rule(mgr, mtype),
                discoveries_cap=mgr.config.budgets.discoveries_per_run),
            MissionType.PLAN: lambda: plan_prompt(
                ident, live, playbook=_pb("PLAN"),
                reference_repos=ref_note,
                blocker_repos=blocker_note,
                plan_approval_rule=plan_approval_rule(mgr, mtype)),
            MissionType.EXECUTE: lambda: execute_prompt(
                ident, live, repo_slug,
                pr_instructions=forge.descriptor.pr_instructions,
                default_branch=repo.default_branch,
                playbook=_pb("EXECUTE"),
                reference_repos=ref_note,
                blocker_repos=blocker_note,
                plan_approval_rule=plan_approval_rule(mgr, mtype),
                discoveries_cap=mgr.config.budgets.discoveries_per_run),
            MissionType.REVIEW: lambda: review_prompt(
                ident, live, playbook=_pb("REVIEW"),
                reference_repos=ref_note,
                blocker_repos=blocker_note,
                discoveries_cap=mgr.config.budgets.discoveries_per_run),
        }[mtype]()

        spec_env = _protocol_spec_env(mgr,
            mission_id=mission.pmo_id, mission_key=mission.key,
            mission_type=mtype.value, dev_type=dev_type, seq=seq,
            extra_args=assignment.extra_cli_args, repo=repo, forge=forge,
            recover_misplaced_result=mgr.config.recover_misplaced_result,
            continuation_policy=mgr.config.continuation_policy,
            max_continuations=mgr.config.max_continuations,
            mirror_path=(str(mgr.repo_cache.mirror_path(repo_name))
                         if mgr.repo_cache.eligible(repo_name) else ""),
            lfs=mgr.config.repo_mirror.lfs)
        run = Run(
            run_id=run_id, mission_key=mission.key, mission_type=mtype.value,
            pmo_kind=mission.pmo_kind, mission_url=mission.url or "",
            pmo_ref=mgr.instance_name, repo_ref=repo_name,
            dev_type=dev_type.name, seq=seq, attempt_of_step=attempt,
            timeout_seconds=mgr.config.dev_timeout_minutes * 60,
            traceparent=traceparent,
            spec_env=spec_env,
            blocker_work=list(blocker_entries),
            mirror_repos=list(needed),
            feed_watermark=dict(feed_watermark),
        )
        run.memory_mounts = await _memory_mount_snapshot(
            mgr, dev_type, repo_name, stale=stale_cards, omit=omit_cards)
        run.spec_skills = await _skill_payload(mgr, dev_type)
        run.spec_skills_dir = HARNESSES[dev_type.harness_template].skills_dir or ""
        run.skill_repo_heads = await _skill_repo_heads(mgr, dev_type)
        run.spec_prompt = append_memory_sentence(
            append_required_skills(
                prompt, dev_type.skills_required, run.spec_skills),
            run.memory_mounts)
        run.branch = mission_branch(mgr.instance_name, mission.key)
        run.stage_label_at_dispatch = stage_of(live)
        run.mission_pmo_id = mission.pmo_id
        try:
            await mgr.runs.bootstrap.launch(
                run, image=resolve_image(dev_type))
        except WorkspaceUnavailable as e:
            # AUD-001/002: the workspace base is unusable — gate exactly like
            # the mirror precondition above (no attempt burned; the run was
            # unwound in launch). Visible on the missions row + /health, and
            # the SPA's "dispatch is frozen" alert is now backed by real
            # gating. Retries next cycle.
            mgr.blocked_reasons[live.pmo_id] = (
                f"workspace base unusable — dispatch deferred: {e}")
            log.warning("dispatch of %s deferred — %s", live.key,
                        mgr.blocked_reasons[live.pmo_id])
            return None

        if live.status == "backlog":
            await mgr.pmo.set_status(mission.ref, "in_progress")
            mgr._audit(mission.pmo_id, "set_status", "in_progress")
        log.info("dispatched %s (attempt %d, dev=%s)", run_id, attempt, dev_type.name)
        return run


async def _skill_payload(mgr, dev_type: DevType) -> list[dict]:
    """Skill-store files for this Dev Type's runs. Delivery is registry-
    driven: a harness with no skills_dir (harness.py) doesn't read personal
    skills anywhere, so a selection there is skipped with a warning — never
    a refused run."""
    if not dev_type.skills or getattr(mgr, "skills", None) is None:
        return []
    if HARNESSES[dev_type.harness_template].skills_dir is None:
        log.warning("dev type %s: skills %s configured but harness %s does "
                    "not support them — skipped", dev_type.name,
                    dev_type.skills, dev_type.harness_template)
        return []
    try:
        payload, warnings = await mgr.skills.payload_for(dev_type.skills)
    except Exception as e:  # noqa: BLE001 — skills are additive and must never refuse a run; dispatch proceeds without them (logged)
        # payload_for swallows STORE errors, but a bundled-copy read can
        # still raise — skills are additive and must never refuse a run
        log.warning("skills for %s unavailable — dispatching without them "
                    "(%s)", dev_type.name, e)
        return []
    for w in warnings:
        log.warning("skills for %s: %s", dev_type.name, w)
    return payload


async def _skill_repo_heads(mgr, dev_type: DevType) -> dict[str, str]:
    """{card: sha} provenance stamp for `<card>/<skill>` skills (ADR-0016
    addendum) — read at dispatch, right after the gate proved the mirrors
    fresh. Best-effort: an unreadable head simply isn't recorded (the
    payload warning already named it)."""
    from ..repo_sourcing import skill_source_cards
    heads: dict[str, str] = {}
    for card in sorted(skill_source_cards(dev_type.skills)):
        sha = await mgr.repo_cache.tree_head(card)
        if sha:
            heads[card] = sha
    return heads


def append_memory_sentence(prompt: str, memory_mounts: list) -> str:
    """PLAN_MEMORY D1: factual mount sentence, only when the run has
    consumer memory mounts. Curator runs (empty snapshot) get none."""
    if not memory_mounts:
        return prompt
    return f"{prompt}\n\n{MEMORY_MOUNT_SENTENCE}"


async def _memory_mount_snapshot(mgr, dev_type, repo_ref: str, *,
                                 stale: set[str], omit: set[str]) -> list[dict]:
    """Dispatch-time provenance for each consumer memory mount (§3.6)."""
    from ..repo_sourcing import memory_mount_names
    inst_set = set(mgr.instance.memory_repos or [])
    dt_set = set(getattr(dev_type, "memory_repos", None) or [])
    out: list[dict] = []
    for card in memory_mount_names(
            instance=mgr.instance, dev_type=dev_type, repo_ref=repo_ref):
        if card in omit:
            continue
        if card in inst_set and card in dt_set:
            binding = "both"
        elif card in inst_set:
            binding = "board"
        else:
            binding = "domain"
        sha = await mgr.repo_cache.tree_head(card) or ""
        if not sha:
            # mirror-ineligible card (bundled Gitea): no mirror to read —
            # ask the forge directly so §3.6 provenance isn't blank in the
            # pilot's default deployment (review N1)
            remote_head = getattr(mgr.repo_cache, "remote_head", None)
            if callable(remote_head):
                sha = (await remote_head(card)) or ""
        out.append({
            "card": card,
            "binding": binding,
            "commit": sha,
            "stale_cache": card in stale,
            # snapshot the failure class at dispatch (PLAN_MEMORY §3.5):
            # the provision step makes a strict mount's clone failure fatal,
            # and a mid-flight toggle must not change a live run's contract
            "strict": bool(mgr.config.context_sourcing_strict),
            "path": f"/workspace/memory/{card}",
        })
    return out


def append_required_skills(prompt: str, skills_required: list[str],
                           shipped: list[dict]) -> str:
    """Soft-force append: instruct the Dev to consult skills that were both
    marked Required on the Dev Type AND actually shipped in the runspec
    payload. Instructional only — harnesses do not hard-enforce skill load.
    Order follows skills_required; missing/cap-dropped skills are omitted."""
    if not skills_required:
        return prompt
    have = {s.get("name") for s in shipped or [] if s.get("name")}
    names = [n for n in skills_required if n in have]
    if not names:
        return prompt
    # external `<card>/<skill>` names install under the BASENAME dir
    # (flattened payload paths) — the instruction must name a dir that
    # exists in the container, with the origin as provenance
    lines = "\n".join(
        f"- `{n.rsplit('/', 1)[-1]}` (from skill source `{n.split('/', 1)[0]}`)"
        if "/" in n else f"- `{n}`" for n in names)
    return (
        f"{prompt}\n\n### Required skills\n"
        "You must consult the following skill(s) before acting on this mission "
        "(they are installed in your harness skills directory; open each "
        "skill's SKILL.md and apply what is relevant — do not skip them):\n"
        f"{lines}"
    )


def _protocol_spec_env(mgr, *, mission_id: str, mission_key: str,
                       mission_type: str, dev_type: DevType, seq: int,
                       extra_args: str, repo, forge,
                       recover_misplaced_result: bool = True,
                       continuation_policy: str = "auto",
                       max_continuations: int = 2,
                       mirror_path: str = "",
                       lfs: bool = False) -> dict[str, str]:
    """The Dev-protocol env contract (docs/07 §3), built in exactly one
    place so mission and steward dispatches can never drift apart — a var
    missing on one path would crash the entrypoint's strict readers."""
    return {
        "DEVCAKE_MISSION_ID": mission_id,
        "DEVCAKE_MISSION_KEY": mission_key,
        "DEVCAKE_MISSION_TYPE": mission_type,
        "DEVCAKE_DEV_TYPE": dev_type.name,
        "DEVCAKE_HARNESS": dev_type.harness_template,  # app-authoritative
        "DEVCAKE_SEQ": str(seq),
        "DEVCAKE_REPO_URL": repo.url,
        "DEVCAKE_DEFAULT_BRANCH": repo.default_branch,
        "DEVCAKE_CLONE_USER": forge.descriptor.clone_user,
        "DEVCAKE_GIT_NAME": forge.descriptor.git_user_name,
        "DEVCAKE_GIT_EMAIL": forge.descriptor.git_email,
        "DEVCAKE_FORGE_CLI_ENVS": ",".join(forge.descriptor.cli_token_envs),
        "DEVCAKE_EXTRA_ARGS": extra_args,
        # "1"/"" — NEVER str(bool): the entrypoint tests these with bare
        # truthiness, and "False" is truthy, which would make a default-True
        # flag impossible to switch off (ADR-0018)
        "DEVCAKE_RECOVER_MISPLACED_RESULT": (
            "1" if recover_misplaced_result else ""),
        # ADR-0022 — a string enum and an int-string, not flags; the
        # entrypoint's continuation_config parses defensively (garbage → off)
        "DEVCAKE_CONTINUATION_POLICY": continuation_policy,
        "DEVCAKE_MAX_CONTINUATIONS": str(max_continuations),
        # ADR-0024 — the work repo's mirror ("" = direct clone: internal
        # repos only); LFS flag rides the "1"/"" convention (never str(bool))
        "DEVCAKE_MIRROR_PATH": mirror_path,
        "DEVCAKE_LFS": "1" if lfs else "",
        "DEVCAKE_MODEL": (dev_type.model
                          or HARNESSES[dev_type.harness_template].default_model),
        "DEVCAKE_CLI_VERSION": effective_cli_version(dev_type),
        # Devs export through the collector, credential-free
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"{OTEL_COLLECTOR_URL}/v1/traces",
    }


def runspec_secret_payload(mgr, run: Run) -> dict | None:
    """Secret half of a run spec, built from current config on request
    (docs/09 §5): nothing secret is at rest between dispatch and the Dev's
    runspec.get, and a slow container start or Redis restart cannot expire
    it. verify_auth has already authenticated the requester.

    May raise ``CredentialRefreshError`` (fail closed): host-side Grok OAuth
    refresh failed — ingress maps it to ``runspec.error`` with a reconnect
    message so the Dev never boots on a known-bad session.
    """
    dt = mgr.dev_types.get(run.dev_type)
    if dt is None:
        return None            # dev type deleted mid-run → runspec.error
    env_creds, spec_files = _credential_spec(mgr, dt)
    # Stage-scope forge credentials: every stage clones the repo (entrypoint
    # always git-clones), so all stages need a clone-capable token. EXECUTE
    # gets the write token (push/PR). Other stages prefer token_ro when set,
    # else fall back to the write token so private repos keep working without
    # a separate RO PAT. Reviewer PAT stays app-side only.
    repo = mgr.forges.instance(run.repo_ref)
    if repo is None:
        # the run's repo vanished from config mid-flight → runspec.error
        # (the resolution-failure contract, domain/forge_runtime.py)
        log.error("runspec for %s refused: repo %r is no longer configured",
                  run.run_id, run.repo_ref)
        return None
    env: dict[str, str] = {**env_creds}
    if run.repo_ref in mgr.forges.internal and mgr.internal_forge is not None:
        # internal fallback forge (M11): Dev tokens are the mission's
        # per-user scoped pair (NOT env vars) — write for EXECUTE, read
        # elsewhere; isolation lives in the token scope (docs/14 §2 Zone B / ADR-0010)
        creds = mgr.internal_forge.mission_credentials(run.repo_ref)
        if creds is None:
            log.error("runspec for %s refused: internal repo %r credentials "
                      "missing", run.run_id, run.repo_ref)
            return None
        env["DEVCAKE_FORGE_TOKEN"] = (creds.token_write
                                      if run.mission_type == "EXECUTE"
                                      else creds.token_read)
    else:
        write = repo.token
        ro = repo.token_ro
        env["DEVCAKE_FORGE_TOKEN"] = (write if run.mission_type == "EXECUTE"
                                      else ro or write)
    # one shared tail — the forge branches differ ONLY in token choice
    payload = {"env": env, "credential_files": spec_files}
    extras = _extra_repos_for(mgr, run)   # references reach zero-repo
    if extras:                            # missions too
        payload["extra_repos"] = extras
    memory = _memory_repos_for(mgr, run)
    if memory:
        payload["memory_repos"] = memory
    if dt.dev_entrypoint.strip():
        payload["dev_entrypoint"] = dt.dev_entrypoint
        payload["mcp_setup_commands"] = list(dt.mcp_setup_commands)
    if dt.override_harness_adapter:
        payload["override_harness_adapter"] = True
    url = (dt.backend_base_url or "").strip()
    if url:
        payload["backend_base_url"] = url
    # ADR-0014 D4: the activity-repo RO clone spec (secret half — the token
    # never rests on the Run). Absent for STEWARD (no mission, no repo), when
    # the forge is off, or before the boot mint — the entrypoint then falls
    # back to the Redis materialization.
    if mgr.internal_forge is not None and run.mission_type != "STEWARD":
        from ...ports.internal_forge import activity_repo_name
        creds = mgr.internal_forge.activity_credentials(
            activity_repo_name(mgr.instance_name, run.mission_key))
        if creds is not None:
            payload["activity_repo"] = {"url": creds.clone_url,
                                        "clone_user": creds.username,
                                        "token": creds.token}
    return payload


def _mirrored(mgr, run: Run, name: str) -> bool:
    """Whether an extra rides the mirror — decided ONCE, at dispatch (2026-08
    evaluation F12). The gate's `needed_for` set is snapshotted on
    `run.mirror_repos`, exactly like the primary's DEVCAKE_MIRROR_PATH in
    spec_env; re-deriving here from live config let a repo added to the
    instance between dispatch and runspec.get receive a mirror_path the gate
    never created (the Dev would `git clone file://` a nonexistent path).
    Legacy records (empty snapshot, pre-field) keep the live derivation.
    Belt: a snapshot member whose mirror vanished mid-flight (operator rm)
    falls back to the token path instead of a dead file:// URL — extra-clone
    failures are non-fatal in the entrypoint, but a silent context omission
    beats a confusing one."""
    snapshot = run.mirror_repos or []
    decided = (name in snapshot) if snapshot else mgr.repo_cache.eligible(name)
    if not decided:
        return False
    if not mgr.repo_cache.mirror_path(name).is_dir():
        log.warning("mirror for %r vanished between dispatch and runspec of "
                    "%s — falling back to a token clone", name, run.run_id)
        return False
    return True


def _extra_repos_for(mgr, run: Run) -> list[dict]:
    """Read-only sibling clones for a run, built at request time (nothing
    secret at rest); read tokens preferred, write fallback (same rule as
    the primary token). Sourcing comes from THE shared rule
    (repo_sourcing.sourced_repo_names, ADR-0034) — the same list the mirror
    gate freshens, so the two structurally cannot disagree; this function
    only enriches each name with its clone credential/mirror.

    Tokens that resolve empty are omitted (a clone with no credential is
    never useful and would only burn a non-fatal failure note).
    """
    from ..repo_sourcing import (blocker_read_credential, memory_mount_names,
                                 sourced_repo_names)
    blocker_names = {(bw.get("repo_ref") or "")
                     for bw in run.blocker_work or []}
    dt = (mgr.dev_types.get(run.dev_type)
          if getattr(mgr, "dev_types", None) else None)
    memory_names = {m.get("card") for m in (run.memory_mounts or [])
                    if m.get("card")}
    if not memory_names:
        memory_names = set(memory_mount_names(
            instance=mgr.instance, dev_type=dt, repo_ref=run.repo_ref))
    extras = []
    for name in sourced_repo_names(
            work_repo=run.repo_ref, mission_type=run.mission_type,
            instance=mgr.instance, blocker_entries=run.blocker_work,
            dev_type=dt, config=getattr(mgr, "config", None)):
        if name == run.repo_ref or name in memory_names:
            continue                     # primary / consumer memory dest
        if name in blocker_names:
            cred = blocker_read_credential(mgr, name)
            if cred is not None and cred[0] == "internal":
                # internal mission work repo: token_read from stored
                # credentials. Gitea ignores userinfo in the clone URL
                # (descriptor clone_user is "devcake"); askpass carries
                # token_read.
                creds = cred[1]
                extras.append({
                    "name": name, "url": creds.clone_url,
                    "clone_user": creds.username or "devcake",
                    "token": creds.token_read})
                continue
        inst_x = mgr.forges.instance(name)
        forge_x = mgr.forges.get(name)
        if inst_x is None or forge_x is None:
            continue         # removed mid-flight — proceed on what remains
        if _mirrored(mgr, run, name):
            # ADR-0024: mirrored — the clone needs NO token (fewer secrets in
            # transit), and the tokenless-omit rule below must not apply: the
            # mirror gate already proved fetchability, and omitting here while
            # the gate blocks on the same repo would be incoherent
            extras.append({"name": name, "url": inst_x.url,
                           "clone_user": forge_x.descriptor.clone_user,
                           "mirror_path": str(mgr.repo_cache.mirror_path(name))})
            continue
        token = inst_x.token_ro or inst_x.token
        if not token:
            continue
        extras.append({"name": name, "url": inst_x.url,
                       "clone_user": forge_x.descriptor.clone_user,
                       "token": token})
    return extras


def _memory_repos_for(mgr, run: Run) -> list[dict]:
    """Consumer memory clones for the runspec — dest is
    /workspace/memory/<card>/, decided by the dispatch snapshot only."""
    out: list[dict] = []
    for mount in run.memory_mounts or []:
        name = mount.get("card") or ""
        inst_x = mgr.forges.instance(name)
        forge_x = mgr.forges.get(name)
        if inst_x is None or forge_x is None:
            # unreachable in the normal flow — dispatch resolved every
            # snapshot card (§3.5); only a mid-flight card deletion lands
            # here, and the snapshot doctrine says serve what remains
            log.warning("memory mount %r vanished after dispatch — "
                        "omitted from the runspec of %s", name, run.run_id)
            continue
        entry: dict = {"name": name, "url": inst_x.url,
                       "clone_user": forge_x.descriptor.clone_user}
        if mount.get("strict"):
            entry["strict"] = True
        if _mirrored(mgr, run, name):
            entry["mirror_path"] = str(mgr.repo_cache.mirror_path(name))
        else:
            token = inst_x.token_ro or inst_x.token
            if not token:
                continue
            if not inst_x.token_ro:
                # PLAN_MEMORY §4 promises read-only tokens on memory
                # mounts; the write token is used TRANSIENTLY for the
                # provision clone only (askpass env, never persisted in
                # the clone) — store a token_ro to close even that window
                log.warning("memory mount %r has no token_ro — provision "
                            "clone will use the write token transiently",
                            name)
            entry["token"] = token
        if mount.get("stale_cache"):
            entry["stale_cache"] = True
        out.append(entry)
    return out


def _credential_spec(mgr, dev_type: DevType) -> tuple[dict[str, str], list[dict]]:
    """Harness credentials for a run spec: requirements come from the
    harness registry, secret material from /data/secrets/{dev_type}/
    (docs/08 §4)."""
    harness = HARNESSES[dev_type.harness_template]
    # harness/model key VALUES are GUI-stored (schema v4, F5): read them from
    # /data/secrets/harness/, no os.environ indirection
    from ... import secrets as _secrets
    env = {var: v for var in harness.credential_env
           if (v := _secrets.read_harness_secret(var))}
    # Dev-Type-declared secret env: named refs into the same GUI store, so
    # mcp_setup_commands can reference e.g. $DD_API_KEY without a value ever
    # touching config.yaml (ADR-0011). Missing value = warn-and-proceed —
    # unless an mcp_setup_command references it, which gates dispatch before
    # a container ever launches (missing_referenced_secret_env).
    for var in dev_type.secret_env:
        if (v := _secrets.read_harness_secret(var)):
            env[var] = v
        else:
            # Logs the env var NAME only — values never reach this call.
            log.warning("secret env %s for dev type %s not stored — add it "
                        "on the admin Config page", var, dev_type.name)
    files = []
    for cf in harness.credential_files:
        content = _secrets.read_credential_file(dev_type.name, cf.secret_file)
        if content is None:
            log.warning(
                "credential file %s missing for %s — connect via OAuth "
                "or upload it on the admin Config page",
                cf.secret_file, dev_type.name,
            )
            continue
        # Host-side Grok OAuth refresh at the inject chokepoint (no Dev
        # write-back). Fail closed → CredentialRefreshError → runspec.error.
        if (cf.secret_file == "grok-auth.json"
                and getattr(mgr, "oidc_tokens", None) is not None):
            from ..grok_oauth import (CredentialRefreshError,
                                      ensure_fresh_for_inject)

            def _write(text: str, _dt=dev_type.name, _fn=cf.secret_file) -> None:
                _secrets.write_credential_file(_dt, _fn, text)

            _dt, _fn = dev_type.name, cf.secret_file
            _snapshot = content

            def _reread(_dt=_dt, _fn=_fn, _snapshot=_snapshot) -> str:
                return _secrets.read_credential_file(_dt, _fn) or _snapshot

            try:
                content = ensure_fresh_for_inject(
                    content,
                    token_port=mgr.oidc_tokens,
                    write_full=_write,
                    lock_path=_secrets.credential_path(
                        dev_type.name, ".grok-auth.lock"),
                    reread=_reread,
                )
            except CredentialRefreshError:
                raise
        files.append({"path_hint": cf.path_hint,
                      "content": content, "mode": "600"})
    return env, files


def _derive_seq(activity) -> int:
    """docs/02 §8 — max step number among prior feed artifacts + 1 (max, not
    count: collision-proof when a human deletes a transcript comment). Scans
    unquoted bodies only (ADR-0014 D2): quoted marker mentions never count."""
    steps = [int(m.group(1)) for e in activity.entries
             for m in STEP_MARKER.finditer(unquoted(e.body))]
    return (max(steps) + 1) if steps else 1


def _last_giveup_at(mgr, pmo_id: str) -> datetime | None:
    """Delegates to the incremental audit-tail reader (markers.last_giveup_at
    — 2026-08 evaluation F8: the full-file rescan per candidate per cycle is
    gone). Kept as a seam: tests and attempt_number call through here.
    Instance-scoped so colliding bare pmo_ids across PMO instances do not
    share a watermark (matches feed._audit's `instance` field)."""
    return markers.last_giveup_at(
        pmo_id, instance=getattr(mgr, "instance_name", "") or "")


# ADR-0018 — classes whose failures NEVER burn a mission's attempts. Both latch
# a breaker, so the mission stops being dispatched at all and cannot livelock:
# that pairing is what makes "uncounted" safe — and since ADR-0027 it is an
# executable invariant over the taxonomy table (`counting == "never" ⇒
# breaker`), from which this set is derived.
#
# DEV_FORGE is deliberately NOT here. Making it unconditionally uncounted was
# the founder's call, but plain exit 13 latches no breaker (only the
# DEV_FORGE_AUTH arm calls forges.latch), so it would re-dispatch every poll
# interval forever on a bad branch name, a DNS failure or a 500 — the exact
# livelock `excusals_left` exists to bound. It gets the bounded treatment
# instead ("forge-bounded" in the table): uncounted while the step has
# excusals, counted once they are spent, so a forge outage costs no attempts
# but a permanent misconfiguration still reaches DEVCAKE-FAILED.
UNCOUNTED_CLASSES = failure_taxonomy.UNCOUNTED_CLASSES


def counts_toward_attempts(r) -> bool:
    """Does this failed run burn one of the step's attempts?

    Matches the STRUCTURED class, never a substring of `run.error`. The old
    substring test was injectable and the path is live: decomposition.py raises
    with the Dev's `blocked_by` list verbatim and finalize wraps that into
    `run.error`, so a Dev emitting `blocked_by: ["DEV_AUTH"]` made its own
    failures stop counting — and its mission never gave up.

    Pre-upgrade records (no class) fall back to a PREFIX match: `dev_failure_error`
    always puts the class first, while injected text can only ever land in the
    tail after `"DEV_BAD_OUTPUT: "`.
    """
    cls = getattr(r, "error_class", "") or ""
    if cls:
        return cls not in UNCOUNTED_CLASSES and getattr(r, "attempt_counted", True)
    return not any((r.error or "").startswith(c) for c in UNCOUNTED_CLASSES)


# ADR-0026 — the explicit human retry gesture recognized under `label-ops`.
# A literal token, not a label: pre-give-up there is no DEVCAKE-FAILED to
# remove, so strict mode needs a comment-shaped escape hatch that chatty
# integrations (sync bots, CI notifiers) never emit by accident.
RETRY_TOKEN = "DEVCAKE-RETRY"


def attempt_number(mgr, pmo_id: str, mission_type: str,
                    activity: Activity | None = None) -> int:
    """Count consecutive counted failures, independent of transcript seq.

    The count resets at the newest of: the last give-up event, ANY finished
    run for this mission (a later step finishing implies earlier failures
    were resolved), and — policy-dependent (ADR-0026, `attempt_reset`) — a
    human feed comment. Under `any-comment` every non-DevCake comment is an
    intervention (the pre-0026 rule; a chatty integration defeats
    max_attempts). Under `label-ops` and `unlimited` only a comment carrying
    the literal `DEVCAKE-RETRY` resets — the deliberate gesture."""
    all_runs = [r for r in mgr.runs.store.all()
                if r.mission_pmo_id == pmo_id and mgr._run_is_ours(r)]
    history = [r for r in all_runs if r.mission_type == mission_type]
    anchors = [t for t in [_last_giveup_at(mgr, pmo_id),
                           *(aware(r.created_at) for r in all_runs
                             if r.state == "finished")] if t]
    if activity is not None:
        policy = mgr.config.attempt_reset
        anchors += [aware(e.ts) for e in activity.entries
                    if e.kind == "comment"
                    and not is_devcake_comment(e.body)
                    and (policy == "any-comment"
                         # IRON RULE (markers.py, ADR-0014 D2): token scans
                         # run on the unquoted body — a human QUOTING a
                         # DevCake post that mentions the token is not the
                         # deliberate retry gesture (2026-08-12 audit F14)
                         or RETRY_TOKEN in unquoted(e.body or ""))]
    since = max(anchors, default=None)
    return 1 + sum(
        1 for r in history
        if r.state in ("failed", "timed_out", "orphaned")
        and counts_toward_attempts(r)
        and (since is None or aware(r.created_at) > since)
    )


# Process-local dedup for `unlimited` warnings: the SAME attempt number recurs
# across poll cycles whenever a later gate (mirror freshness, blockers) defers
# the dispatch, and a duplicate warning per cycle would be feed spam. A restart
# may repeat at most one warning — harmless in the loud direction.
_UNLIMITED_WARNED: set[tuple[str, str, int]] = set()


def mission_cost(mgr, pmo_id: str, *,
                 split_estimated: bool = False):
    """Cumulative effective cost of a mission's runs (ADR-0021 semantics —
    native harness cost when reported, else the finalize-stamped estimate).
    THE one cost-rollup (ADR-0034 PR-3 — review's loop warning carried a
    near-duplicate of this arithmetic): split_estimated=True additionally
    returns the estimated share, named rather than blended away."""
    from .. import costing
    ci = mgr.config.cost_inputs
    total = est = 0.0
    for r in mgr.runs.store.all():
        if r.mission_pmo_id != pmo_id or not mgr._run_is_ours(r):
            continue
        tr = r.token_report or {}
        native = tr.get("cost_usd_native")
        estimated = tr.get("cost_usd_estimated")
        eff = costing.effective_cost(native, estimated, ci)
        if eff is None:
            continue
        total += eff
        if estimated is not None and (ci.override_native or native is None):
            est += eff
    return (total, est) if split_estimated else total


async def _attempt_gate(mgr, mission: Mission, mtype: MissionType,
                        attempt: int) -> bool:
    """True when dispatch may proceed past the attempt budget (ADR-0026).

    `unlimited` always proceeds — warning loudly at cadence instead of ever
    applying DEVCAKE-FAILED; every other policy gives up past max_attempts."""
    if attempt <= mgr.config.max_attempts:
        return True
    if mgr.config.attempt_reset == "unlimited":
        await _unlimited_warn(mgr, mission, mtype, attempt)
        return True
    await _give_up(mgr, mission, mtype, attempt - 1)
    return False


async def _unlimited_warn(mgr, mission: Mission, mtype: MissionType,
                          attempt: int) -> None:
    """ADR-0026 `unlimited` guardrail: the mode deliberately builds the
    livelock `excusals_left`'s docstring warns about (no give-up, ever-growing
    run store), so it must be LOUD — a feed warning with cumulative cost every
    `review_loop_warning_every` consecutive failures."""
    failures = attempt - 1
    every = mgr.config.review_loop_warning_every
    if every < 1 or failures % every != 0:
        return
    key = (mission.pmo_id, mtype.value, failures)
    if key in _UNLIMITED_WARNED:
        return
    _UNLIMITED_WARNED.add(key)
    cost = mission_cost(mgr, mission.pmo_id)
    await mgr._feed(
        mission.pmo_id, mission.pmo_kind,
        f"⚠️ **Unlimited-attempts mode:** this mission's {mtype.value} step "
        f"has failed {failures} consecutive times, and `attempt_reset: "
        f"unlimited` means DevCake will keep retrying indefinitely. "
        f"Cumulative recorded cost so far: ${cost:.2f}. Add `DEVCAKE-SKIP` "
        f"to stop this mission, or change Policies → Attempts & retries "
        f"to restore give-up.")
    mgr._audit(mission.pmo_id, "unlimited_loop_warning",
               f"{mtype.value} x{failures}")
    log.warning("unlimited-attempts warning for %s (%s): %d failures",
                mission.key, mtype.value, failures)


def _last_counted_failure(mgr, pmo_id: str, mission_type: str):
    """Newest counted failed/timed_out/orphaned run for (pmo_id, step) under
    the same ownership rules as attempt_number — one source for give-up
    detail, not a second counter."""
    candidates = [
        r for r in mgr.runs.store.all()
        if r.mission_pmo_id == pmo_id
        and r.mission_type == mission_type
        and mgr._run_is_ours(r)
        and r.state in ("failed", "timed_out", "orphaned")
        and counts_toward_attempts(r)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: aware(r.created_at))


def _give_up_feed_body(mission: Mission, mtype: MissionType, attempts: int,
                       last) -> str:
    """docs/15 §3: last error class + message, attempt count, concrete
    final-attempt trace pointer (run_id; + OO UI URL / trace_id when set)."""
    from ...security import redact

    cls = (getattr(last, "error_class", None) or "") if last else ""
    raw = (getattr(last, "error", None) or "") if last else ""
    if cls and raw.startswith(f"{cls}:"):
        msg = raw[len(cls) + 1:].strip()
    else:
        msg = raw.strip()
    msg = redact(msg)[:500] if msg else ""

    detail = ""
    if cls and msg:
        detail = f" Last error: `{cls}` — {msg}."
    elif cls:
        detail = f" Last error: `{cls}`."
    elif msg:
        detail = f" Last error: {msg}."

    run_id = (last.run_id or "") if last is not None else ""
    tp = (last.traceparent or "") if last is not None else ""
    # same extraction as domain/runs.failure_record
    trace_id = tp.split("-")[1] if tp and "-" in tp else ""
    oo = (os.environ.get("OO_UI_URL") or "").rstrip("/")

    if run_id and oo and trace_id:
        pointer = (f"Final attempt: run `{run_id}`, trace `{trace_id}` "
                   f"(OpenObserve: {oo}).")
    elif run_id and trace_id:
        pointer = f"Final attempt: run `{run_id}`, trace `{trace_id}`."
    elif run_id:
        pointer = f"Final attempt: run `{run_id}`."
    else:
        pointer = (f"Traces: search run ids `{mission.key}-*` "
                   f"in OpenObserve.")

    return (
        f"⚠️ **DevCake gave up on this mission's {mtype.value} step** after "
        f"{attempts} failed attempts.{detail} Remove the `DEVCAKE-FAILED` "
        f"label to retry. ({pointer})")


async def _give_up(mgr, mission: Mission, mtype: MissionType, attempts: int) -> None:
    if LABEL_FAILED in mission.labels:
        return
    last = _last_counted_failure(mgr, mission.pmo_id, mtype.value)
    with tracer.start_as_current_span("mission.give_up") as span:
        span.set_attribute("devcake.mission.key", mission.key)
        span.set_attribute("devcake.mission.type", mtype.value)
        span.set_attribute("devcake.run.attempt", attempts)
        if last is not None:
            if last.error_class:
                span.set_attribute("devcake.error.class", last.error_class)
            span.set_attribute("devcake.run.id", last.run_id)
        span.set_status(Status(StatusCode.ERROR,
                               f"gave up after {attempts} attempts"))
        await mgr.pmo.swap_labels(mission.ref, remove=set(), add={LABEL_FAILED})
        await mgr._feed(
            mission.pmo_id, mission.pmo_kind,
            _give_up_feed_body(mission, mtype, attempts, last))
        mgr._audit(mission.pmo_id, "devcake_failed", mtype.value)
    log.warning("DEVCAKE-FAILED applied to %s (%s)", mission.key, mtype.value)



async def resolve_repo_live(mgr, mission, all_runs=None):
    """(repo_name | None, gate_reason | None), UN-GATING zero-repo missions
    onto the internal fallback forge (M11). Async — it may provision a repo.

    Order: re-register any internal repo this mission already used (so the
    sticky resolver finds it after an app restart), run the sticky resolver,
    and if that returns the specific zero-repo gate, provision an internal
    repo. Any OTHER gate (unknown marker, sticky-vanished external, mid-
    mission change) is a real gate — never silently redirected internal."""
    from ..repo_routing import REASON_ZERO_REPO
    from ...ports.internal_forge import internal_repo_name

    if all_runs is None:
        all_runs = mgr.runs.store.all()

    async def _provision() -> str:
        # ensure service accounts first (lazy retry — boot provisioning may
        # have failed against a not-yet-ready Gitea; review finding #7)
        if not mgr.internal_forge.service_tokens():
            await mgr.internal_forge.ensure_service_accounts()
        creds = await mgr.internal_forge.ensure_mission_repo(
            mgr.instance_name, mission.key)
        # the row + app-side adapter come from the PORT (2026-08 F9): only
        # the internal forge knows its own vendor — token choice, auto-merge
        # doctrine and the synthesized-name RepoInstance live in the adapter
        inst, adapter = mgr.internal_forge.mission_repo_binding(creds)
        mgr.forges.register_internal(creds.repo_name, inst, adapter)
        return creds.repo_name

    async def _ensure_registered(name: str) -> None:
        # already registered this process → no per-cycle I/O (finding #8);
        # else (re)provision — covers restart recovery + first intake
        if name not in mgr.forges.instances:
            await _provision()

    expected = (internal_repo_name(mgr.instance_name, mission.key)
                if mgr.internal_forge is not None else None)

    # a done/canceled mission must never (re-)provision: the poll loop sees
    # terminal missions too, so without this guard the admin Clear endpoint
    # was silently undone within one cycle — repo, svc user, and a fresh
    # token pair resurrected (audit A4). Terminal missions are never
    # scheduled (derivation row 5), so gating them is inert.
    terminal = mission.status in ("done", "canceled")

    # restart recovery: a prior run points at this mission's internal repo,
    # but ForgeRuntime lost it on restart — re-register before resolving
    if not terminal and expected is not None and any(
            r.repo_ref == expected for r in all_runs
            if r.mission_pmo_id == mission.pmo_id):
        await _ensure_registered(expected)

    name, reason = resolve_repo(mgr, mission, all_runs=all_runs)
    if name is not None:
        return name, reason
    if (reason is REASON_ZERO_REPO and mgr.internal_forge is not None
            and not terminal):
        await _ensure_registered(expected)
        return expected, None
    return None, reason
