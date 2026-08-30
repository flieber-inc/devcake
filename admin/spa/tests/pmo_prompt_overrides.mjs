// Hermetic checks for per-PMO prompt-override expand/collapse (CAKE-160).
// Presence of a non-empty Mission-Type key = override; empty/missing = inherit.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  pmoHasPromptOverride,
  pmoOverrideExpandIndexes,
  pmoOverrideSummaryText,
} from "../src/lib/pmoPromptOverrides.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const promptsSrc = readFileSync(
  join(root, "src/components/PromptsSection.jsx"),
  "utf8",
);

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

check("empty / missing map inherits (no override)", () => {
  assert.equal(pmoHasPromptOverride({}), false);
  assert.equal(pmoHasPromptOverride({ active_prompt_templates: {} }), false);
  assert.equal(pmoHasPromptOverride({ active_prompt_templates: { ONBOARD: "" } }), false);
});

check("any non-empty Mission-Type key counts as override", () => {
  assert.equal(
    pmoHasPromptOverride({ active_prompt_templates: { PLAN: "Customer Success" } }),
    true,
  );
  assert.equal(
    pmoHasPromptOverride({
      active_prompt_templates: { ONBOARD: "", EXECUTE: "Development" },
    }),
    true,
  );
});

check("expand indexes list only boards with real overrides", () => {
  const pmos = [
    { name: "a", active_prompt_templates: {} },
    { name: "b", active_prompt_templates: { REVIEW: "Customer Success" } },
    { name: "c" },
    { name: "d", active_prompt_templates: { ONBOARD: "" } },
  ];
  assert.deepEqual(pmoOverrideExpandIndexes(pmos), [1]);
});

check("fully-inheriting summary names all four", () => {
  assert.match(pmoOverrideSummaryText({}), /all four inherit global/i);
  assert.match(pmoOverrideSummaryText({ ONBOARD: "" }), /all four inherit global/i);
});

check("partial override summary lists the overridden types", () => {
  const text = pmoOverrideSummaryText({ PLAN: "X", REVIEW: "Y" });
  assert.match(text, /PLAN/);
  assert.match(text, /REVIEW/);
  assert.ok(!/ONBOARD/.test(text));
  assert.ok(!/EXECUTE/.test(text));
});

check("PromptsSection collapses overrides behind a summary testid", () => {
  assert.match(promptsSrc, /pmoPromptOverrides/,
    "PromptsSection must use the override helpers");
  assert.match(promptsSrc, /data-testid="pmo-prompt-override-summary"/,
    "collapsed override rows need a stable testid");
  assert.match(promptsSrc, /pmoHasPromptOverride|pmoOverrideExpandIndexes/,
    "expand seed must follow hasOverride");
});

check("setPmoOverride draft contract is unchanged", () => {
  assert.match(promptsSrc,
    /setField\(`cfg\.pmos\.\$\{i\}\.active_prompt_templates`,\s*rest\)/,
    "empty value must delete the Mission-Type key (inherit)");
  assert.match(promptsSrc,
    /setField\(`cfg\.pmos\.\$\{i\}\.active_prompt_templates\.\$\{mt\}`,\s*name\)/,
    "non-empty value must set the override key");
});

check("Workflow switcher is visually subordinated (no accent card)", () => {
  assert.doesNotMatch(promptsSrc,
    /border-accent-200 bg-accent-50/,
    "Workflow switcher must not use the accent-tinted card chrome");
  assert.match(promptsSrc, /Workflow switcher/,
    "Workflow switcher control must remain");
});

if (failed) {
  console.error(`pmo_prompt_overrides.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("pmo_prompt_overrides.mjs: all checks passed");
