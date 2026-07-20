"""Mission dispatch, attempt counting, credentials, activity payload (docs/04 §3.1)."""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry import trace
from opentelemetry.propagate import inject
from opentelemetry.trace import SpanKind, Status, StatusCode

from ...harness import HARNESSES, missing_referenced_secret_env
from ...ports.forge import mission_branch
from ...telemetry import OTEL_COLLECTOR_URL
from ...config import DevType
from ..model import (Activity, LABEL_FAILED, Mission, MissionRef, MissionType,
                     STAGE_LABELS, derive)
from ..run import Run, utcnow
from . import markers
from . import schedule
from .feed import _is_devcake_comment, _stage_of, _unquoted
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
         and r.mission_type != "MAPPER"),
        key=lambda r: r.created_at, reverse=True)
    return resolve_repo(mission, mgr.instance,
                        set(mgr.forges.instances), history)


def mapper_repo(mgr) -> str | None:
    """The repo a MAPPER run clones (the entrypoint always clones): the
    instance's default repo when configured, else any configured repo. A
    MAPPER run reads the whole team's relations, not a mission — it never
    routes to a per-mission internal repo (returns None → mapper stays idle
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
    # M11: zero-repo missions un-gate onto the internal forge)
    repo_name, gate_reason = await resolve_repo_live(mgr, live)
    if repo_name is None:
        mgr.blocked_reasons[live.pmo_id] = gate_reason
        log.info("dispatch of %s refused — %s", live.key, gate_reason)
        return None
    repo = mgr.forges.instance(repo_name)
    forge = mgr.forges.get(repo_name)
    if live.blocked_by:
        open_blockers = await schedule._open_blockers(mgr, live, {}, {})  # all live
        if open_blockers:
            log.info("dispatch of %s aborted — blocked by %s",
                     live.key, ", ".join(open_blockers))
            return None

    if mission.pmo_kind == "project":
        seq = 1                       # projects only ever ONBOARD (ADR-0006)
        activity = None
    else:
        activity = await mgr.pmo.get_activity(mission.ref)
        seq = _derive_seq(activity)
    # attempts restart when a human removes DEVCAKE-FAILED (docs/15 §3),
    # a later step finishes, or a human comments on the mission
    attempt = attempt_number(mgr, mission.pmo_id, mtype.value, activity)
    if attempt > mgr.config.max_attempts:
        await _give_up(mgr, live, mtype, attempt - 1)
        return None

    assignment = mgr.config.assignments[mtype.value]
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

    # ADR-0014 D4: refresh the mission's activity repo BEFORE the step — the
    # repo records what this Dev actually receives. NEVER gates dispatch.
    await _push_activity_repo(mgr, live, mtype, seq)

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
            # the ACTIVE template for this mission type (v0.1.1); a missing/
            # corrupt file falls back to the built-in default — dispatch
            # never fails on template trouble (warning also in /health)
            text, warn = prompt_templates.resolve_playbook(
                mt_name, mgr.config.active_prompt_templates.get(mt_name))
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
                decomposition_rule=decomposition_rule(mgr, live)),
            MissionType.PLAN: lambda: plan_prompt(
                ident, live, playbook=_pb("PLAN"),
                reference_repos=ref_note),
            MissionType.EXECUTE: lambda: execute_prompt(
                ident, live, repo_slug,
                pr_instructions=forge.descriptor.pr_instructions,
                default_branch=repo.default_branch,
                playbook=_pb("EXECUTE"),
                reference_repos=ref_note),
            MissionType.REVIEW: lambda: review_prompt(
                ident, live, playbook=_pb("REVIEW"),
                reference_repos=ref_note),
        }[mtype]()

        spec_env = _protocol_spec_env(mgr, 
            mission_id=mission.pmo_id, mission_key=mission.key,
            mission_type=mtype.value, dev_type=dev_type, seq=seq,
            extra_args=assignment.extra_cli_args, repo=repo, forge=forge)
        run = Run(
            run_id=run_id, mission_key=mission.key, mission_type=mtype.value,
            pmo_kind=mission.pmo_kind,
            pmo_ref=mgr.instance_name, repo_ref=repo_name,
            dev_type=dev_type.name, seq=seq, attempt_of_step=attempt,
            timeout_seconds=mgr.config.dev_timeout_minutes * 60,
            traceparent=traceparent,
            spec_env=spec_env,
        )
        run.spec_skills = await _skill_payload(mgr, dev_type)
        run.spec_skills_dir = HARNESSES[dev_type.harness_template].skills_dir or ""
        run.spec_prompt = append_required_skills(
            prompt, dev_type.skills_required, run.spec_skills)
        run.branch = mission_branch(mgr.instance_name, mission.key)
        run.stage_label_at_dispatch = _stage_of(live)
        run.mission_pmo_id = mission.pmo_id
        await mgr.runs.bootstrap.launch(
            run, image=HARNESSES[dev_type.harness_template].image)

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
    lines = "\n".join(f"- `{n}`" for n in names)
    return (
        f"{prompt}\n\n### Required skills\n"
        "You must consult the following skill(s) before acting on this mission "
        "(they are installed in your harness skills directory; open each "
        "skill's SKILL.md and apply what is relevant — do not skip them):\n"
        f"{lines}"
    )


def _protocol_spec_env(mgr, *, mission_id: str, mission_key: str,
                       mission_type: str, dev_type: DevType, seq: int,
                       extra_args: str, repo, forge) -> dict[str, str]:
    """The Dev-protocol env contract (docs/07 §3), built in exactly one
    place so mission and mapper dispatches can never drift apart — a var
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
        "DEVCAKE_MODEL": (dev_type.model
                          or HARNESSES[dev_type.harness_template].default_model),
        # Devs export through the collector, credential-free (ISSUES #13)
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"{OTEL_COLLECTOR_URL}/v1/traces",
    }


