// Pure-node pins for the Runs token/cost absence phrasebook (CAKE-171).
// Literal expected strings only — never re-derive the helper's branches.
import assert from "node:assert/strict";
import { absenceCopy } from "../src/lib/tokenCostAbsence.js";

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

if (failed) {
  console.error(`token_cost_absence_helpers: ${failed} failed`);
  process.exit(1);
}
console.log("token_cost_absence_helpers: all passed");
