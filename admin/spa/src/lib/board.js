// Pure column-derivation for the Missions board.
//
// Mirrors `derive()` in `app/devcake/domain/model.py` (first match wins).
// Runs client-side over the /missions cache so the UI can filter, sort,
// and re-column instantly without another server call. No React, no I/O —
// unit-friendly.

export const COLUMNS = [
  { id: "backlog", label: "Backlog" },
  { id: "plan", label: "Plan" },
  { id: "execute", label: "Execute" },
  { id: "review", label: "Review" },
  { id: "merge", label: "Merge wait" },
  { id: "needs_human", label: "Needs human" },
  { id: "done", label: "Done" },
];

const STAGE_LABELS = new Set(["DEVCAKE-PLAN", "DEVCAKE-EXECUTE", "DEVCAKE-REVIEW"]);

// Reason badge shown on Needs-human cards. First-match precedence mirrors
// the derivation table so a single mission never appears with two badges.
export function needsHumanReason(labels) {
  const set = new Set(labels || []);
  const stage = [...set].filter((l) => STAGE_LABELS.has(l));
  if (stage.length >= 2) return "conflict";
  if (set.has("DEVCAKE-SKIP")) return "parked";
  if (set.has("DEVCAKE-FAILED")) return "failed";
  if (set.has("DEVCAKE-NEEDS-HUMAN")) return "needs human";
  return null;
}

// Returns the column id, or `null` if the row is hidden from the board
// (opt-in without DEVCAKE, or in_progress with no stage label — "not
// DevCake's" in derivation terms).
export function columnOf(row, adoptionMode) {
  if (!row) return null;
  const labels = new Set(row.labels || []);
  const hasAnyDevcake = [...labels].some((l) => l === "DEVCAKE" || l.startsWith("DEVCAKE-"));

  if (row.status === "done" || row.status === "canceled") {
    return hasAnyDevcake ? "done" : null;
  }

  // opt-in adoption gate — hidden from the board just like scheduler ignores it
  if (adoptionMode === "opt_in" && !labels.has("DEVCAKE")) return null;

  const stage = [...labels].filter((l) => STAGE_LABELS.has(l));
  if (stage.length >= 2) return "needs_human";                       // conflict
  if (labels.has("DEVCAKE-SKIP")) return "needs_human";              // parked
  if (labels.has("DEVCAKE-FAILED")) return "needs_human";            // failed
  if (labels.has("DEVCAKE-NEEDS-HUMAN")) return "needs_human";       // steering wanted
  if (labels.has("DEVCAKE-MERGE")) return "merge";
  if (stage.length === 1) {
    if (stage[0] === "DEVCAKE-PLAN") return "plan";
    if (stage[0] === "DEVCAKE-EXECUTE") return "execute";
    if (stage[0] === "DEVCAKE-REVIEW") return "review";
  }
  if (row.status === "backlog") return "backlog";
  return null; // in_progress without a stage label — hidden
}

// How many rows the ADOPTION gate hides — opt-in missions with no DEVCAKE
// label. Defined against columnOf so it can never drift from the real drop
// rule, and narrowed by the label so it counts only the adoption omission
// (an in_progress row WITH the label is hidden too, but for a different
// reason and no label would fix it). The Missions page surfaces this:
// bucketize silently drops these rows, so a freshly created unadopted
// mission simply vanished from the board (founder report 2026-08-05).
export function unadoptedHiddenCount(rows, adoptionMode) {
  if (adoptionMode !== "opt_in") return 0;
  return (rows || []).filter(
    (r) => r && columnOf(r, adoptionMode) === null
      && !(r.labels || []).includes("DEVCAKE")
  ).length;
}

// Buckets an array of rows into COLUMNS. Done column caps to `doneCap`
// newest by `updated_at` (default 30) to keep the board scannable.
export function bucketize(rows, adoptionMode, { doneCap = 30 } = {}) {
  const buckets = Object.fromEntries(COLUMNS.map((c) => [c.id, []]));
  for (const row of rows || []) {
    const col = columnOf(row, adoptionMode);
    if (col) buckets[col].push(row);
  }
  // Newest updated first per column. Done is capped.
  for (const col of COLUMNS) {
    buckets[col.id].sort((a, b) => {
      const ta = Date.parse(a.updated_at || 0) || 0;
      const tb = Date.parse(b.updated_at || 0) || 0;
      return tb - ta;
    });
  }
  if (buckets.done.length > doneCap) buckets.done = buckets.done.slice(0, doneCap);
  return buckets;
}

// Menu items available on a card for its current label state.
// Empty when nothing applies — the caller should not render an empty menu.
export function contextActions(row) {
  const set = new Set(row.labels || []);
  const items = [];
  if (set.has("DEVCAKE-FAILED"))       items.push({ id: "retry",  label: "Retry" });
  if (set.has("DEVCAKE-NEEDS-HUMAN"))  items.push({ id: "resume", label: "Resume" });
  if (set.has("DEVCAKE-SKIP"))         items.push({ id: "unpark", label: "Unpark" });
  else                                  items.push({ id: "park",   label: "Park", danger: false });
  return items;
}
