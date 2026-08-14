"""STEWARD mission dispatch/finalize and edge application (ADR-0007)."""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind

from ...harness import HARNESSES
from ...security import redact_value
from ...config import DevType
from .. import costing
# LABEL_DISCOVERY read is a documented ruling (ADR-0033 addendum): the
# routing lane REMOVES the sweep gate once a source's batches are all
# receipted — allowlisted in test_structure_guards.
from ..model import LABEL_DISCOVERY, LABEL_OPTIN, Mission, MissionRef
from ..workspaces import WorkspaceUnavailable
from . import dispatch
from ..run import Run, utcnow
from .feed import unquoted
from .markers import (DISCOVERY_IN_EXCERPT_MAX, FEED_INLINE_MAX, defang,
                      discovery_in_keys)

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


async def _launch_steward(mgr, dev_type: DevType, *, duty: str,
                          prompt_text: str,
                          blocker_work: list[dict] | None = None,
                          batches: list[dict] | None = None,
                          context_stale=frozenset(),
                          context_omit=frozenset()) -> Run | None:
    """The ONE steward launch body, shared by both flavors (chokepoint
    ruling): repo/forge resolution, seq, span, spec env, skills, launch.
    `duty` stamps Run.steward_duty ("" = relations); `blocker_work` carries
    the discovery flavor's family repos (RO clones via the runspec extras).
    Returns None when the workspace base is unusable (AUD-001: a periodic
    steward must skip cleanly, never poison the poll segment)."""
    from ..ids import make_run_id
    from ..repo_sourcing import skill_source_cards
    repo_name = dispatch.steward_repo(mgr)
    if repo_name is None:
        # spec env carries the forge dialect — no repo, no steward runs either
        raise RuntimeError("no repository configured — steward runs need the "
                           "forge dialect in their run spec")
    repo, forge = mgr.forges.instance(repo_name), mgr.forges.get(repo_name)
    # own-instance STEWARD runs only (audit A29, cosmetic): run ids carry the
    # instance prefix + random suffix, so no collision — but a cross-instance
    # count made the human-visible seq misleading
    seq = 1 + sum(1 for r in mgr.runs.store.all()
                  if r.mission_type == "STEWARD" and mgr._run_is_ours(r))
    run_id = make_run_id(mgr.instance_name, "TEAM", seq, "STEWARD")

    with tracer.start_as_current_span("mission.dispatch", kind=SpanKind.PRODUCER) as span:
        span.set_attribute("devcake.run.id", run_id)
        span.set_attribute("devcake.mission.key", "TEAM")
        span.set_attribute("devcake.mission.type", "STEWARD")
        span.set_attribute("devcake.dev_type", dev_type.name)
        span.set_attribute("devcake.steward.duty", duty or "relations")
        carrier: dict[str, str] = {}
        inject(carrier)
        traceparent = carrier.get("traceparent", "")

        spec_env = dispatch._protocol_spec_env(
            mgr,
            recover_misplaced_result=mgr.config.recover_misplaced_result,
            continuation_policy=mgr.config.continuation_policy,
            max_continuations=mgr.config.max_continuations,
            mirror_path=(str(mgr.repo_cache.mirror_path(repo_name))
                         if mgr.repo_cache.eligible(repo_name) else ""),
            lfs=mgr.config.repo_mirror.lfs,
            mission_id="", mission_key="TEAM", mission_type="STEWARD",
            dev_type=dev_type, seq=seq, extra_args="",
            repo=repo, forge=forge)
        run = Run(
            run_id=run_id, mission_key="TEAM", mission_type="STEWARD",
            pmo_ref=mgr.instance_name, repo_ref=repo_name,
            dev_type=dev_type.name, seq=seq,
            timeout_seconds=mgr.config.dev_timeout_minutes * 60,
            traceparent=traceparent,
            spec_env=spec_env,
            steward_duty=duty,
            steward_batches=batches or [],
            blocker_work=blocker_work or [],
            # F12 snapshot rule: which extras serve via mirror is decided at
            # dispatch, when the gate proved them fresh (StewardService
            # widened ensure_fresh to the family set before calling us)
            mirror_repos=sorted((set(mgr.repo_cache.needed_for(
                work_repo=repo_name, mission_type="STEWARD",
                instance=mgr.instance,
                blocker_entries=blocker_work or [],
                dev_type=dev_type, config=mgr.config))
                | skill_source_cards(dev_type.skills))
                - set(context_omit)),
        )
        run.memory_mounts = await dispatch._memory_mount_snapshot(
            mgr, dev_type, repo_name, stale=set(context_stale),
            omit=set(context_omit))
        run.spec_skills = await dispatch._skill_payload(mgr, dev_type)
        run.spec_skills_dir = HARNESSES[dev_type.harness_template].skills_dir or ""
        run.skill_repo_heads = await dispatch._skill_repo_heads(mgr, dev_type)
        run.spec_prompt = dispatch.append_memory_sentence(
            dispatch.append_required_skills(
                prompt_text, dev_type.skills_required, run.spec_skills),
            run.memory_mounts)
        try:
            await mgr.runs.bootstrap.launch(
                run, image=HARNESSES[dev_type.harness_template].image)
        except WorkspaceUnavailable as e:
            # AUD-001: STEWARD is periodic and shares the poll segment — a bad
            # workspace base must skip this run cleanly, never raise into the
            # poll loop and mark the whole instance poll_degraded.
            log.warning("steward dispatch for %s skipped — workspace base "
                        "unusable: %s", mgr.instance_name, e)
            return None
        log.info("dispatched steward %s (duty=%s, dev=%s)",
                 run_id, duty or "relations", dev_type.name)
        return run


