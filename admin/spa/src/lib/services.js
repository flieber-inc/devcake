// Shared service/dev-fleet status helpers (v0.1.1 B2/B3) — the single home
// for the sidebar services list (previously duplicated in Sidebar and
// Overview) and the Devs-card state derivation.

import { TERMINAL_STATES } from "./runStates.js";

// [health key, display label] — 3×2 grid: row 1 control plane, row 2 run
// infrastructure. Gitea reports as health.internal_forge ({ok,detail} or
// null when GITEA_ADMIN_PASSWORD is unset → unknown/grey).
export const SERVICES = [
  ["app", "app"], ["pmo", "pmo"], ["redis", "redis"],
  ["dagu", "dagu"], ["internal_forge", "gitea"], ["openobserve", "logs"],
];

// tri-state bool for the sidebar dots: true | false | undefined (unknown).
// Sidebar lights are green/red/grey ONLY (founder decision) — no blue.
export function serviceValue(health, key) {
  if (key === "internal_forge") {
    const g = health.internal_forge;
    return g == null ? undefined : !!g.ok;
  }
  return health[key];
}

// Devs-card state per Dev Type (the Runs-table color code): red = broken
// (breaker latched / no credentials), blue = a run is using it, green = ok.
export function devTypeState(dt, health, runs) {
  const breaker = (health.circuit_breakers || {})[dt.name];
  if (breaker) return { state: "broken", why: breaker };
  if (dt.credentials_ready === false) {
    return { state: "broken", why: "no credentials configured" };
  }
  const busy = (runs || []).some(
    (r) => r.dev_type === dt.name && !TERMINAL_STATES.includes(r.state));
  return busy ? { state: "running" } : { state: "ok" };
}