def runspec_secret_payload(mgr, run: Run) -> dict | None:
    """Secret half of a run spec, built from current config on request
    (docs/09 §5): nothing secret is at rest between dispatch and the Dev's
    runspec.get, and a slow container start or Redis restart cannot expire
    it. verify_auth has already authenticated the requester."""
    dt = mgr.dev_types.get(run.dev_type)
    if dt is None:
        return None            # dev type deleted mid-run → runspec.error
    env_creds, spec_files = _credential_spec(mgr, dt)
    # Stage-scope forge credentials (ISSUES #15): every stage clones the
    # repo (entrypoint always git-clones), so all stages need a
    # clone-capable token. EXECUTE gets the write token (push/PR). Other
    # stages prefer token_ro when set, else fall back to the write token
    # so private repos keep working without a separate RO PAT.
    # Reviewer PAT stays app-side only.
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
    if dt.mcp_setup_commands:             # docs/07 §5 step 5 (exit 14)
        payload["mcp_setup_commands"] = list(dt.mcp_setup_commands)
    # ADR-0014 D4: the activity-repo RO clone spec (secret half — the token
    # never rests on the Run). Absent for MAPPER (no mission, no repo), when
    # the forge is off, or before the boot mint — the entrypoint then falls
    # back to the Redis materialization.
    if mgr.internal_forge is not None and run.mission_type != "MAPPER":
        from ...ports.internal_forge import activity_repo_name
        creds = mgr.internal_forge.activity_credentials(
            activity_repo_name(mgr.instance_name, run.mission_key))
        if creds is not None:
            payload["activity_repo"] = {"url": creds.clone_url,
                                        "clone_user": creds.username,
                                        "token": creds.token}
    return payload


