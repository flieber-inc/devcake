// Pure-node pins for the Runs token/cost absence phrasebook (CAKE-171)
// and empty-card cost composition (CAKE-174). Literal expected strings
// only — never re-derive the helper's branches.
import assert from "node:assert/strict";
import { absenceCopy, costAbsenceCopy } from "../src/lib/tokenCostAbsence.js";

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

check("dispatched → waiting phrase", () =>
  assert.equal(absenceCopy({ state: "dispatched" }),
    "available after the run ends"));
check("running → waiting phrase", () =>
  assert.equal(absenceCopy({ state: "running" }),
    "available after the run ends"));
check("finalizing → waiting phrase (Dev exited; report not stamped yet)", () =>
  assert.equal(absenceCopy({ state: "finalizing" }),
    "available after the run ends"));

check("failed → not extracted (run failed)", () =>
  assert.equal(absenceCopy({ state: "failed" }),
    "not extracted (run failed)"));
check("orphaned → not extracted (run failed)", () =>
  assert.equal(absenceCopy({ state: "orphaned" }),
    "not extracted (run failed)"));
check("timed_out → not extracted (run failed)", () =>
  assert.equal(absenceCopy({ state: "timed_out" }),
    "not extracted (run failed)"));

check("finished, no source → not extracted", () =>
  assert.equal(absenceCopy({ state: "finished" }), "not extracted"));
check("finished + source unavailable → not extracted (unavailable)", () =>
  assert.equal(absenceCopy({ state: "finished", source: "unavailable" }),
    "not extracted (unavailable)"));
check("finished + successful extraction source stays bare not extracted", () =>
  assert.equal(absenceCopy({ state: "finished", source: "end_event" }),
    "not extracted"));

check("aggregate (finished, no source) → not extracted", () =>
  assert.equal(absenceCopy({ state: "finished", source: null }),
    "not extracted"));

check("unknown state falls through to not extracted", () =>
  assert.equal(absenceCopy({ state: "mystery" }), "not extracted"));

// CAKE-174: empty rate card must not steal waiting/failure cost honesty
check("empty card + running → still waiting phrase (native cost may arrive)", () =>
  assert.equal(
    costAbsenceCopy({ state: "running", emptyRateCard: true }),
    "available after the run ends"));
check("empty card + dispatched → waiting phrase", () =>
  assert.equal(
    costAbsenceCopy({ state: "dispatched", emptyRateCard: true }),
    "available after the run ends"));
check("empty card + finalizing → waiting phrase", () =>
  assert.equal(
    costAbsenceCopy({ state: "finalizing", emptyRateCard: true }),
    "available after the run ends"));
check("empty card + failed → failure phrase", () =>
  assert.equal(
    costAbsenceCopy({ state: "failed", emptyRateCard: true }),
    "not extracted (run failed)"));
check("empty card + orphaned → failure phrase", () =>
  assert.equal(
    costAbsenceCopy({ state: "orphaned", emptyRateCard: true }),
    "not extracted (run failed)"));
check("empty card + timed_out → failure phrase", () =>
  assert.equal(
    costAbsenceCopy({ state: "timed_out", emptyRateCard: true }),
    "not extracted (run failed)"));
check("empty card + finished → no-rate-card taxonomy", () =>
  assert.equal(
    costAbsenceCopy({ state: "finished", source: "end_event", emptyRateCard: true }),
    "no rate card — add rates under Cost inputs"));
check("empty card + finished unavailable → no-rate-card (rate reason wins)", () =>
  assert.equal(
    costAbsenceCopy({ state: "finished", source: "unavailable", emptyRateCard: true }),
    "no rate card — add rates under Cost inputs"));
check("priced card + finished → CAKE-171 not extracted", () =>
  assert.equal(
    costAbsenceCopy({ state: "finished", source: "end_event", emptyRateCard: false }),
    "not extracted"));

if (failed) {
  console.error(`token_cost_absence_helpers: ${failed} failed`);
  process.exit(1);
}
console.log("token_cost_absence_helpers: all passed");