async def dispatch_steward(mgr, dev_type: DevType, missions: list[Mission],
                           context_stale=frozenset(),
                           context_omit=frozenset()) -> Run:
    """Dispatch the RELATIONS steward: a Dev whose only job is proposing
    missing blocked-by edges. No PMO writes at dispatch (no status, no
    labels) — finalize_steward validates and applies whatever it proposes."""
    from ...prompts import STEWARD_MISSION_CAP, steward_prompt
    eligible = [m for m in missions
                if m.pmo_kind == "issue" and m.status not in ("done", "canceled")
                and (mgr.config.adoption_mode != "opt_in"
                     or LABEL_OPTIN in m.labels)]
    if len(eligible) > STEWARD_MISSION_CAP:
        log.warning("steward prompt truncated to %d of %d missions",
                    STEWARD_MISSION_CAP, len(eligible))
    return await _launch_steward(
        mgr, dev_type, duty="",
        prompt_text=steward_prompt(
            dispatch._identifying_prompt(mgr, dev_type), eligible),
        context_stale=context_stale, context_omit=context_omit)


async def dispatch_steward_discovery(mgr, dev_type: DevType, family,
                                     pending: dict,
                                     context_stale=frozenset(),
                                     context_omit=frozenset()) -> Run | None:
    """Dispatch the DISCOVERY steward (ADR-0033): curated family package,
    the family's work repos as read-only extras, propose-only routes.
    `pending` maps source pmo_id → [(step, n)] un-routed batches; batches
    whose source run record is gone are left for the sweep's unroutable
    terminal (D7) — if nothing remains, no run dispatches."""
    from ...prompts import steward_discovery_prompt
    from .family_graph import family_work_repos
    primary = dispatch.steward_repo(mgr)
    if primary is None:
        raise RuntimeError("no repository configured — steward runs need the "
                           "forge dialect in their run spec")
    runs = mgr.runs.store.all()
    package, included = build_discovery_package(mgr, family, pending, runs)
    if not included:
        return None
    by_id = family.by_id
    return await _launch_steward(
        mgr, dev_type, duty="discovery",
        prompt_text=steward_discovery_prompt(
            dispatch._identifying_prompt(mgr, dev_type), package),
        blocker_work=family_work_repos(mgr, family, runs,
                                       exclude=frozenset({primary})),
        batches=[{"pmo_id": pid, "key": by_id[pid].key, "step": step}
                 for pid, step in included],
        context_stale=context_stale, context_omit=context_omit)


