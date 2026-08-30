// Hermetic checks for Skills View editable-source / external lock (CAKE-166).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const skillsSrc = readFileSync(
  join(root, "src/components/SkillsSection.jsx"),
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

check("ViewSkillDialog accepts skill_sources for external URL resolution", () => {
  assert.match(skillsSrc,
    /function ViewSkillDialog\(\{\s*name,\s*onClose,\s*skillSources/,
    "ViewSkillDialog must take skillSources (or skill_sources) for external links");
});

check("external skills show a read-only notice", () => {
  assert.match(skillsSrc,
    /external repository|changes must be made in the external/i,
    "external View must state edits belong in the external repository");
});

check("store skills can save via POST /skills overwrite", () => {
  assert.match(skillsSrc, /POST["'],\s*["']\/skills/,
    "store View edit saves through POST /skills");
  assert.match(skillsSrc, /overwrite:\s*true/,
    "store View edit must overwrite explicitly");
});

check("SkillsSection is passed cfg.skill_sources from Fleet", () => {
  const fleetSrc = readFileSync(
    join(root, "src/pages/FleetPage.jsx"),
    "utf8",
  );
  assert.match(fleetSrc,
    /<SkillsSection[^>]*skillSources=\{/,
    "Fleet must pass skillSources into SkillsSection");
});

if (failed) {
  console.error(`skills_view_edit.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("skills_view_edit.mjs: all checks passed");
