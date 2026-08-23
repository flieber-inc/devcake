// Derive the active alert list from /health. Every alert here is
// incident-driven and safety-critical — the shell must keep them visible
// on every page (full stack on Overview, slim strip elsewhere).
//
// `dismissable` marks compliance/advisory alerts the operator may mute; the
// dismissal key includes a content hash so changed content resurfaces.

function hash(s) {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(36);
}

export function alertKey(a) {
  return `${a.id}:${hash(`${a.title}|${a.body || ""}`)}`;
}

export default function deriveAlerts(health) {
  const alerts = [];

  if (health.intake_paused) {
    alerts.push({
      id: "intake",
      severity: "info",
      title: "Intake paused",
      body:
        ((health.active_runs || 0) > 0
          ? `${health.active_runs} run${health.active_runs > 1 ? "s" : ""} still finishing (they may still update labels/statuses as they complete)`
          : "all runs drained; your PMO is all yours") +
        ". Resume with the sidebar master switch.",
    });
  } else {
    // Per-PMO freezes only show when the master is ON — otherwise the master
    // alert already covers "nothing dispatches". Forgotten team freezes are
    // the failure mode multi-PMO intake makes possible.
    const pausedPmos = Object.entries(health.pmo_instances || {})
      .filter(([, v]) => v && v.intake_paused)
      .map(([name]) => name);
    if (pausedPmos.length === 1) {
      alerts.push({
        id: "pmo-intake",
        severity: "info",
        title: `Intake paused for ${pausedPmos[0]}`,
        body: `PMO '${pausedPmos[0]}' dispatches no new runs. Other PMOs keep baking. Resume on Configuration → PMO.`,
      });
    } else if (pausedPmos.length > 1) {
      alerts.push({
        id: "pmo-intake",
        severity: "info",
        title: `Intake paused for ${pausedPmos.length} PMOs`,
        body: `${pausedPmos.join(", ")} dispatch no new runs. Resume on Configuration → PMO.`,
      });
    }
  }
  if ((health.dependency_cycles || []).length > 0) {
    alerts.push({
      id: "cycles",
      severity: "warning",
      title: "Dependency cycle detected",
      body:
        health.dependency_cycles.map((c) => [...c, c[0]].join(" → ")).join(" · ") +
        " — these missions will never start until a relation is deleted in your PMO.",
    });
  }

  // forge_protection is {repoName: protection|null} since M10 (repos plural)
  for (const [repoName, prot] of Object.entries(health.forge_protection || {})) {
    if (prot && prot.protected === false) {
      alerts.push({
        id: `forge:${repoName}`,
        severity: "critical",
        dismissable: true, // compliance nag — the operator may accept the risk
        title: `Default branch of repo '${repoName}' is unprotected`,
        body:
          "A Dev's forge token could merge to it without review. Enable branch " +
          "protection (require PRs + 1 approval; Dev account must not bypass). " +
          "This is advisory — DevCake does not block dispatch on it. " +
          "Auto-merge off only stops the app, not the Dev.",
      });
    }
  }

  // adapters no PMO selects (2026-08-01 incident): each one is rebuilt on
  // every config/secret reload and probed on every full forge sweep — pure
  // latency cost. Count in the title so a changed count resurfaces the alert.
  for (const id of health.cron_degraded || []) {
    alerts.push({
      id: `cron-degraded:${id}`,
      severity: "warning",
      dismissable: true,
      title: `Scheduled task ${id} is degraded`,
      body: "The last 3 automatic fires failed. Automatic fires are paused; Run now still works, and a successful fire resumes the schedule.",
    });
  }
  for (const card of health.memory_curator_no_board || []) {
    alerts.push({
      id: `memory-curator-no-board:${card}`,
      severity: "warning",
      dismissable: true,
      title: `Notebook ${card} has no Curator board`,
      body: "Leads are queuing but no worker reviews them. Create a PMO instance whose ONLY repository is this notebook — the Memory Curator then files curation tickets there.",
    });
  }
  for (const card of health.claims_queue_capped || []) {
    alerts.push({
      id: `claims-queue-capped:${card}`,
      severity: "warning",
      dismissable: true,
      title: `Memory notebook ${card} is full of unreviewed leads`,
      body: "New leads are being refused. Run the Memory Curator (Configuration → Scheduled Tasks) to review the queue, or raise the cap under Limits → Counting budgets — old leads are never deleted automatically.",
    });
  }

  const unused = health.unused_repos;
  if (unused && unused.count > 0) {
    const shown = unused.names.slice(0, 8).join(", ");
    const more = unused.count > 8 ? ` and ${unused.count - 8} more` : "";
    alerts.push({
      id: "unused-repos",
      severity: "warning",
      dismissable: true,
      title: `${unused.count} of ${unused.configured} configured repositories are unused`,
      body:
        `${shown}${more} — selected as work, reference, or memory on no PMO ` +
        "or Dev Type, but still probed on every forge sweep (skill sources " +
        "are a separate connection type, not repo cards). Remove these " +
        "repositories on Repositories → ⋯ → Remove unused repositories " +
        "(their stored tokens are deleted with them on Save).",
    });
  }

  // server-computed credential-posture warnings: loud but
  // dismissable — the operator may accept the risk; a changed body (e.g. a
  // different missing credential) changes the key and resurfaces the alert
  for (const w of health.security_warnings || []) {
    alerts.push({
      id: w.id,
      severity: w.severity || "warning",
      dismissable: true,
      title: w.title,
      body: w.body,
    });
  }

  // an ACTIVE prompt template that no longer resolves — dispatch silently
  // falls back to the built-in default until fixed (v0.1.1)
  for (const w of health.prompt_template_warnings || []) {
    alerts.push({
      id: `prompt-template:${w.split(":")[0]}`,
      severity: "warning",
      dismissable: true,
      title: "Prompt template missing",
      body: w,
    });
  }

  // A per-instance poll segment failed with a PERMANENT error (revoked key,
  // deleted team — main.py `_poll_degraded`). The other instances keep
  // polling; this names the sick one. Not dismissable: while this is set,
  // /health `last_poll_at` will update but ZERO missions from this instance
  // are being processed — the operator needs to know before the "Last polled
  // just now" cadence line misleads them (docs/11 §0 honest-by-design).
  const degraded = Object.entries(health.poll_degraded || {});
  if (degraded.length > 0) {
    alerts.push({
      id: "poll-degraded",
      severity: "critical",
      title:
        degraded.length === 1
          ? `PMO instance '${degraded[0][0]}' is not polling`
          : `${degraded.length} PMO instances are not polling`,
      body:
        degraded.map(([name, msg]) => `${name}: ${msg}`).join(" · ") +
        " — no new missions from this instance until it recovers. " +
        "Check the API key in Configuration → PMO; DevCake auto-heals when the " +
        "next segment succeeds.",
    });
  }

  if (Object.keys(health.anomalies || {}).length > 0) {
    alerts.push({
      id: "anomalies",
      severity: "warning",
      dismissable: true, // informational; new anomalies change the key and resurface
      title: "Out-of-pipeline activity",
      body:
        Object.values(health.anomalies).join(" · ") +
        " — see the mission's activity feed.",
    });
  }

  // merge hand-offs and needs-human batons are NOT alerts — they render as
  // the Needs Human Action panel on Overview (counted in the sidebar pill
  // separately via the stat card, not here)

  // ADR-0018 — model-backend degradation. Deliberately NOT a circuit breaker:
  // it throttles the named Dev Types to one probe run and clears itself when
  // runs succeed, so the remediation is the opposite of the breaker advice
  // below (there is no credential to fix).
  const backend = Object.entries(health.dev_backend_degraded || {});
  if (backend.length > 0) {
    alerts.push({
      id: "dev-backend",
      severity: "warning", // throttled, not stopped — and it self-heals
      title:
        backend.length === 1
          ? `Dev Type '${backend[0][0]}' is throttled — model backend degraded`
          : `${backend.length} Dev Types are throttled — model backend degraded`,
      body:
        backend.map(([name, msg]) => `${name}: ${msg}`).join(" · ") +
        " — DevCake has throttled these Dev Types to one probe run at a time " +
        "and resumes full concurrency automatically once runs succeed. " +
        "No credential change is needed; check your model provider's status.",
    });
  }

  // ADR-0024: repo source mirrors. Fail-closed dispatch precondition, so a
  // failing mirror silently FREEZES every mission touching that repo — same
  // honesty argument as poll-degraded: not dismissable.
  const mirror = health.repo_mirror || {};
  if (mirror.volume_error) {
    alerts.push({
      id: "mirror-volume",
      severity: "critical",
      title: "Repo mirror volume is unusable — dispatch is frozen",
      body: mirror.volume_error,
    });
  }
  const badMirrors = Object.entries(mirror.mirrors || {}).filter(
    ([, st]) => st && st.ok === false,
  );
  if (badMirrors.length > 0) {
    alerts.push({
      id: "mirror-sync",
      severity: "warning",
      title:
        badMirrors.length === 1
          ? `Repo mirror '${badMirrors[0][0]}' is not syncing`
          : `${badMirrors.length} repo mirrors are not syncing`,
      body:
        badMirrors.map(([name, st]) => `${name}: ${st.detail || "sync failed"}`).join(" · ") +
        " — missions needing these repositories will not dispatch until the " +
        "sync succeeds (retried every poll cycle). Auth failures also latch " +
        "the repository's circuit breaker below.",
    });
  }

  // ADR-0025: per-run workspace base (host bind). An unusable base freezes
  // dispatch exactly like the mirror volume; leaked dirs mean the cleanup
  // hooks AND the sweep are losing to something (disk fills → DEV_FORGE
  // retry churn), so both surface loudly.
  const ws = health.workspaces || {};
  if (ws.volume_error) {
    alerts.push({
      id: "workspaces-volume",
      severity: "critical",
      title: "Workspace base is unusable — dispatch is frozen",
      body: ws.volume_error,
    });
  }
  if ((ws.leaked || 0) > 0) {
    alerts.push({
      id: "workspaces-leaked",
      severity: "warning",
      title:
        ws.leaked === 1
          ? "1 leaked run workspace on disk"
          : `${ws.leaked} leaked run workspaces on disk`,
      body:
        "Terminal runs left workspace directories behind that the periodic " +
        "sweep could not remove — check $DEVCAKE_WS_HOST permissions and " +
        "free disk (docs/13 §8).",
    });
  }

  // Host baker is a process started by ./up.sh, not a container. If its
  // heartbeat on /data is missing or stale, pins will not compile — same
  // honesty class as a tripped breaker: critical, not dismissable.
  const bake = health.bake_status || {};
  if (bake.baker_alive === false) {
    alerts.push({
      id: "baker-dead",
      severity: "critical",
      title: "Host baker is not running",
      body:
        (bake.baker_detail ? `${bake.baker_detail}. ` : "") +
        "Harness images will not compile. Restart with ./up.sh " +
        "(or ./up.sh --foreground-baker when the parent may reap detached " +
        "children). The baker is a host process started with the app, not a " +
        "container.",
    });
  } else if (bake.state === "error" && bake.detail && bake.baker_alive) {
    alerts.push({
      id: "baker-error",
      severity: "warning",
      title: "Host baker failed a compile",
      body: String(bake.detail),
    });
  }

  const breakers = Object.entries(health.circuit_breakers || {});
  if (breakers.length > 0) {
    // Per-ENTRY remediation. The old single trailing sentence was chosen by one
    // `repo:` prefix test, so a mixed map told half the operator's breakers the
    // wrong fix — and any future namespace would have inherited that bug.
    const remedy = (k) =>
      k.startsWith("repo:")
        ? "update that repository's forge token with write access to resume"
        : "upload or refresh that Dev Type's credential to resume";
    alerts.push({
      id: "breakers",
      severity: "critical",
      title: "Circuit breaker tripped",
      body: breakers
        .map(([k, v]) => `${k} (${v}) — ${remedy(k)}`)
        .join(" · "),
    });
  }

  return alerts;
}
