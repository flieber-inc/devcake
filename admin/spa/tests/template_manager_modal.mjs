// Hermetic contracts for Manage-templates Duplicate-to-edit + Modal scroll
// shell (CAKE-170). Source-level pins — browser proof remains Always Works™.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const promptsSrc = readFileSync(
  join(root, "src/components/PromptsSection.jsx"),
  "utf8",
);
const modalSrc = readFileSync(
  join(root, "src/components/Modal.jsx"),
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

check("Modal surface is viewport-bounded with internal scroll", () => {
  // Chokepoint for tall dialogs (Manage templates, Skills View). Confirm /
  // Prompt use Overlay directly and must not be forced into empty flex-fill.
  const modalFn = modalSrc.match(
    /export function Modal\([\s\S]*?\n\}/,
  );
  assert.ok(modalFn, "Modal export must exist");
  assert.match(modalFn[0], /max-h-\[85vh\]/,
    "Modal must cap height near 85vh (SaveReview precedent)");
  assert.match(modalFn[0], /overflow-y-auto/,
    "Modal must scroll tall content internally");
});

check("built-in templates expose Duplicate to edit via PromptDialog", () => {
  assert.match(promptsSrc, /PromptDialog/,
    "TemplateManagerModal must use PromptDialog (no window.prompt)");
  assert.match(promptsSrc, /Duplicate to edit/,
    "built-ins need a visible Duplicate to edit affordance");
  assert.match(promptsSrc, /data-testid="duplicate-to-edit"/,
    "Duplicate control needs a stable test id");
});

check("Duplicate persists an operator copy then selects it in Source", () => {
  assert.match(promptsSrc, /duplicatePrompt|onConfirm=\{[^}]*duplicate|confirmDuplicate/i,
    "Duplicate confirm handler must exist");
  assert.match(promptsSrc, /data-testid="duplicate-active-hint"/,
    "post-duplicate active-selection hint needs a stable test id");
  assert.match(promptsSrc,
    /setViewMode\(\s*["']source["']\s*\)/,
    "after Duplicate the modal must land in Source mode");
});

check("read-only built-in note points at Duplicate to edit", () => {
  assert.match(promptsSrc, /Duplicate to edit/,
    "copy must name the button");
  assert.doesNotMatch(promptsSrc,
    /create a copy to customize\.?\s*<\/p>/,
    "dead 'create a copy' instruction without a button is the bug");
});

if (failed) {
  console.error(`template_manager_modal.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("template_manager_modal.mjs: all checks passed");
