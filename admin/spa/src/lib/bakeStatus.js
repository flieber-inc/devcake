// Host-baker status readouts for the Dev Types panel (docs/11 `bake_status`).
// The baker rebuilds its status every tick; the last prune's outcome rides
// along with its time (core.carry_last_prune), so the panel can always say
// what the last prune did and when — and why the baker cannot act right now.

function hhmm(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} UTC`;
}

/** One sentence about the last prune, or "" when none has run. */
export function describePrune(bake) {
  const p = bake && bake.prune;
  if (!p || typeof p !== "object") return "";
  const when = hhmm(p.at);
  const prefix = when ? `Last prune (${when}): ` : "Last prune: ";
  if (p.detail) return prefix + p.detail;
  const n = Array.isArray(p.removed) ? p.removed.length : 0;
  if (n === 0) return prefix + "nothing to prune.";
  const dropped = Array.isArray(p.receipts_dropped) ? p.receipts_dropped.length : 0;
  return prefix + `removed ${n} image(s)` + (dropped ? `, ${dropped} receipt(s) dropped.` : ".");
}

/** Why a request would not be acted on right now, or "" when the baker is
 *  ready: not alive, or its status is not `ready`/`baking` (a digest
 *  mismatch after a checkout move, a listing failure, …). */
export function bakerBlockedReason(bake) {
  if (!bake || typeof bake !== "object") return "";
  if (bake.baker_alive === false) return "The host baker is not running.";
  const state = bake.state || "";
  if (state && state !== "ready" && state !== "baking" && state !== "virgin") {
    return `The host baker is not acting (${state}): ${bake.detail || "no detail"}`;
  }
  return "";
}

/** Whether Devs can run containers on this host, from the newest
 *  nested-engine receipt the baker published (docs/11 `bake_status.nested`):
 *  "" when no receipt exists yet, one sentence otherwise. */
export function describeNestedEngine(bake) {
  const n = bake && bake.nested;
  if (!n || typeof n !== "object") return "";
  const raw = String(n.measured_at || "");
  const when = raw.length >= 13
    ? `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)} ${raw.slice(9, 11)}:${raw.slice(11, 13)} UTC`
    : "";
  const tail = when ? ` (measured ${when})` : "";
  if (n.rig_ok) {
    const compose = n.compose_ok === true ? ", compose included"
      : n.compose_ok === false ? "; docker compose is not working (see the probe log)" : "";
    return `Nested engine: Devs can run containers on this host${compose}${tail}.`;
  }
  const why = n.first_red ? String(n.first_red) : "see the probe receipt";
  return `Nested engine unavailable: ${why}${tail}. Devs are told so in their prompt; runs still launch.`;
}