def build_discovery_package(mgr, family, pending: dict,
                            runs: list) -> tuple[str, list[tuple[str, int]]]:
    """The curated context package (ADR-0033 D4 — curated, not accumulated):
    the family map with statuses; finished/canceled members contribute a
    description head + handoff excerpt; open members description heads; the
    NEW discovery entries at full fidelity with their source's description.
    Returns (sections text, included (pmo_id, step) batches). Entries are
    re-derived from the source run's stored result — the same normalization
    harvest used, truncated to the marker's n — so the package transports
    exactly what the board memorialized."""
    from ...prompts import STEWARD_DESC_HEAD_CHARS, STEWARD_MISSION_CAP
    from .discovery import valid_entries
    from .markers import handoff_of

    def head(m) -> str:
        return (" ".join((m.description or "").split())
                [:STEWARD_DESC_HEAD_CHARS] or "(no description)")

    by_id = family.by_id
    rows = []
    for m in family.members[:STEWARD_MISSION_CAP]:
        blockers = ", ".join(by_id[b].key for b in (m.blocked_by or [])
                             if b in by_id) or "(none)"
        rows.append(f"- **{m.key}** · {m.status} · blocked by: {blockers}\n"
                    f"  {m.title} — {head(m)}")
        if m.status in ("done", "canceled"):
            note = handoff_of(m.description)
            if note:
                from .markers import HANDOFF_EXCERPT_MAX
                rows.append(f"  Handoff: {note[:HANDOFF_EXCERPT_MAX]}")

    finds: list[str] = []
    included: list[tuple[str, int]] = []
    run_ix = {(r.mission_pmo_id, r.seq): r for r in runs
              if r.mission_type in ("ONBOARD", "EXECUTE", "REVIEW")
              and mgr._run_is_ours(r) and r.result}
    for pmo_id, batches in sorted(pending.items()):
        src = by_id.get(pmo_id)
        if src is None:
            continue
        for step, n in batches:
            r = run_ix.get((pmo_id, step))
            entries = valid_entries(r.result)[:n] if r is not None else []
            if not entries:
                continue        # run record gone — the sweep terminates it
            included.append((pmo_id, step))
            finds.append(f"From **{src.key}** step {step} — mission: "
                         f"{head(src)}")
            for i, e in enumerate(entries, 1):
                finds.append(f"{i}. Finding: {e['finding']}\n"
                             f"   Evidence: {e['evidence']}\n"
                             f"   Scope: {e['scope']}")
    package = ("### The family (key · status · blocked by · title)\n"
               + "\n".join(rows)
               + "\n\n### New discoveries to route\n"
               + ("\n".join(finds) if finds else "(none)"))
    return package, included


