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

  if (health.forge_protection && health.forge_protection.protected === false) {
    alerts.push({
      id: "forge",
      severity: "critical",
      dismissable: true, // compliance nag — the operator may accept the risk
      title: "Default branch is unprotected",
      body:
        "A Dev's forge token could merge to it without review. Enable branch " +
        "protection (require PRs + 1 approval); DevCake's own pipeline keeps working.",
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

  const breakers = Object.entries(health.circuit_breakers || {});
  if (breakers.length > 0) {
    alerts.push({
      id: "breakers",
      severity: "critical",
      title: "Circuit breaker tripped",
      body:
        breakers.map(([k, v]) => `${k} (${v})`).join(" · ") +
        " — upload or refresh that Dev Type's credential to resume.",
    });
  }

  return alerts;
}
