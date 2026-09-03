// Baker-dead must paint like a circuit breaker: critical, not dismissable,
// and the body names devcake up. Independent expected values are those literals.
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
  assert.match(hit.body, /devcake up/);
  assert.match(hit.body, /devcake up --foreground-baker/);
});

// ADR-0040: a nearly-spent request budget is a dismissable warning that names
// the bucket and carries the advisory text verbatim (poll-interval guidance).
check("a nearly-spent PMO request budget is a dismissable warning", () => {
  const alerts = deriveAlerts({
    pmo_budget_warnings: {
      "tracker.example/user:u1":
        "measured ~2900 requests/hour against 2500/hour shared by a, b; " +
        "raise the poll interval to at least 110 s",
    },
  });
  const hit = alerts.find((a) => a.id === "pmo-budget");
  assert.ok(hit, "pmo-budget alert missing");
  assert.equal(hit.severity, "warning");
  assert.equal(hit.dismissable, true);
  assert.match(hit.body, /tracker\.example\/user:u1: measured ~2900/);
  assert.match(hit.body, /at least 110 s/);
});

check("no budget warning when the payload carries none", () => {
  const alerts = deriveAlerts({ pmo_budget_warnings: {}, pmo_budget: {} });
  assert.equal(alerts.some((a) => a.id === "pmo-budget"), false);
});

check("an alive baker does not warn", () => {
  const alerts = deriveAlerts({
    bake_status: { baker_alive: true, baker_detail: "" },
  });
  assert.equal(alerts.some((a) => a.id === "baker-dead"), false);
});

// CAKE-134: launch-failure detail on /data must surface in the critical body.
check("dead baker with error detail includes the launch-failure cause", () => {
  const cause = "host baker died at launch: ModuleNotFoundError: No module named 'x'";
  const alerts = deriveAlerts({
    bake_status: {
      baker_alive: false,
      baker_detail: "host baker has not checked in",
      state: "error",
      detail: cause,
    },
  });
  const hit = alerts.find((a) => a.id === "baker-dead");
  assert.ok(hit, "baker-dead alert missing");
  assert.equal(hit.severity, "critical");
  assert.match(hit.body, /host baker has not checked in/);
  assert.match(hit.body, /ModuleNotFoundError: No module named 'x'/);
  assert.match(hit.body, /devcake up/);
  assert.match(hit.body, /devcake up --foreground-baker/);
  assert.equal(alerts.some((a) => a.id === "baker-error"), false);
});

check("dead baker without error detail stays generic", () => {
  const alerts = deriveAlerts({
    bake_status: {
      baker_alive: false,
      baker_detail: "host baker last checked in 41s ago",
    },
  });
  const hit = alerts.find((a) => a.id === "baker-dead");
  assert.ok(hit, "baker-dead alert missing");
  assert.match(hit.body, /host baker last checked in 41s ago/);
  assert.match(hit.body, /devcake up/);
  assert.doesNotMatch(hit.body, /host baker died at launch/);
  assert.doesNotMatch(hit.body, /ModuleNotFoundError/);
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

// CAKE-30 / CAKE-179: forge_protection is advisory (docs/14 §8) — never a hard
// gate — but the banner names the deployment-requirement / Dev-merge hazard.
check("unprotected default branch is a dismissable advisory, not a dispatch gate", () => {
  const alerts = deriveAlerts({
    forge_protection: { main: { protected: false } },
  });
  const hit = alerts.find((a) => a.id === "forge:main");
  assert.ok(hit, "forge:main alert missing");
  assert.equal(hit.dismissable, true);
  assert.equal(hit.severity, "critical");
  assert.match(hit.title, /unprotected/i);
  assert.match(hit.body, /does not block dispatch/i);
  assert.match(hit.body, /deployment requirement/i);
  assert.match(hit.body, /required status checks/i);
  // Required reviews (≥1 approval) are part of the deployment-requirement shape —
  // checks alone do not stop a patient Dev self-merge after green CI (CAKE-179).
  assert.match(hit.body, /1 approval|≥1 approval|reviews/i);
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

// CAKE-161: claims-queue alert points at Policies, not the old Limits label
check("claims-queue-capped alert names Policies → Counting budgets", () => {
  const alerts = deriveAlerts({
    claims_queue_capped: ["notebook_a"],
  });
  const hit = alerts.find((a) => a.id === "claims-queue-capped:notebook_a");
  assert.ok(hit, "claims-queue-capped alert missing");
  assert.match(hit.body, /Policies → Counting budgets/);
  assert.doesNotMatch(hit.body, /Limits →/);
});

if (failed) {
  console.error(`alerts.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("alerts.mjs: all checks passed");
