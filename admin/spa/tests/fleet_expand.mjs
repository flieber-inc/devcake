// Hermetic checks for fleet card expand-on-load seeding (CAKE-89).
// ≤3 cards expand once the draft length is known; 4+ stay collapsed;
// re-entry after the first seed must not fight operator collapses.
import assert from "node:assert/strict";
import { fleetSeedIndexes } from "../src/lib/fleetExpand.js";

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

check("unknown / empty length waits (null) until draft loads", () => {
  assert.equal(fleetSeedIndexes(0, false), null);
});

check("1–3 cards seed every index expanded", () => {
  assert.deepEqual(fleetSeedIndexes(1, false), [0]);
  assert.deepEqual(fleetSeedIndexes(2, false), [0, 1]);
  assert.deepEqual(fleetSeedIndexes(3, false), [0, 1, 2]);
});

check("4+ cards seed collapsed (empty list)", () => {
  assert.deepEqual(fleetSeedIndexes(4, false), []);
  assert.deepEqual(fleetSeedIndexes(12, false), []);
});

check("already seeded never returns indexes again", () => {
  assert.equal(fleetSeedIndexes(2, true), null);
  assert.equal(fleetSeedIndexes(8, true), null);
  assert.equal(fleetSeedIndexes(0, true), null);
});

if (failed) {
  console.error(`fleet_expand.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("fleet_expand.mjs: all checks passed");
