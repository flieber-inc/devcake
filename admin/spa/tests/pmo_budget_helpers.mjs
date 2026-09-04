// Hermetic checks for the PMO card's request-budget readout (ADR-0040
// visibility): bucket lookup by instance, the one-line summary, hover
// details, and the tone ladder. Independent expected values are literals.
import assert from "node:assert/strict";
import {
  budgetDetails,
  budgetForInstance,
  budgetLine,
  budgetShare,
  budgetTone,
} from "../src/lib/budget.js";

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

const health = {
  pmo_budget: {
    "tracker.example/user:u1": {
      label: "tracker.example/user:u1",
      instances: ["a", "b", "c"],
      limit: 2500,
      remaining: 2471,
      reset_at: 1788541871,
      blocked_until: null,
      waits: 0,
      limited_last_hour: 0,
      foreign_spend: 0,
      demand_per_hour: { a: 61, b: 119, c: 1541 },
    },
    "forge.example/key-070231": {
      label: "forge.example/key-070231",
      instances: ["board"],
      limit: null,
      remaining: null,
      demand_per_hour: { board: 250 },
      limited_last_hour: 0,
    },
  },
};

check("finds the bucket an instance spends from and sums the shared demand", () => {
  const b = budgetForInstance(health, "c");
  assert.equal(b.perHour, 1541);
  assert.equal(b.totalPerHour, 1721);
  assert.deepEqual(b.sharedWith, ["a", "b"]);
  assert.equal(budgetShare(b), 69);
  assert.equal(budgetForInstance(health, "nope"), null);
  assert.equal(budgetForInstance({}, "a"), null);
});

check("the card line states demand, limit, share and the instances sharing it", () => {
  assert.equal(
    budgetLine(budgetForInstance(health, "c")),
    "about 1,541 requests/hour of 2,500 (69% of the credential's hour, " +
      "1,721/hour together with a, b)",
  );
  assert.equal(
    budgetLine(budgetForInstance(health, "board")),
    "about 250 requests/hour — the tracker publishes no limit",
  );
  const zero = { pmo_budget: { x: { instances: ["a"], limit: 0, demand_per_hour: { a: 5 } } } };
  assert.equal(budgetLine(budgetForInstance(zero, "a")),
    "about 5 requests/hour — the tracker publishes no limit");
});

check("an unmeasured instance says so instead of showing zero", () => {
  const h = { pmo_budget: { x: { instances: ["a"], limit: 100, demand_per_hour: { a: null } } } };
  assert.match(budgetLine(budgetForInstance(h, "a")), /measuring/);
});

check("an idle connection on a limited credential reads 0 %, never null", () => {
  const h = { pmo_budget: { x: { instances: ["a"], limit: 2500, demand_per_hour: { a: 0 } } } };
  const b = budgetForInstance(h, "a");
  assert.equal(budgetShare(b), 0);
  assert.equal(budgetLine(b), "about 0 requests/hour of 2,500 (0% of the credential's hour)");
  assert.equal(budgetTone(b), "neutral");
});

check("the estimate wins over the last observed remaining", () => {
  const h = { pmo_budget: { x: { instances: ["a"], limit: 2500, remaining: 2471,
    remaining_estimate: 2400, demand_per_hour: { a: 10 } } } };
  assert.match(budgetDetails(budgetForInstance(h, "a")), /remaining: 2,400/);
});

check("hover details carry remaining, refill time and rejections", () => {
  const d = budgetDetails(budgetForInstance(health, "a"));
  assert.match(d, /credential: tracker\.example\/user:u1/);
  assert.match(d, /remaining: 2,471/);
  assert.match(d, /refills by: \d\d:\d\d UTC/);
  assert.match(d, /rejected by the tracker in the last hour: 0/);
  assert.doesNotMatch(d, /paused/);
});

check("tone: neutral under 80 %, warning at 80 %, critical when rejected or paused", () => {
  assert.equal(budgetTone(budgetForInstance(health, "a")), "neutral");
  const hot = JSON.parse(JSON.stringify(health));
  hot.pmo_budget["tracker.example/user:u1"].demand_per_hour = { a: 2000, b: 100, c: 0 };
  assert.equal(budgetTone(budgetForInstance(hot, "a")), "warning");
  hot.pmo_budget["tracker.example/user:u1"].limited_last_hour = 3;
  assert.equal(budgetTone(budgetForInstance(hot, "a")), "critical");
  const paused = JSON.parse(JSON.stringify(health));
  paused.pmo_budget["tracker.example/user:u1"].blocked_until = 1788541900;
  assert.equal(budgetTone(budgetForInstance(paused, "b")), "critical");
  assert.match(budgetDetails(budgetForInstance(paused, "b")), /paused by the tracker until/);
  assert.equal(budgetTone(null), "neutral");
});

if (failed) {
  console.log(`${failed} check(s) failed`);
  process.exit(1);
}
console.log("pmo_budget_helpers.mjs: all checks passed");
