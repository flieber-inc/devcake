// Pure dependency-graph derivation for the mission drawer's chain panel.
//
// Built entirely from the /missions snapshot already in memory — the PMO is
// the source of truth and the server's cross-instance key resolution is
// "advisory display only" (poll.py), so this module renders what the
// snapshot supports and says so honestly where it can't:
//   - blocked_by entries are mission KEYS when resolvable, RAW vendor ids
//     when a blocker aged out of every snapshot — raw ids stay unresolved
//     nodes, never fabricated.
//   - keys can collide across instances (gitea keys are owner/repo#N;
//     config only forbids same-(system,api_base,team_key) twins), so
//     resolution prefers the same instance, then a UNIQUE foreign match;
//     an ambiguous foreign match is unresolved (matches the server stance).
// No React, no I/O — unit-friendly (the board.js mold).

import { COLUMNS, columnOf, needsHumanReason } from "./board.js";

// Node identity: instance-qualified — bare pmo_id collides across
// multi-gitea fleets (per-repo integer ids, ADR-0009).
export const nodeId = (row) => `${row.instance}:${row.pmo_id}`;

export function buildDepIndex(rows) {
  const byKey = new Map(); // key → row[] (collisions possible cross-instance)
  for (const r of rows || []) {
    if (!byKey.has(r.key)) byKey.set(r.key, []);
    byKey.get(r.key).push(r);
  }
  const index = { byKey, dependents: new Map() }; // nodeId → row[]
  for (const r of rows || []) {
    for (const k of r.blocked_by || []) {
      const target = resolveKey(index, k, r.instance);
      if (target) {
        const id = nodeId(target);
        if (!index.dependents.has(id)) index.dependents.set(id, []);
        index.dependents.get(id).push(r);
      }
    }
  }
  return index;
}

export function resolveKey(index, key, instance) {
  const cands = index.byKey.get(key) || [];
  if (cands.length === 1) return cands[0];
  if (cands.length > 1) {
    const same = cands.filter((r) => r.instance === instance);
    if (same.length === 1) return same[0];
  }
  return null; // absent (aged out / raw vendor id) or ambiguous — unresolved
}

function neighbors(index, row, dir) {
  return dir === "up"
    ? (row.blocked_by || []).map((k) => resolveKey(index, k, row.instance)).filter(Boolean)
    : index.dependents.get(nodeId(row)) || [];
}

// Direct ring + the transitive remainder in each direction. Visited sets
// make the walk cycle-safe (the scheduler's find_cycles names cycles; this
// walk merely must not hang on one).
export function chainAround(index, row) {
  const blockers = (row.blocked_by || []).map((k) => ({
    key: k,
    row: resolveKey(index, k, row.instance),
  }));
  const dependents = neighbors(index, row, "down");
  const beyond = (dir, ring) => {
    const out = [];
    const seen = new Set([nodeId(row), ...ring.map(nodeId)]);
    let frontier = ring;
    while (frontier.length) {
      const next = [];
      for (const r of frontier) {
        for (const n of neighbors(index, r, dir)) {
          if (!seen.has(nodeId(n))) {
            seen.add(nodeId(n));
            next.push(n);
            out.push(n);
          }
        }
      }
      frontier = next;
    }
    return out;
  };
  return {
    blockers,
    dependents,
    upstream: beyond("up", blockers.map((b) => b.row).filter(Boolean)),
    downstream: beyond("down", dependents),
  };
}

// /health.dependency_cycles entries are mission keys, prefixed
// "instance:key" when more than one manager runs (health.py). Instance
// names and keys never contain ":" themselves, so both forms are kept.
export function parseCycles(cycles) {
  const members = new Set();
  for (const cyc of cycles || []) {
    for (const entry of cyc || []) {
      members.add(entry);
      const i = entry.indexOf(":");
      if (i !== -1) members.add(entry.slice(i + 1));
    }
  }
  return members;
}

export function inCycle(row, cycleMembers) {
  return cycleMembers.has(row.key) || cycleMembers.has(`${row.instance}:${row.key}`);
}

// Status line for a chain node. Guard labels are named verbatim — a FAILED
// or SKIP blocker keeps blocking FOREVER until a human acts (docs/04 §2),
// and hiding that behind a soft word would lie about the deadlock.
const GUARD_TEXT = {
  conflict: "stage conflict — blocks until a human acts",
  parked: "parked (DEVCAKE-SKIP) — blocks until a human acts",
  failed: "failed (DEVCAKE-FAILED) — blocks until a human acts",
  "needs human": "needs human (DEVCAKE-NEEDS-HUMAN) — blocks until a human acts",
};

export function nodeStatus(row, adoptionMode, { asBlocker = false } = {}) {
  if (row.status === "done" || row.status === "canceled") {
    const word = row.status === "done" ? "Done" : "Canceled";
    return {
      text: asBlocker ? `${word} — no longer blocking` : word.toLowerCase(),
      tone: "satisfied",
    };
  }
  const guard = needsHumanReason(row.labels);
  if (guard && asBlocker) return { text: GUARD_TEXT[guard], tone: "guard" };
  if (guard) return { text: guard, tone: "guard" };
  const col = columnOf(row, adoptionMode);
  const label = COLUMNS.find((c) => c.id === col)?.label;
  return {
    text: (label || row.status || "").toLowerCase() + (asBlocker ? " — open" : ""),
    tone: "open",
  };
}
