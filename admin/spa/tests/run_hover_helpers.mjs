// Pure-node checks for synthetic-key Runs hover copy (CAKE-167).
import assert from "node:assert/strict";
import { runHoverDetail } from "../src/lib/runHover.js";

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

check("ordinary mission keys stay empty (key + PMO link only)", () => {
  assert.equal(runHoverDetail({ mission_key: "CAKE-167", pmo_ref: "linear" }), "");
  assert.equal(runHoverDetail({ mission_key: "ENG-1" }), "");
  assert.equal(runHoverDetail(null), "");
  assert.equal(runHoverDetail({}), "");
});

check("TEAM: PMO + relations duty + outcome summary", () => {
  assert.equal(
    runHoverDetail({
      mission_key: "TEAM",
      pmo_ref: "linear",
      steward_duty: "",
      outcome_summary: "2 relations proposed (1 rejected)",
    }),
    "linear · relations · 2 relations proposed (1 rejected)",
  );
});

check("TEAM: discovery duty; empty outcome still shows PMO + duty", () => {
  assert.equal(
    runHoverDetail({
      mission_key: "TEAM",
      pmo_ref: "gitea",
      steward_duty: "discovery",
      outcome_summary: "",
    }),
    "gitea · discovery",
  );
});

check("HELLO / OAUTH: honest label + pmo_ref when present", () => {
  assert.equal(
    runHoverDetail({ mission_key: "HELLO", pmo_ref: "sys" }),
    "hello smoke · sys",
  );
  assert.equal(
    runHoverDetail({ mission_key: "OAUTH", pmo_ref: "sys" }),
    "OAuth probe · sys",
  );
  assert.equal(runHoverDetail({ mission_key: "HELLO" }), "hello smoke");
});

if (failed) {
  console.error(`\n${failed} runHover helper check(s) failed`);
  process.exit(1);
}
console.log("\nall runHover helper checks passed");
