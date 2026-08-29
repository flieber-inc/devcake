// CAKE-152: repo rename must not cascade into draft Dev Type memory_repos.
// DraftChrome saves dirty Dev Types BEFORE PUT /config. Rewriting
// `devTypes.*.memory_repos` on rename dirties them; a successful Dev Type
// PUT followed by a failed config rename leaves disk citing the new name
// while AppConfig still has the old one (server dirty_dts cannot heal it).
// PMO list cascades in the same config PUT remain necessary; removal may
// still deselect Dev Type citations via filter.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
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

check("ReposPage rename does not rewrite draft Dev Type memory_repos", () => {
  assert.doesNotMatch(
    reposSrc,
    /setField\(`devTypes\.\$\{nm\}\.memory_repos`,\s*\n?\s*dt\.memory_repos\.map/,
    "ReposPage must not cascade repo rename into draft Dev Type memory_repos — "
      + "DraftChrome saves Dev Types before PUT /config; rely on server rewrite after successful reload");
});

check("ReposPage rename still cascades PMO repo citations in the same config draft", () => {
  assert.match(
    reposSrc,
    /for \(const field of \["repos", "reference_repos", "memory_repos"\]\)/,
    "PMO citation cascades on rename must remain (inline draft validation)");
});

if (failed) {
  console.error(`repo_rename_cascade.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("repo_rename_cascade.mjs: all checks passed");
