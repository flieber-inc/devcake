// Hermetic checks for Devs-card busy derivation (CAKE-125).
// Busy coloring must use an active-only runs list — never the recent-25
// page, which can omit still-running runs older than the page window.
import assert from "node:assert/strict";
import { devTypeState, runsForDevActivity } from "../src/lib/services.js";

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

const dt = { name: "T", credentials_ready: true };
const health = { circuit_breakers: {} };
const olderActive = {
  run_id: "old-active",
  dev_type: "T",
  state: "running",
};
const recentTerminals = Array.from({ length: 25 }, (_, i) => ({
  run_id: `new-term-${i}`,
  dev_type: "T",
  state: "finished",
}));

check("recent-25 terminal window alone reports ok (loses older active)", () => {
  assert.equal(devTypeState(dt, health, recentTerminals).state, "ok");
});

check("active-only list via runsForDevActivity reports running", () => {
  assert.equal(
    devTypeState(dt, health, runsForDevActivity([olderActive])).state,
    "running",
  );
});

check("empty active-only list is ok even when a recent page has noise", () => {
  // Contract: helper takes ONLY the active-only poll — it must not fall
  // back to or merge a recent page (that is Overview wiring's job).
  assert.equal(devTypeState(dt, health, runsForDevActivity([])).state, "ok");
  assert.equal(
    devTypeState(dt, health, runsForDevActivity(undefined)).state,
    "ok",
  );
});

check("broken / credentials paths still win over busy", () => {
  assert.equal(
    devTypeState(dt, { circuit_breakers: { T: "latched" } },
      runsForDevActivity([olderActive])).state,
    "broken",
  );
  assert.equal(
    devTypeState({ name: "T", credentials_ready: false }, health,
      runsForDevActivity([olderActive])).state,
    "broken",
  );
});

if (failed) {
  console.error(`services_helpers.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("services_helpers.mjs: all checks passed");
