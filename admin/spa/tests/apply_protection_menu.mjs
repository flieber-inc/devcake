// CAKE-182: apply-protection on Repositories is operator-explicit only —
// MoreMenu + ConfirmDialog, never silent on connect/create. Pins consequence
// copy and the InstantZone / ImmediateBadge regime lightly.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const reposSrc = readFileSync(join(root, "src/pages/ReposPage.jsx"), "utf8");
const createIdx = reposSrc.indexOf("function CreateInternalRepoModal");
const exportIdx = reposSrc.indexOf("export default function ReposPage");
const createSrc = (createIdx >= 0 && exportIdx > createIdx)
  ? reposSrc.slice(createIdx, exportIdx)
  : "";

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

check("per-repo MoreMenu offers Apply branch protection…", () => {
  assert.match(reposSrc, /label:\s*"Apply branch protection…"/,
    "card MoreMenu must offer Apply branch protection…");
  assert.match(reposSrc, /APPLY_PROTECTION_DESC/,
    "per-repo item must carry honest consequence desc");
});

check("section MoreMenu offers bulk Apply protection…", () => {
  assert.match(reposSrc,
    /label:\s*"Apply protection to unprotected repos…"/,
    "section MoreMenu must offer bulk apply");
  assert.match(reposSrc, /BULK_APPLY_PROTECTION_DESC/,
    "bulk item must carry honest consequence desc");
});

check("apply is confirm-gated via ConfirmDialog (not window.confirm)", () => {
  assert.match(reposSrc, /requestApplyProtection/,
    "per-repo apply must go through a request helper");
  assert.match(reposSrc, /requestBulkApplyProtection/,
    "bulk apply must go through a request helper");
  assert.match(reposSrc, /Apply protection now/,
    "confirm label must name the forge write");
  assert.doesNotMatch(reposSrc, /window\.confirm/,
    "native confirm is banned");
});

check("apply hits operator POST endpoints only", () => {
  assert.match(reposSrc,
    /\/connections\/forge\/\$\{encodeURIComponent\(name\)\}\/apply-protection/,
    "single apply POST path");
  assert.match(reposSrc,
    /POST",\s*"\/connections\/forge\/apply-protection"/,
    "bulk apply POST path");
});

check("create-internal does not call apply-protection", () => {
  assert.ok(createSrc.length > 0, "CreateInternalRepoModal must exist");
  assert.doesNotMatch(createSrc, /apply-protection/,
    "Create repository must not auto-apply protection");
});

check("InstantZone / ImmediateBadge mark the instant regime", () => {
  assert.match(reposSrc, /InstantZone/,
    "apply result surfacing uses InstantZone");
  assert.match(reposSrc, /protection apply is instant/,
    "section ImmediateBadge discloses instant apply");
  assert.match(reposSrc,
    /applies immediately — does not wait for Save/,
    "InstantZone note matches DESIGN copy");
});

check("consequence copy names already-strict and 403", () => {
  assert.match(reposSrc, /already as strict|already-as-strict|already_as_strict/i,
    "copy must treat already_as_strict as a real outcome");
  assert.match(reposSrc, /403|admin permission/i,
    "copy must name permission failure honestly");
});

check("no silent apply on connect/test paths", () => {
  // Test connection stays on /test only.
  assert.match(reposSrc,
    /send\("POST",\s*`?\/connections\/forge\/\$\{[^}]+\}\/test`?\)/,
    "Test connection still posts /test");
  const testForgeBlock = reposSrc.match(
    /const testForge = async[\s\S]*?\n  \};/);
  assert.ok(testForgeBlock, "testForge helper present");
  assert.doesNotMatch(testForgeBlock[0], /apply-protection/,
    "testForge must not call apply-protection");
});

if (failed) {
  console.error(`apply_protection_menu.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("apply_protection_menu.mjs: ok");
