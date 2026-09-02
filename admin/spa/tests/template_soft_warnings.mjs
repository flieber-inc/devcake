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

check("gated board + planning-stage template without {plan_approval_rule} → soft warning", () => {
  for (const mt of ["ONBOARD", "PLAN", "EXECUTE"]) {
    const warns = templateSoftWarnings({
      missionType: mt,
      templateName: "Custom",
      text: "No placeholder here {decomposition_rule}",
      maxDecompositionDepth: 1,
      planApproval: true,
    });
    assert.equal(warns.length, 1, mt);
    assert.match(warns[0], /\{plan_approval_rule\}/);
    assert.match(warns[0], /Custom/);
  }
});

check("no gated board, or REVIEW, or placeholder present → quiet", () => {
  assert.deepEqual(templateSoftWarnings({
    missionType: "PLAN", templateName: "Custom", text: "x",
    maxDecompositionDepth: 1, planApproval: false,
  }), []);
  assert.deepEqual(templateSoftWarnings({
    missionType: "REVIEW", templateName: "Custom", text: "x",
    maxDecompositionDepth: 1, planApproval: true,
  }), []);
  assert.deepEqual(templateSoftWarnings({
    missionType: "PLAN", templateName: "Custom",
    text: "x {plan_approval_rule}", maxDecompositionDepth: 1,
    planApproval: true,
  }), []);
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

// Modal contract: soft warnings set on Save must survive Create→setCreating(false).
// REVIEW reject CAKE-166: a useEffect that cleared softWarns listed `creating` (and
// entry?.name) as deps, so Create flashed then wiped the amber banner.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const promptsSrc = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "../src/components/PromptsSection.jsx"),
  "utf8",
);

check("unconditional softWarns clear effect must not key on creating/entry?.name", () => {
  // REVIEW reject: save() setSoftWarns(soft) then setCreating(false); an effect
  // that unconditionally setSoftWarns([]) on [entry?.name, creating, …] wiped
  // the amber banner on Create. Selection-sync may clear only after proving the
  // current selection left the list — that is not this bug.
  assert.doesNotMatch(
    promptsSrc,
    /setSoftWarns\(\[\]\);\s*\n\s*setErr\(null\);\s*\n\s*\}, \[entry\?\.name,\s*creating/,
    "must not unconditionally clear softWarns on [entry?.name, creating, …]",
  );
  const effectRe = /useEffect\(\(\) => \{([\s\S]*?)\}, \[([^\]]*)\]/g;
  let m;
  while ((m = effectRe.exec(promptsSrc))) {
    const body = m[1];
    const deps = m[2];
    if (!/setSoftWarns\(\[\]\)/.test(body)) continue;
    const unconditional = /^\s*setSoftWarns\(\[\]\)/.test(body);
    if (!unconditional) continue;
    assert.doesNotMatch(deps, /\bcreating\b/,
      "unconditional softWarns clear must not depend on creating");
    assert.doesNotMatch(deps, /entry\?\.name/,
      "unconditional softWarns clear must not depend on entry?.name");
  }
});

check("Create/Save still surfaces soft warnings in the modal", () => {
  assert.match(promptsSrc, /data-testid="template-soft-warnings"/,
    "soft-warn banner testid must remain");
  assert.match(promptsSrc, /setSoftWarns\(soft\)/,
    "save() must still assign soft warnings from templateSoftWarnings");
  assert.match(promptsSrc, /setCreating\(false\)/,
    "successful Create still exits creating mode");
});

if (failed) {
  console.error(`template_soft_warnings.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("template_soft_warnings.mjs: all checks passed");
