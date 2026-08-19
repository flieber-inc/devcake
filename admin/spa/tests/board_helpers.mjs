// Pure-node checks for the board's adoption-gate honesty (no browser).
// bucketize DROPS rows the gate hides, so the page must be able to SAY how
// many it dropped — a freshly created unadopted mission otherwise vanishes
// with no counter, no empty state, nothing (founder report 2026-08-05).
import assert from "node:assert/strict";
import {
  bucketize, columnOf, unadoptedHiddenCount,
} from "../src/lib/board.js";

let failed = 0;
const check = (name, fn) => {
  try {
    fn();
    console.log(`  ✓ ${name}`);
  } catch (e) {
    failed += 1;
    console.log(`  ✗ ${name}: ${e.message}`);
  }
};

const row = (labels, status = "backlog", extra = {}) => ({
  key: "t/x#1", status, labels, updated_at: "2026-08-05T00:00:00Z", ...extra,
});

console.log("board helpers — adoption gate");

check("opt_in: an unlabeled mission is hidden AND counted", () => {
  const rows = [row([]), row(["DEVCAKE"])];
  assert.equal(columnOf(rows[0], "opt_in"), null);
  assert.equal(unadoptedHiddenCount(rows, "opt_in"), 1);
});

check("the count equals what bucketize actually drops", () => {
  const rows = [row([]), row([]), row(["DEVCAKE"]), row(["DEVCAKE"], "done")];
  const shown = Object.values(bucketize(rows, "opt_in"))
    .reduce((n, list) => n + list.length, 0);
  assert.equal(shown, 2);
  assert.equal(unadoptedHiddenCount(rows, "opt_in"), rows.length - shown);
});

check("opt_out: nothing is hidden by adoption, so the count is 0", () => {
  const rows = [row([]), row([])];
  assert.equal(unadoptedHiddenCount(rows, "opt_out"), 0);
  assert.notEqual(columnOf(rows[0], "opt_out"), null);
});

check("a closed unlabeled mission counts — labeling it would surface it", () => {
  const rows = [row([], "done"), row([], "canceled")];
  assert.equal(unadoptedHiddenCount(rows, "opt_in"), 2);
  assert.equal(columnOf(row(["DEVCAKE"], "done"), "opt_in"), "done");
});

check("a LABELED row hidden for another reason is NOT counted", () => {
  // in_progress with no stage label is hidden too, but no label fixes it —
  // counting it under "not adopted" would misdirect the operator
  const hidden = row(["DEVCAKE"], "in_progress");
  assert.equal(columnOf(hidden, "opt_in"), null);
  assert.equal(unadoptedHiddenCount([hidden], "opt_in"), 0);
});

check("empty / missing input is 0, never a crash", () => {
  assert.equal(unadoptedHiddenCount([], "opt_in"), 0);
  assert.equal(unadoptedHiddenCount(null, "opt_in"), 0);
  assert.equal(unadoptedHiddenCount([null], "opt_in"), 0);
});

console.log("board helpers — Done / stage totals (CAKE-127)");

check("Done keeps every row — no silent 30-cap", () => {
  const rows = Array.from({ length: 35 }, (_, i) =>
    row(["DEVCAKE"], "done", {
      key: `t/done#${i + 1}`,
      updated_at: new Date(Date.UTC(2026, 7, 1, i)).toISOString(),
    }),
  );
  const buckets = bucketize(rows, "opt_in");
  assert.equal(buckets.done.length, 35);
});

check("Done stays newest-updated first after keeping the full set", () => {
  const rows = [
    row(["DEVCAKE"], "done", { key: "old", updated_at: "2026-08-01T00:00:00Z" }),
    row(["DEVCAKE"], "done", { key: "new", updated_at: "2026-08-19T00:00:00Z" }),
    row(["DEVCAKE"], "done", { key: "mid", updated_at: "2026-08-10T00:00:00Z" }),
  ];
  const keys = bucketize(rows, "opt_in").done.map((r) => r.key);
  assert.deepEqual(keys, ["new", "mid", "old"]);
});

check("non-Done buckets stay uncapped", () => {
  const rows = Array.from({ length: 40 }, (_, i) =>
    row(["DEVCAKE"], "backlog", { key: `t/backlog#${i + 1}` }),
  );
  const buckets = bucketize(rows, "opt_in");
  assert.equal(buckets.backlog.length, 40);
});

// contextActions precondition replay lives in contracts.mjs (CAKE-88) —
// do not keep a second SPA-only expectation set here.

if (failed) {
  console.log(`\n${failed} board helper check(s) failed`);
  process.exit(1);
}
console.log("board helpers: all checks passed");
