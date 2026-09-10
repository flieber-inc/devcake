// Stalled dispatches (/health.stalled_dispatches, stalls.py): missions
// DevCake cannot start, past the threshold, with an age and a reason in
// plain words. Pure helpers — no React, no I/O — shared by the Overview
// alert and the Missions board badge so the two never disagree.

const KIND_WORDS = {
  harness: "Dev image not receipted",
  upstream: "upstream context missing",
  repo: "repository not ready",
  pmo: "board unreadable",
  config: "Dev Type not ready",
  other: "cannot start",
};

export function stallAge(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}

export function stallWords(row) {
  return KIND_WORDS[row.kind] || KIND_WORDS.other;
}

// instance:pmo_id → row, for the board's O(1) lookup per card
export function stallIndex(stalled) {
  const out = new Map();
  for (const row of stalled || []) out.set(`${row.instance}:${row.pmo_id}`, row);
  return out;
}

export function stallSeverity(stalled) {
  return (stalled || []).some((r) => r.severity === "critical") ? "critical" : "warning";
}
