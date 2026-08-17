// Pure-node checks for the board's adoption-gate honesty (no browser).
// bucketize DROPS rows the gate hides, so the page must be able to SAY how
// many it dropped — a freshly created unadopted mission otherwise vanishes
// with no counter, no empty state, nothing (founder report 2026-08-05).
import assert from "node:assert/strict";
import {
  bucketize, columnOf, contextActions, unadoptedHiddenCount,
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

console.log("board helpers — contextActions");

check("MERGE rows offer Re-check freshness", () => {
  const items = contextActions(row(["DEVCAKE", "DEVCAKE-MERGE"], "in_progress"));
  assert.ok(items.some((it) => it.id === "force_freshness"));
  assert.ok(items.some((it) => it.id === "park"));
});

check("non-MERGE rows do not offer force_freshness", () => {
  const items = contextActions(row(["DEVCAKE", "DEVCAKE-REVIEW"], "in_progress"));
  assert.ok(!items.some((it) => it.id === "force_freshness"));
});

if (failed) {
  console.log(`\n${failed} board helper check(s) failed`);
  process.exit(1);
}
console.log("board helpers: all checks passed");
