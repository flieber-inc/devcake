// Hermetic checks for the bulk "Discover default branches" draft edit:
// blank fields are filled, pins the repository lacks are replaced, pins the
// repository has are KEPT (a deliberate non-default base stays deliberate),
// failures are isolated and reported. Independent expected values are literals.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { DISCOVER_ALL_BRANCHES_DESC, applyDiscoveredBranches } from "../src/lib/branches.js";

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

const repos = [
  { name: "blank", url: "https://forge.example/o/blank", default_branch: "" },
  { name: "wrongpin", url: "https://forge.example/o/wrongpin", default_branch: "main" },
  { name: "goodpin", url: "https://forge.example/o/goodpin", default_branch: "development" },
  { name: "same", url: "https://forge.example/o/same", default_branch: "master" },
  { name: "broken", url: "https://forge.example/o/broken", default_branch: "" },
  { name: "untouched", url: "https://forge.example/o/untouched", default_branch: "" },
];
const results = {
  blank: { ok: true, branch: "master", pinned: false },
  wrongpin: { ok: true, branch: "master", pinned: true, pin_exists: false },
  goodpin: { ok: true, branch: "main", pinned: true, pin_exists: true },
  same: { ok: true, branch: "master", pinned: true, pin_exists: true },
  broken: { ok: false, error: "authentication failed" },
};

check("fills blank fields and replaces pins the repository lacks", () => {
  const out = applyDiscoveredBranches(repos, results);
  const by = Object.fromEntries(out.repos.map((r) => [r.name, r.default_branch]));
  assert.equal(by.blank, "master");
  assert.equal(by.wrongpin, "master");
  assert.equal(out.changed, true);
  assert.deepEqual(out.summary.filled.map((f) => f.name), ["blank", "wrongpin"]);
  assert.deepEqual(out.summary.filled[1], { name: "wrongpin", from: "main", to: "master" });
});

check("keeps a pin the repository has and reports it", () => {
  const out = applyDiscoveredBranches(repos, results);
  const by = Object.fromEntries(out.repos.map((r) => [r.name, r.default_branch]));
  assert.equal(by.goodpin, "development");
  assert.deepEqual(out.summary.kept, [
    { name: "goodpin", pin: "development", branch: "main" },
    { name: "same", pin: "master", branch: "master" },
  ]);
});

check("isolates failures and leaves cards without a result alone", () => {
  const out = applyDiscoveredBranches(repos, results);
  const by = Object.fromEntries(out.repos.map((r) => [r.name, r.default_branch]));
  assert.equal(by.broken, "");
  assert.equal(by.untouched, "");
  assert.deepEqual(out.summary.failed, [{ name: "broken", error: "authentication failed" }]);
  // the input is never mutated (draft edits go through setField)
  assert.equal(repos[0].default_branch, "");
});

check("no change when every card already matches", () => {
  const out = applyDiscoveredBranches(
    [{ name: "a", default_branch: "master" }],
    { a: { ok: true, branch: "master", pinned: true, pin_exists: true } },
  );
  assert.equal(out.changed, false);
  assert.deepEqual(out.summary.filled, []);
});

check("the page wires the per-card button, the field and the section action", () => {
  assert.match(reposSrc, /discover-branch`/, "per-card endpoint");
  assert.match(reposSrc, /"\/connections\/forge\/discover-branches"/, "bulk endpoint");
  assert.match(reposSrc, /label:\s*"Discover default branches for all repositories…"/);
  assert.match(reposSrc, /<Field label="Branch" hint="Empty = the repository's default"/);
  assert.match(reposSrc, /confirmLabel:\s*"Discover now"/);
  assert.match(DISCOVER_ALL_BRANCHES_DESC, /kept/);
});

if (failed) {
  console.log(`${failed} check(s) failed`);
  process.exit(1);
}
console.log("discover_branch_helpers.mjs: all checks passed");