def _extra_repos_for(mgr, run: Run) -> list[dict]:
    """Read-only sibling clones for a run, built at request time (nothing
    secret at rest); read tokens preferred, write fallback (same rule as
    the primary token). Two sources:
    - the routing set's OTHER repos: ONBOARD only (multi-repo triage,
      item 2 full scope)
    - the instance's REFERENCE repos: EVERY mission stage (consultation
      material — docs sources, style guides; founder request 2026-07-15)"""
    wanted: list[str] = []
    if run.mission_type == "ONBOARD":
        wanted += list(mgr.instance.repos or [])
    if run.mission_type in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW"):
        wanted += list(mgr.instance.reference_repos or [])
    extras, seen = [], {run.repo_ref}
    for name in wanted:
        if name in seen:
            continue
        seen.add(name)
        inst_x = mgr.forges.instance(name)
        forge_x = mgr.forges.get(name)
        if inst_x is None or forge_x is None:
            continue         # removed mid-flight — proceed on what remains
        extras.append({"name": name, "url": inst_x.url,
                       "clone_user": forge_x.descriptor.clone_user,
                       "token": inst_x.token_ro or inst_x.token})
    return extras


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
            log.warning("secret env %s for dev type %s not stored — add it "
                        "on the admin Config page", var, dev_type.name)
    files = []
    secrets_dir = (Path(os.environ.get("DEVCAKE_DATA_DIR", "/data"))
                   / "secrets" / dev_type.name)
    for cf in harness.credential_files:
        p = secrets_dir / cf.secret_file
        if p.exists():
            files.append({"path_hint": cf.path_hint,
                          "content": p.read_text(), "mode": "600"})
        else:
            log.warning("credential file %s missing for %s — connect via OAuth "
                        "or upload it on the admin Config page", p, dev_type.name)
    return env, files


def _derive_seq(activity) -> int:
    """docs/02 §8 — max step number among prior feed artifacts + 1 (max, not
    count: collision-proof when a human deletes a transcript comment). Scans
    _unquoted bodies only (ADR-0014 D2): quoted marker mentions never count."""
    steps = [int(m.group(1)) for e in activity.entries
             for m in STEP_MARKER.finditer(_unquoted(e.body))]
    return (max(steps) + 1) if steps else 1


def _unique_name(name: str, used: set[str]) -> str:
    """docs/07 §2 collision rule: later duplicates get -2, -3, … suffixes."""
    stem, dot, ext = name.rpartition(".")
    cand, i = name, 1
    while cand in used:
        i += 1
        cand = f"{stem}-{i}.{ext}" if dot else f"{name}-{i}"
    used.add(cand)
    return cand


def _aware(ts: datetime) -> datetime:
    """Anchor timestamps come from three sources (audit log, run records,
    PMO comments); a stray naive one must not crash the scheduler."""
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _last_giveup_at(pmo_id: str) -> datetime | None:
    try:
        ts = None
        with open(markers.AUDIT_PATH) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get("pmo_id") == pmo_id \
                            and e.get("action") == "devcake_failed":
                        ts = _aware(datetime.fromisoformat(e["ts"]))
                except Exception:  # noqa: BLE001 — one bad audit line must never halt scheduling
                    log.debug("_last_giveup_at: skipping unparseable audit line")
                    continue
        return ts
    except FileNotFoundError:
        return None


def attempt_number(mgr, pmo_id: str, mission_type: str,
                    activity: Activity | None = None) -> int:
    """Count consecutive counted failures, independent of transcript seq.

    The count resets at the newest of: the last give-up event, ANY finished
    run for this mission (a later step finishing implies earlier failures
    were resolved), or the latest human feed comment (a human touching the
    mission is an intervention — the step deserves fresh attempts)."""
    all_runs = [r for r in mgr.runs.store.all()
                if r.mission_pmo_id == pmo_id and mgr._run_is_ours(r)]
    history = [r for r in all_runs if r.mission_type == mission_type]
    anchors = [t for t in [_last_giveup_at(pmo_id),
                           *(_aware(r.created_at) for r in all_runs
                             if r.state == "finished")] if t]
    if activity is not None:
        anchors += [_aware(e.ts) for e in activity.entries
                    if e.kind == "comment"
                    and not _is_devcake_comment(e.body)]
    since = max(anchors, default=None)
    ignored = ("DEV_AUTH", "DEV_FORGE_AUTH", "dev failure artifact (exit 13)")
    return 1 + sum(
        1 for r in history
        if r.state in ("failed", "timed_out", "orphaned")
        and not any(marker in (r.error or "") for marker in ignored)
        and (since is None or _aware(r.created_at) > since)
    )


