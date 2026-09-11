// Hermetic checks for the Dev Types panel's host-baker readouts: the last
// prune's sentence (with its time) and the reason the baker cannot act.
import assert from "node:assert/strict";
import { bakerBlockedReason, describeNestedEngine, describePrune } from "../src/lib/bakeStatus.js";

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

check("no prune yet → empty", () => {
  assert.equal(describePrune(null), "");
  assert.equal(describePrune({ state: "ready" }), "");
});

check("removed images with the time", () => {
  assert.equal(
    describePrune({ prune: { removed: ["devcake/dev-x:v1-1.0", "devcake/dev-y:v1"],
      kept: 3, detail: "", receipts_dropped: ["x@1.0"], at: "2026-09-08T18:07:12+00:00" } }),
    "Last prune (18:07 UTC): removed 2 image(s), 1 receipt(s) dropped.");
});

check("nothing to prune and a refusal keep their detail", () => {
  assert.equal(describePrune({ prune: { removed: [], kept: 4, detail: "nothing to prune", at: "2026-09-08T18:07:12Z" } }),
    "Last prune (18:07 UTC): nothing to prune");
  assert.equal(describePrune({ prune: { removed: [], kept: 0, detail: "refused: no keep-set order this tick" } }),
    "Last prune: refused: no keep-set order this tick");
});

check("blocked reasons", () => {
  assert.equal(bakerBlockedReason({ state: "ready", baker_alive: true }), "");
  assert.equal(bakerBlockedReason({ state: "baking", baker_alive: true }), "");
  assert.equal(bakerBlockedReason({ state: "ready", baker_alive: false }), "The host baker is not running.");
  assert.equal(bakerBlockedReason({ state: "error", detail: "the checkout has moved since the app was baked; run devcake up --bake" }),
    "The host baker is not acting (error): the checkout has moved since the app was baked; run devcake up --bake");
  assert.equal(bakerBlockedReason(null), "");
});

check("nested engine: no receipt → empty; green and red sentences", () => {
  assert.equal(describeNestedEngine(null), "");
  assert.equal(describeNestedEngine({ state: "ready" }), "");
  assert.equal(
    describeNestedEngine({ nested: { rig_ok: true, measured_at: "20260910T235021Z", first_red: "" } }),
    "Nested engine: Devs can run containers on this host (measured 2026-09-10 23:50 UTC).");
  assert.equal(
    describeNestedEngine({ nested: { rig_ok: false, measured_at: "20260910T220000Z",
      first_red: "the engine cannot create a user namespace (uid_map: EPERM)" } }),
    "Nested engine unavailable: the engine cannot create a user namespace (uid_map: EPERM) (measured 2026-09-10 22:00 UTC). Devs are told so in their prompt; runs still launch.");
  assert.equal(
    describeNestedEngine({ nested: { rig_ok: true, compose_ok: true, measured_at: "20260910T235021Z" } }),
    "Nested engine: Devs can run containers on this host, compose included (measured 2026-09-10 23:50 UTC).");
  assert.match(
    describeNestedEngine({ nested: { rig_ok: true, compose_ok: false } }),
    /docker compose is not working/);
  assert.match(
    describeNestedEngine({ nested: { rig_ok: false, runs_launch: false, first_red: "the Docker host no longer applies the AppArmor profile the stack names, so no Dev container can start until it is loaded again" } }),
    /Every run fails at container create until the profile is loaded again or devcake up is run\./);
  assert.equal(
    describeNestedEngine({ nested: { rig_ok: false } }),
    "Nested engine unavailable: see the probe receipt. Devs are told so in their prompt; runs still launch.");
});

if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}
console.log("bake_status helpers: all checks passed");
