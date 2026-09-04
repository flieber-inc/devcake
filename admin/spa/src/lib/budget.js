// Request-budget readout for a PMO connection (ADR-0040 visibility).
// Pure helpers over the /health payload: the bucket a named instance spends
// from, a one-line summary for the card, the hover details, and a tone.
// Hermetic — covered by tests/pmo_budget_helpers.mjs.

const fmt = (n) => Number(n).toLocaleString("en-US");

function hhmm(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm} UTC`;
}

/** The budget bucket `name` spends from, or null when /health has none. */
export function budgetForInstance(health, name) {
  const rows = Object.values((health && health.pmo_budget) || {});
  const row = rows.find((r) => (r.instances || []).includes(name));
  if (!row) return null;
  const demand = row.demand_per_hour || {};
  const mine = demand[name];
  const total = Object.values(demand).reduce(
    (acc, v) => acc + (typeof v === "number" ? v : 0),
    0,
  );
  return {
    label: row.label || "",
    limit: row.limit ?? null,
    remaining: row.remaining_estimate ?? row.remaining ?? null,
    resetAt: row.reset_at ?? null,
    blockedUntil: row.blocked_until ?? null,
    perHour: typeof mine === "number" ? mine : null,
    totalPerHour: total,
    sharedWith: (row.instances || []).filter((n) => n !== name),
    limitedLastHour: row.limited_last_hour || 0,
    waits: row.waits || 0,
    foreignSpend: row.foreign_spend || 0,
  };
}

/** Share of the credential's hour the measured demand takes (0–100+), or null. */
export function budgetShare(b) {
  if (!b || !b.limit) return null;
  return Math.round(((b.totalPerHour || 0) / b.limit) * 100);
}

/** One line for the card. */
export function budgetLine(b) {
  if (!b) return null;
  if (b.perHour == null) return "requests: measuring this connection's demand…";
  const mine = `about ${fmt(b.perHour)} requests/hour`;
  if (b.limit == null) return `${mine} — the tracker publishes no limit`;
  const share = budgetShare(b);
  const shared = b.sharedWith.length
    ? `, ${fmt(b.totalPerHour)}/hour together with ${b.sharedWith.join(", ")}`
    : "";
  return `${mine} of ${fmt(b.limit)} (${share}% of the credential's hour${shared})`;
}

/** Hover details (title attribute; one fact per line). */
export function budgetDetails(b) {
  if (!b) return "";
  const out = [];
  if (b.label) out.push(`credential: ${b.label}`);
  if (b.remaining != null) out.push(`remaining: ${fmt(b.remaining)}`);
  if (b.resetAt) out.push(`refills by: ${hhmm(b.resetAt)}`);
  out.push(`rejected by the tracker in the last hour: ${fmt(b.limitedLastHour)}`);
  if (b.waits) out.push(`write-backs that waited for quota: ${fmt(b.waits)}`);
  if (b.foreignSpend) out.push(`spent by another consumer this hour: ${fmt(b.foreignSpend)}`);
  if (b.blockedUntil) out.push(`paused by the tracker until ${hhmm(b.blockedUntil)}`);
  return out.join("\n");
}

/** "critical" (rejected / paused) · "warning" (≥ 80 % of the hour) · "neutral". */
export function budgetTone(b) {
  if (!b) return "neutral";
  if (b.limitedLastHour > 0 || b.blockedUntil) return "critical";
  const share = budgetShare(b);
  if (share != null && share >= 80) return "warning";
  return "neutral";
}
