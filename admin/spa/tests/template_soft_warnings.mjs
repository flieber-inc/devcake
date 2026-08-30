// Soft save-time warnings for the Prompts template manager (CAKE-166).
// Mirrors templates.template_warnings wording; does not block Save.
import assert from "node:assert/strict";
import { templateSoftWarnings } from "../src/lib/templateSoftWarnings.js";

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

check("clean ONBOARD body with depth 1 → no soft warnings", () => {
  assert.deepEqual(
    templateSoftWarnings({
      missionType: "ONBOARD",
      templateName: "Development",
      text: "Hello {decomposition_rule}",
      maxDecompositionDepth: 1,
    }),
    [],
  );
});

check("missing {decomposition_rule} when depth ≠ 1 → soft warning", () => {
  const warns = templateSoftWarnings({
    missionType: "ONBOARD",
    templateName: "Custom",
    text: "No placeholder here",
    maxDecompositionDepth: 3,
  });
  assert.equal(warns.length, 1);
  assert.match(warns[0], /\{decomposition_rule\}/);
  assert.match(warns[0], /decomposition depth \(3\)/);
  assert.match(warns[0], /Custom/);
});

check("missing {decomposition_rule} when depth is 1 → silent", () => {
  assert.deepEqual(
    templateSoftWarnings({
      missionType: "ONBOARD",
      templateName: "Custom",
      text: "No placeholder here",
      maxDecompositionDepth: 1,
    }),
    [],
  );
});

check("EXECUTE ignores decomposition_rule check", () => {
  assert.deepEqual(
    templateSoftWarnings({
      missionType: "EXECUTE",
      templateName: "Custom",
      text: "plain body",
      maxDecompositionDepth: 5,
    }),
    [],
  );
});

check("executed_trivially staleness on ONBOARD", () => {
  const warns = templateSoftWarnings({
    missionType: "ONBOARD",
    templateName: "Old",
    text: "outcome executed_trivially is fine",
    maxDecompositionDepth: 1,
  });
  assert.equal(warns.length, 1);
  assert.match(warns[0], /executed_trivially/);
  assert.match(warns[0], /DEVCAKE-SKIP/);
});

check("Work ONLY inside staleness on any mission type", () => {
  const warns = templateSoftWarnings({
    missionType: "PLAN",
    templateName: "Stale",
    text: 'Work ONLY inside /workspace/repo/foo',
    maxDecompositionDepth: 1,
  });
  assert.equal(warns.length, 1);
  assert.match(warns[0], /Work ONLY inside/);
  assert.match(warns[0], /result\.json/);
});

if (failed) {
  console.error(`template_soft_warnings.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("template_soft_warnings.mjs: all checks passed");