async def finalize_steward(mgr, run: Run, payload: dict) -> None:
    """STEWARD runs have no host mission: no transcript/token-report comments —
    the output lands as relations + a notification comment on each blocked
    mission. Failures are logged only; the next interval simply retries."""
    result = payload.get("result") or {}
    outcome = result.get("outcome", "")
    # same ADR-0021 stamp as mission finalize — steward spend is fleet spend
    run.token_report = redact_value(costing.stamp_estimate(
        payload.get("token_report") or {}, mgr.config.cost_inputs))
    try:                                               # ADR-0022, as finalize()
        run.continuations_used = int(payload.get("continuations_used") or 0)
    except (TypeError, ValueError):
        run.continuations_used = 0

    ctx = None
    if run.traceparent:
        from opentelemetry.propagate import extract
        ctx = extract({"traceparent": run.traceparent})
    with tracer.start_as_current_span("run.finalize", context=ctx,
                                      kind=SpanKind.CONSUMER) as span:
        span.set_attribute("devcake.run.id", run.run_id)
        span.set_attribute("devcake.outcome", outcome or "(failure artifact)")
        await mgr.messaging.delete_run_user(run.run_id)
        await mgr.messaging.delete_reply_stream(run.run_id)
        if outcome != "stewarded":
            run.state = "failed"
            run.error = mgr.dev_failure_error(run, payload)
            run.ended_at = utcnow()
            mgr.runs.store.save(run)
            log.warning("steward run %s failed: %s", run.run_id, run.error)
            return
        if run.steward_duty == "discovery":
            delivered, rejected = await apply_discovery_routes(
                mgr, run, result.get("routes") or [])
            span.set_attribute("devcake.steward.routes_delivered", delivered)
            span.set_attribute("devcake.steward.routes_rejected", rejected)
        else:
            created, rejected = await apply_steward_edges(
                mgr, result.get("edges") or [])
            span.set_attribute("devcake.steward.edges_created", created)
            span.set_attribute("devcake.steward.edges_rejected", rejected)
        run.result = redact_value(result)
        run.state, run.ended_at = "finished", utcnow()
        mgr.runs.store.save(run)
        log.info("steward %s (%s) finished", run.run_id,
                 run.steward_duty or "relations")


async def apply_steward_edges(mgr, edges: list) -> tuple[int, int]:
    """The Dev is advisory; the app is the gatekeeper — drop edges that are
    unknown, self-referential, terminal, duplicate, or cycle-forming (ADR-0007)."""
    missions = await mgr.pmo.list_all(mgr.instance.team_key)
    by_key = {m.key.upper(): m for m in missions if m.pmo_kind == "issue"}
    graph = {m.pmo_id: set(m.blocked_by) for m in missions
             if m.pmo_kind == "issue"}                 # node → its blockers
    created = rejected = 0
    for e in edges:
        blocker_key = str((e or {}).get("blocker") or "").strip().upper()
        blocked_key = str((e or {}).get("blocked") or "").strip().upper()
        blocker, blocked = by_key.get(blocker_key), by_key.get(blocked_key)
        reason = None
        if blocker is None or blocked is None:
            reason = "unknown mission key"
        elif blocker.pmo_id == blocked.pmo_id:
            reason = "self-edge"
        elif blocker.status in ("done", "canceled") \
                or blocked.status in ("done", "canceled"):
            reason = "terminal mission"
        elif blocker.pmo_id in graph.get(blocked.pmo_id, set()):
            reason = "duplicate"
        elif _creates_cycle(graph, blocker.pmo_id, blocked.pmo_id):
            reason = "would create a cycle"
        if reason:
            rejected += 1
            mgr._audit(blocked.pmo_id if blocked else "", "steward_edge_rejected",
                        f"{blocker_key}→{blocked_key}: {reason}")
            log.info("steward edge %s blocks %s rejected: %s",
                     blocker_key, blocked_key, reason)
            continue
        await mgr.pmo.create_relation(blocker.pmo_id, blocked.pmo_id)
        graph.setdefault(blocked.pmo_id, set()).add(blocker.pmo_id)
        mgr._audit(blocked.pmo_id, "relation_created",
                    f"steward: blocked by {blocker.key}")
        await mgr._feed(
            blocked.pmo_id, "issue",
            f"🔗 DevCake mapped a blocking relation: this mission is blocked by "
            f"**{blocker.key}** and will not start before it finishes. Remove "
            f"the relation in the PMO if this is wrong.")
        created += 1
    return created, rejected


