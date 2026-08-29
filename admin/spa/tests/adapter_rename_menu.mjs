// CAKE-156: PMO / repository adapter rename must be discoverable via a
// per-card MoreMenu → PromptDialog that applies through the draft setField
// path (not Dev Types' immediate POST /dev-types/{name}/rename).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const pmoSrc = readFileSync(join(root, "src/components/PmoSection.jsx"), "utf8");
const reposSrc = readFileSync(join(root, "src/pages/ReposPage.jsx"), "utf8");

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

check("PmoSection exposes Rename adapter via PromptDialog", () => {
  assert.match(pmoSrc, /PromptDialog/, "PmoSection must import/use PromptDialog");
  assert.match(pmoSrc, /label:\s*"Rename adapter"/, "PmoSection MoreMenu must offer Rename adapter");
  assert.match(pmoSrc, /applyPmoNameChange|applyNameChange/,
    "PmoSection must factor rename apply for Input + PromptDialog");
});

check("ReposPage exposes Rename adapter via PromptDialog", () => {
  assert.match(reposSrc, /PromptDialog/, "ReposPage must import/use PromptDialog");
  assert.match(reposSrc, /label:\s*"Rename adapter"/, "ReposPage MoreMenu must offer Rename adapter");
  assert.match(reposSrc, /applyRepoNameChange|applyNameChange/,
    "ReposPage must factor rename apply for Input + PromptDialog");
});

check("adapter rename hints do not claim immediate rename", () => {
  assert.doesNotMatch(pmoSrc, /Renames immediately/,
    "PMO adapter PromptDialog must not copy Dev Types' immediate-rename hint");
  assert.doesNotMatch(reposSrc, /Renames immediately/,
    "Repo adapter PromptDialog must not copy Dev Types' immediate-rename hint");
  assert.match(pmoSrc, /applies on Save/i, "PMO rename copy must say applies on Save");
  assert.match(reposSrc, /applies on Save/i, "Repo rename copy must say applies on Save");
});

check("adapter rename does not POST a rename endpoint", () => {
  assert.doesNotMatch(pmoSrc, /POST["'].*\/rename/,
    "PMO rename must stay draft setField — no POST rename");
  assert.doesNotMatch(reposSrc, /POST["'].*\/rename/,
    "Repo rename must stay draft setField — no POST rename");
});

check("ReposPage rename still avoids draft Dev Type memory_repos cascade", () => {
  assert.doesNotMatch(
    reposSrc,
    /setField\(`devTypes\.\$\{nm\}\.memory_repos`,\s*\n?\s*dt\.memory_repos\.map/,
    "ReposPage must not cascade repo rename into draft Dev Type memory_repos");
  assert.match(
    reposSrc,
    /for \(const field of \["repos", "reference_repos", "memory_repos"\]\)/,
    "PMO citation cascades on rename must remain");
});

if (failed) {
  console.error(`adapter_rename_menu.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("adapter_rename_menu.mjs: all checks passed");
