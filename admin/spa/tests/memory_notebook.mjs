// Hermetic checks for memory-notebook binding (CAKE-125).
// Same selection sets as unusedRepos.js / health.unused_repo_names:
// PMO memory_repos ∪ Dev Type memory_repos — not skill-source prefixes.
import assert from "node:assert/strict";
import { isMemoryNotebookCard } from "../src/lib/memoryNotebook.js";

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

check("unbound card is not a notebook", () => {
  assert.equal(isMemoryNotebookCard("work1", {
    pmos: [{ repos: ["work1"], reference_repos: [], memory_repos: [] }],
  }, {}), false);
});

check("PMO memory_repos binds the card as a notebook", () => {
  assert.equal(isMemoryNotebookCard("notes", {
    pmos: [{ repos: [], reference_repos: [], memory_repos: ["notes"] }],
  }, {}), true);
});

check("Dev Type memory_repos binds the card as a notebook", () => {
  assert.equal(isMemoryNotebookCard("dtmem", {
    pmos: [],
  }, { curator: { memory_repos: ["dtmem"] } }), true);
});

check("skill-source prefixes are not notebook binding", () => {
  assert.equal(isMemoryNotebookCard("myskills", {
    pmos: [],
  }, { implementer: { memory_repos: [], skills: ["myskills/foo"] } }), false);
});

check("missing name / empty fleet is not a notebook", () => {
  assert.equal(isMemoryNotebookCard("", { pmos: [] }, {}), false);
  assert.equal(isMemoryNotebookCard(null, null, null), false);
});

if (failed) {
  console.error(`memory_notebook.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("memory_notebook.mjs: all checks passed");