async def apply_discovery_routes(mgr, run: Run, routes: list) -> tuple[int, int]:
    """The Dev is advisory; the app is the gatekeeper (the
    apply_steward_edges precedent, ADR-0033 D5). Validate every route
    against the LIVE board and feed-counted budgets, copy finding text from
    the source run record — verbatim transport is structural, steward text
    never lands beyond its one-line "because" — deliver one marked comment
    per recipient, receipt a batch only when it was *dispositioned*
    (delivered, or deliberately routed nowhere / a terminal reject), and
    drop the sweep-gate label once a source has nothing pending.
    Transient holds (unreadable feed, failed post, routing toggle off)
    write no ``to=-`` — the sweep re-drives. A recipient past the
    full-read page ceiling is NOT transient (feeds only grow): terminal
    ``to=-`` with a human-directed reason on the receipt comment
    (addendum 14). No numeric route budgets — the (source, step) dedup
    and family size are the structural bounds; the steward is the
    judgment layer. Idempotent against the board: a redelivered finalize
    re-checks the recipient's delivery markers and the source's receipts
    before every write."""
    from .discovery import render_entry_lines, scan_source, valid_entries
    from .family_graph import family_of

    if not mgr.instance.discovery_routing:          # D11 — delivery too
        return 0, 0

    missions = await mgr.pmo.list_all(mgr.instance.team_key)
    by_key = {m.key.upper(): m for m in missions if m.pmo_kind == "issue"}
    by_id = {m.pmo_id: m for m in missions if m.pmo_kind == "issue"}
    runs = mgr.runs.store.all()
    run_ix = {(r.mission_pmo_id, r.seq): r for r in runs
              if r.mission_type in ("ONBOARD", "EXECUTE", "REVIEW")
              and mgr._run_is_ours(r) and r.result}
    batches = {(b.get("pmo_id"), int(b.get("step") or 0)): b
               for b in (run.steward_batches or [])}

    delivered = rejected = 0
    # (source pmo_id, step) held for a transient — no to=- (an outage is
    # a pause; the sweep re-drives once the board answers again)
    held: set[tuple[str, int]] = set()
    # (source pmo_id, step) → human-directed lines that ride the receipt
    # comment (posted once — the receipt pre-scan is the dedup)
    reasons: dict[tuple[str, int], list[str]] = {}

    def reject(pmo_id: str, detail: str, *,
               hold: tuple[str, int] | None = None,
               reason: tuple[tuple[str, int], str] | None = None) -> None:
        nonlocal rejected
        rejected += 1
        if hold is not None:
            held.add(hold)
        if reason is not None:
            reasons.setdefault(reason[0], []).append(reason[1])
        mgr._audit(pmo_id or "", "discovery_route_rejected", detail)
        log.info("discovery route rejected: %s", detail)

    src_state: dict[str, object] = {}     # source pmo_id → SourceState
    families: dict[str, object] = {}      # source pmo_id → Family

    # 1 — validate + resolve, grouped per recipient
    accepted: dict[str, list[dict]] = {}
    for raw in routes:
        e = raw if isinstance(raw, dict) else {}
        src_key = str(e.get("source") or "").strip().upper()
        tgt_key = str(e.get("target") or "").strip().upper()
        try:
            step, index = int(e.get("step")), int(e.get("finding"))
        except (TypeError, ValueError):
            reject("", f"{src_key}→{tgt_key}: malformed route")
            continue
        because = " ".join(str(e.get("because") or "").split())[:300]
        src, tgt = by_key.get(src_key), by_key.get(tgt_key)
        if src is None or (src.pmo_id, step) not in batches:
            reject(src.pmo_id if src else "",
                   f"{src_key} step {step}: not in this run's package")
            continue
        if tgt is None:
            reject(src.pmo_id, f"{src_key}→{tgt_key}: unknown target")
            continue
        if tgt.pmo_id == src.pmo_id:
            reject(src.pmo_id, f"{src_key}→{tgt_key}: self-route")
            continue
        if tgt.status in ("done", "canceled"):
            reject(src.pmo_id, f"{src_key}→{tgt_key}: terminal recipient")
            continue
        fam = families.get(src.pmo_id)
        if fam is None:
            fam = families[src.pmo_id] = family_of(src, missions)
        if tgt.pmo_id not in fam.by_id:
            reject(src.pmo_id, f"{src_key}→{tgt_key}: outside the family")
            continue
        state = src_state.get(src.pmo_id)
        if state is None:
            state = src_state[src.pmo_id] = await scan_source(mgr, src)
        if getattr(state, "truncated", False):
            # hold, don't receipt: receipts on an unreadable-in-full feed
            # can never be re-read — the sweep raises the ceiling case to
            # the humans and retires the gate (addendum 14)
            reject(src.pmo_id, f"{src_key}: source feed truncated",
                   hold=(src.pmo_id, step))
            continue
        n = next((n_ for s_, n_ in state.posted if s_ == step), 0)
        source_run = run_ix.get((src.pmo_id, step))
        entries = valid_entries(source_run.result)[:n] if source_run else []
        if not 1 <= index <= len(entries):
            reject(src.pmo_id, f"{src_key} step {step}: no finding #{index}")
            continue
        accepted.setdefault(tgt.pmo_id, []).append(
            {"src": src, "tgt": tgt, "step": step, "index": index,
             "entry": entries[index - 1], "because": because})

    # 2 — deliver, one comment per recipient; dedup + recipient cap read
    # from the recipient's LIVE feed just before posting (D6 idempotency)
    receipts: dict[str, set[tuple[int, str]]] = {}
    for tgt_id, batch in accepted.items():
        tgt = batch[0]["tgt"]
        try:
            act = await mgr.pmo.get_activity(MissionRef(tgt_id, "issue"),
                                             full=True)
        except Exception as ex:  # noqa: BLE001 — recipient skipped; the board re-drives via the sweep
            for x in batch:
                reject(x["src"].pmo_id,
                       f"→{tgt.key}: recipient feed unreadable ({ex})",
                       hold=(x["src"].pmo_id, x["step"]))
            continue
        if act.truncated:
            # past the full-read page ceiling: the dedup markers cannot all
            # be seen and feeds only grow, so this never heals — terminal,
            # raised to the humans on the receipt comment (addendum 14)
            for x in batch:
                reject(x["src"].pmo_id,
                       f"→{tgt.key}: recipient feed past the read ceiling",
                       reason=((x["src"].pmo_id, x["step"]),
                               f"⚠️ step {x['step']} → {tgt.key} declined: "
                               f"its feed exceeds the readable page ceiling, "
                               f"so duplicate-delivery checks are impossible "
                               f"— routing to it is permanently off. Carry "
                               f"`DISCOVERY_{x['step']}.md` over manually if "
                               f"it matters."))
            continue
        existing: set[tuple[str, int]] = set()
        for en in act.entries:
            existing |= discovery_in_keys(unquoted(en.body))
        by_pair: dict[tuple[str, int], list[dict]] = {}
        for x in batch:
            by_pair.setdefault((x["src"].key.upper(), x["step"]),
                               []).append(x)
        sections: list[str] = []
        landed: list[dict] = []
        for (skey, step), xs in sorted(by_pair.items()):
            src = xs[0]["src"]
            if (skey, step) in existing:
                # already on the recipient — idempotent re-run; the receipt
                # still records the delivery (self-heal)
                receipts.setdefault(src.pmo_id, set()).add(
                    (step, tgt.key.upper()))
                mgr._audit(src.pmo_id, "discovery_route_duplicate",
                           f"{skey} step {step}→{tgt.key}")
                continue
            head = [f"`devcake:discovery-in:v1 src={skey} step={step}`",
                    f"🔎 [{skey} · step {step} · {utcnow():%Y-%m-%d}] — "
                    f"leads, not truths: verify against the source before "
                    f"relying. Full record: `DISCOVERY_{step}.md` on "
                    f"{skey}."]
            body_lines: list[str] = []
            for x in xs:
                body_lines += render_entry_lines(
                    [x["entry"]], cap=DISCOVERY_IN_EXCERPT_MAX)
                if x["because"]:
                    body_lines.append(f"*— steward: {defang(x['because'])}*")
            sections.append("\n\n".join(head + body_lines))
            landed.extend(xs)
            receipts.setdefault(src.pmo_id, set()).add(
                (step, tgt.key.upper()))
        if not sections:
            continue
        body = "\n\n---\n\n".join(sections)
        if len(body) > FEED_INLINE_MAX:
            # marker + provenance must stay inline — drop finding bodies,
            # keep the pointer (never externalize a counted marker)
            body = "\n\n---\n\n".join(
                s.split("\n\n**", 1)[0] for s in sections)
        try:
            await mgr._feed(tgt_id, "issue", body, externalize=False)
            delivered += len(landed)
        except Exception as ex:  # noqa: BLE001 — nothing landed; hold the
            # batch so phase 3 cannot stamp to=- (the sweep re-drives)
            for x in landed:
                receipts.get(x["src"].pmo_id, set()).discard(
                    (x["step"], tgt.key.upper()))
                reject(x["src"].pmo_id,
                       f"→{tgt.key}: delivery post failed ({ex})",
                       hold=(x["src"].pmo_id, x["step"]))

    # 3 — receipts: a batch is dispositioned when something landed, the
    # steward routed nowhere, or every proposal was a *terminal* reject
    # (incl. a ceiling-truncated recipient, whose human-directed reason
    # rides this comment). Transient holds (unreadable / post-fail) stay
    # pending — to=- is deliberate/clear-runs only (ADR-0033 addendum).
    for (pid, step), b in batches.items():
        if (pid, step) in held:
            continue
        receipts.setdefault(pid, set())
        if not any(s == step for s, _t in receipts[pid]):
            receipts[pid].add((step, "-"))
    for pid, pairs in receipts.items():
        state = src_state.get(pid)
        if state is None and pid in by_id:
            # routed-nowhere sources were never scanned during validation —
            # scan now so a redelivered finalize never re-posts receipts
            state = await scan_source(mgr, by_id[pid])
        pre = getattr(state, "receipted", set())
        new = sorted(p for p in pairs if p not in pre)
        if new:
            lines = [f"`devcake:discovery-routed:v1 step={s} to={t}`"
                     for s, t in new]
            notes = [note for s in sorted({s for s, _t in new})
                     for note in reasons.get((pid, s), [])]
            try:
                await mgr._feed(
                    pid, "issue",
                    "🧭 Discovery routing receipts (dispositioned batches; "
                    "`to=-` = routed nowhere):\n" + "\n".join(lines)
                    + ("\n\n" + "\n".join(notes) if notes else ""),
                    externalize=False)
            except Exception as ex:  # noqa: BLE001 — receipts missing ⇒ the sweep re-detects; never fail the finalize
                mgr._audit(pid, "discovery_receipt_failed", str(ex)[:200])
                continue
        src = by_id.get(pid)
        if src is None:
            continue
        try:
            state2 = await scan_source(mgr, src)
            if not state2.pending:
                await mgr.pmo.swap_labels(MissionRef(pid, "issue"),
                                          remove={LABEL_DISCOVERY},
                                          add=set())
                mgr._discoveries_pending.discard(pid)
        except Exception as ex:  # noqa: BLE001 — label is a hint; the sweep self-heals it later
            mgr._audit(pid, "discovery_label_failed", str(ex)[:200])
    return delivered, rejected


def _creates_cycle(graph: dict[str, set[str]], blocker: str, blocked: str) -> bool:
    """graph maps node → its blockers. Adding `blocked ← blocker` cycles iff
    `blocked` already (transitively) blocks `blocker`."""
    stack, seen = [blocker], set()
    while stack:
        n = stack.pop()
        if n == blocked:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(graph.get(n, ()))
    return False