async def _give_up(mgr, mission: Mission, mtype: MissionType, attempts: int) -> None:
    if LABEL_FAILED in mission.labels:
        return
    with tracer.start_as_current_span("mission.give_up") as span:
        span.set_attribute("devcake.mission.key", mission.key)
        span.set_attribute("devcake.mission.type", mtype.value)
        span.set_attribute("devcake.run.attempt", attempts)
        span.set_status(Status(StatusCode.ERROR,
                               f"gave up after {attempts} attempts"))
        await mgr.pmo.swap_labels(mission.ref, remove=set(), add={LABEL_FAILED})
        await mgr._feed(
            mission.pmo_id, mission.pmo_kind,
            f"⚠️ **DevCake gave up on this mission's {mtype.value} step** after "
            f"{attempts} failed attempts. Remove the `DEVCAKE-FAILED` label to retry. "
            f"(Traces: search run ids `{mission.key}-*` in OpenObserve.)")
        mgr._audit(mission.pmo_id, "devcake_failed", mtype.value)
    log.warning("DEVCAKE-FAILED applied to %s (%s)", mission.key, mtype.value)


def _activity_snapshot_files(payload: dict) -> list[dict]:
    """Activity payload → the flat file list a snapshot commit mirrors
    (identical layout to the Dev's /workspace/activity)."""
    files = []
    if payload.get("mission_md"):
        files.append({"path": "MISSION.md", "content_b64": base64.b64encode(
            payload["mission_md"].encode()).decode()})
    files.append({"path": "ACTIVITY.md", "content_b64": base64.b64encode(
        payload.get("activity_md", "").encode()).decode()})
    for a in payload.get("attachments", []):
        files.append({"path": Path(a["filename"]).name,
                      "content_b64": a["content_b64"]})
    return files


async def _push_activity_repo(mgr, mission, mtype, seq: int) -> None:
    """ADR-0014 D4: one snapshot commit per step dispatch. Any failure is
    audited loudly and swallowed — the run proceeds on the Redis fallback;
    Gitea down degrades to pre-ADR behavior, never to a halt."""
    if mgr.internal_forge is None:
        return
    try:
        payload = await activity_payload(mgr, mission.pmo_id, mission.pmo_kind)
        name = await mgr.internal_forge.ensure_activity_repo(
            mgr.instance_name, mission.key)
        await mgr.internal_forge.push_activity_snapshot(
            name, _activity_snapshot_files(payload),
            f"step {seq} {mtype.value} dispatch")
        log.info("activity repo %s: snapshot for step %d", name, seq)
    except Exception as e:
        log.exception("activity repo push failed for %s", mission.key)
        mgr._audit(mission.pmo_id, "activity_repo_push_failed",
                    f"{mission.key}: {type(e).__name__}: {str(e)[:180]}")


def _mission_md(m, attachment_lines=()) -> str:
    """ADR-0014 D3: MISSION.md — the brief. Stable regardless of feed length;
    every step playbook points here."""
    lines = [
        f"# {m.key}: {m.title}",
        f"> Kind: {m.pmo_kind} · Status: {m.status} · Priority: {m.priority} · URL: {m.url}",
        f"> Labels: {', '.join(sorted(m.labels)) or '(none)'}", "",
        "## Description", m.description or "(none)"]
    if attachment_lines:
        lines += ["", "## Mission attachments", *attachment_lines]
    return "\n".join(lines)


