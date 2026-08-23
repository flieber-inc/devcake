// Baker-dead must paint like a circuit breaker: critical, not dismissable,
// and the body names ./up.sh. Independent expected values are those literals.
import assert from "node:assert/strict";
import deriveAlerts from "../src/lib/alerts.js";

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

check("a dead baker is a critical non-dismissable alert", () => {
  const alerts = deriveAlerts({
    bake_status: {
      baker_alive: false,
      baker_detail: "host baker last checked in 41s ago",
    },
  });
  const hit = alerts.find((a) => a.id === "baker-dead");
  assert.ok(hit, "baker-dead alert missing");
  assert.equal(hit.severity, "critical");
  assert.equal(hit.dismissable, undefined);
  assert.match(hit.title, /baker/i);
  assert.match(hit.body, /\.\/up\.sh/);
  assert.match(hit.body, /\.\/up\.sh --foreground-baker/);
});

check("an alive baker does not warn", () => {
  const alerts = deriveAlerts({
    bake_status: { baker_alive: true, baker_detail: "" },
  });
  assert.equal(alerts.some((a) => a.id === "baker-dead"), false);
});

check("circuit breaker stays critical red in the same list", () => {
  const alerts = deriveAlerts({
    bake_status: { baker_alive: false, baker_detail: "gone" },
    circuit_breakers: { "implementer": "DEV_AUTH" },
  });
  const kinds = alerts.filter((a) => a.severity === "critical").map((a) => a.id);
  assert.ok(kinds.includes("baker-dead"));
  assert.ok(kinds.includes("breakers"));
});

// CAKE-30: forge_protection is advisory (docs/14 §8) — never a hard gate.
check("unprotected default branch is a dismissable advisory, not a dispatch gate", () => {
  const alerts = deriveAlerts({
    forge_protection: { main: { protected: false } },
  });
  const hit = alerts.find((a) => a.id === "forge:main");
  assert.ok(hit, "forge:main alert missing");
  assert.equal(hit.dismissable, true);
  assert.match(hit.title, /unprotected/i);
  assert.match(hit.body, /does not block dispatch/i);
  assert.match(hit.body, /branch protection/i);
});

// CAKE-89: unused-repos alert names memory + skill-source separation
check("unused-repos alert includes memory and excludes skill-source as selection", () => {
  const alerts = deriveAlerts({
    unused_repos: { count: 2, names: ["orphan1", "orphan2"], configured: 5 },
  });
  const hit = alerts.find((a) => a.id === "unused-repos");
  assert.ok(hit, "unused-repos alert missing");
  assert.match(hit.body, /memory/i);
  assert.match(hit.body, /skill sources/i);
  assert.doesNotMatch(hit.body, /work or reference repos on no PMO/);
});

if (failed) {
  console.error(`alerts.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("alerts.mjs: all checks passed");
