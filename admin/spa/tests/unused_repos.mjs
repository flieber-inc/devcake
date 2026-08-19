// Hermetic checks for the unused-repo predicate (CAKE-89) — must match
// app/devcake/api/health.py::unused_repo_names: PMO work/reference/memory
// + Dev Type memory_repos only; skill-source prefixes are NOT repo cards.
import assert from "node:assert/strict";
import { unusedRepoNames } from "../src/lib/unusedRepos.js";

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

check("orphans with no PMO selection are unused", () => {
  const names = unusedRepoNames({
    repos: [{ name: "work1" }, { name: "refdocs" }, { name: "orphan1" }],
    pmos: [{ repos: ["work1"], reference_repos: ["refdocs"], memory_repos: [] }],
  }, {});
  assert.deepEqual(names, ["orphan1"]);
});

check("PMO memory_repos count as selected (not unused)", () => {
  const names = unusedRepoNames({
    repos: [{ name: "notebook" }, { name: "orphan" }],
    pmos: [{ repos: [], reference_repos: [], memory_repos: ["notebook"] }],
  }, {});
  assert.deepEqual(names, ["orphan"]);
});

check("Dev Type memory_repos count as selected", () => {
  const names = unusedRepoNames({
    repos: [{ name: "dtmem" }, { name: "orphan" }],
    pmos: [],
  }, { curator: { memory_repos: ["dtmem"], skills: [] } });
  assert.deepEqual(names, ["orphan"]);
});

check("skill-source prefixes do NOT protect a repo card", () => {
  // Backend has no skills branch: `<source>/<skill>` names a skill_source,
  // never a repo card. A repo whose name equals a skill prefix stays unused.
  const names = unusedRepoNames({
    repos: [{ name: "myskills" }, { name: "orphan" }],
    pmos: [],
  }, {
    implementer: { memory_repos: [], skills: ["myskills/foo", "bundled/bar"] },
  });
  assert.deepEqual(names, ["myskills", "orphan"]);
});

check("empty fleet yields no unused names", () => {
  assert.deepEqual(unusedRepoNames({ repos: [], pmos: [] }, {}), []);
});

if (failed) {
  console.error(`unused_repos.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("unused_repos.mjs: all checks passed");