async def activity_payload(mgr, pmo_id: str, kind: str = "issue") -> dict:
    """ADR-0014 D3: MISSION.md = the brief; ACTIVITY.md = a faithful MIRROR
    of the feed — full bodies inline (never externalized), attachments by
    name in feed order, reply nesting; every attachment's bytes ride as
    sibling files."""
    if kind == "project":
        # projects have no comments/attachments: the brief IS the payload
        m = await mgr.pmo.get(MissionRef(pmo_id, "project"))
        md = "\n".join([
            f"# {m.key}: {m.title}",
            "> The mission brief lives in MISSION.md (same folder).", "",
            "## Activity", "(projects carry no comment feed — see child issues)"])
        return {"mission_md": _mission_md(m), "activity_md": md,
                "attachments": []}
    act = await mgr.pmo.get_activity(MissionRef(pmo_id, "issue"), full=True)
    m = act.mission
    attachments = []
    used: set[str] = {"ACTIVITY.md", "MISSION.md"}   # docs/07 §2 dedupe seed

    async def _materialize(att):
        """Download one file attachment into the folder; return its index
        line. The adapter resolves names (AttachmentRef.name) — the domain
        never parses vendor asset URLs."""
        try:
            data = await mgr.pmo.download_asset(att.url)
        except Exception:  # noqa: BLE001 — attachment fetch degrades to an inline "unavailable" marker; the mirror build continues
            return f"[attachment unavailable: {att.url}]"
        # basename BEFORE dedupe: a slash-bearing link text ([v1/r.md](…))
        # must yield the same name in the index, the snapshot commit, and
        # the folder — a path-y name would desync them and trip the
        # snapshot dup-path guard forever (full-diff review finding)
        raw = (Path(att.name).name if att.name
               else att.url.rsplit("/", 1)[-1][:80])
        fname = _unique_name(raw or "attachment.bin", used)
        attachments.append({"filename": fname,
                            "content_b64": base64.b64encode(data).decode()})
        return f"[attachment: {fname}]"

    mission_lines = []
    for att in act.mission_attachments:
        if att.kind == "link":
            mission_lines.append(f"[link: {att.name or att.url}]({att.url})")
        else:
            mission_lines.append(await _materialize(att))

    lines = []
    if act.truncated:   # the adapter's hard stop — never silent (ADR-0014)
        lines += ["⚠ FEED TRUNCATED — the feed exceeded the full-history "
                  "hard stop; the OLDEST entries are missing from this "
                  "mirror.", ""]
    lines += [
        f"# {m.key}: {m.title}",
        "> Brief: MISSION.md (same folder) — description, labels, mission attachments.", "",
        "## Activity (chronological mirror of the PMO feed)",
        "Entries marked 🧑 HUMAN are instructions/steering from a person — they",
        "are authoritative. Entries marked 🤖 DevCake are DevCake's own records.",
    ]
    by_id = {e.entry_id: e for e in act.entries if e.entry_id}
    for e in act.entries:
        body = e.body or ""
        # provenance is sentinel-based, never author-based (docs/03 §8a):
        # DevCake may post with the operator's own PMO credentials
        provenance = "🤖 DevCake" if _is_devcake_comment(body) else "🧑 HUMAN"
        lines.append(f"### {e.ts:%Y-%m-%d %H:%M} — {e.author} — {provenance} ({e.kind})")
        parent = by_id.get(e.parent_id) if e.parent_id else None
        if parent is not None:
            lines.append(f"↳ reply to {parent.author} @ {parent.ts:%Y-%m-%d %H:%M}")
        elif e.parent_id:
            lines.append("↳ reply to (deleted comment)")
        lines.append(body)                # full body — the mirror never trims
        for att in e.attachments:
            lines.append(await _materialize(att))
        lines.append("")
    return {"mission_md": _mission_md(m, mission_lines),
            "activity_md": "\n".join(lines), "attachments": attachments}


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
    from ...adapters.registry import make_gitea_adapter

    from ...config import RepoInstance

    if all_runs is None:
        all_runs = mgr.runs.store.all()

    async def _provision() -> str:
        # ensure service accounts first (lazy retry — boot provisioning may
        # have failed against a not-yet-ready Gitea; review finding #7)
        svc = mgr.internal_forge.service_tokens()
        if not svc:
            await mgr.internal_forge.ensure_service_accounts()
            svc = mgr.internal_forge.service_tokens() or {}
        creds = await mgr.internal_forge.ensure_mission_repo(
            mgr.instance_name, mission.key)
        # the APP-SIDE adapter uses the devcake-app SERVICE token (org owner:
        # write:issue for PR comments + write:repository for merge), NOT the
        # mission's Dev write token (write:repository only → issue-scope 403s;
        # review finding #1). The mission's write/read pair is the Dev's,
        # delivered via runspec.
        adapter = make_gitea_adapter(creds.clone_url, svc.get("app_token"),
                                     svc.get("reviewer_token"))
        # model_construct: internal repo names carry hyphens / exceed the
        # operator-name pattern by design — they are synthesized, not input
        inst = RepoInstance.model_construct(
            name=creds.repo_name, forge="gitea", url=creds.clone_url,
            default_branch="main", api_base=None)
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
