// Hermetic checks for filtered list pagination (CAKE-145) —
// literal page windows over synthetic names; never re-derive the util's math.
import assert from "node:assert/strict";
import { listWindow, pageForIndex } from "../src/lib/listWindow.js";

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

const names160 = Array.from({ length: 160 }, (_, i) => `repo${String(i).padStart(3, "0")}`);

check("160 names, empty query, pageSize 30: page 0 is first 30", () => {
  const w = listWindow(names160, "", 30, 0);
  assert.equal(w.totalMatched, 160);
  assert.equal(w.pageCount, 6);
  assert.equal(w.pageIndex, 0);
  assert.deepEqual(w.pageNames, names160.slice(0, 30));
  assert.equal(w.matched.length, 160);
});

check("160 names: page 5 holds the last 10; every name on exactly one page", () => {
  const seen = new Set();
  for (let p = 0; p < 6; p++) {
    const w = listWindow(names160, "", 30, p);
    assert.equal(w.pageCount, 6);
    assert.equal(w.pageIndex, p);
    for (const n of w.pageNames) {
      assert.equal(seen.has(n), false, `duplicate ${n}`);
      seen.add(n);
    }
  }
  assert.equal(seen.size, 160);
  const last = listWindow(names160, "", 30, 5);
  assert.deepEqual(last.pageNames, names160.slice(150, 160));
});

check("filter matching 45 names yields two pages with no silent drop", () => {
  const matched = Array.from({ length: 45 }, (_, i) => `alpha${String(i).padStart(2, "0")}`);
  const fillers = Array.from({ length: 20 }, (_, i) => `other${i}`);
  const names = [...matched, ...fillers];
  const w0 = listWindow(names, "alpha", 30, 0);
  assert.equal(w0.totalMatched, 45);
  assert.equal(w0.pageCount, 2);
  assert.deepEqual(w0.pageNames, matched.slice(0, 30));
  const w1 = listWindow(names, "alpha", 30, 1);
  assert.deepEqual(w1.pageNames, matched.slice(30, 45));
  assert.equal(w0.pageNames.length + w1.pageNames.length, 45);
});

check("empty query returns all names; case-insensitive substring filter", () => {
  const names = ["Alpha", "beta", "GAMMA"];
  assert.deepEqual(listWindow(names, "", 10, 0).pageNames, names);
  assert.deepEqual(listWindow(names, "LP", 10, 0).pageNames, ["Alpha"]);
  assert.deepEqual(listWindow(names, "BETA", 10, 0).pageNames, ["beta"]);
  assert.deepEqual(listWindow(names, "amm", 10, 0).pageNames, ["GAMMA"]);
});

check("stale high pageIndex clamps to last page", () => {
  const names = Array.from({ length: 45 }, (_, i) => `n${i}`);
  const w = listWindow(names, "", 30, 99);
  assert.equal(w.pageIndex, 1);
  assert.equal(w.pageCount, 2);
  assert.deepEqual(w.pageNames, names.slice(30, 45));
});

check("zero matches: empty page, pageCount 0, pageIndex 0", () => {
  const w = listWindow(["a", "b"], "zzz", 30, 3);
  assert.equal(w.totalMatched, 0);
  assert.equal(w.pageCount, 0);
  assert.equal(w.pageIndex, 0);
  assert.deepEqual(w.pageNames, []);
});

check("pageForIndex maps absolute indexes to page numbers", () => {
  assert.equal(pageForIndex(0, 30), 0);
  assert.equal(pageForIndex(29, 30), 0);
  assert.equal(pageForIndex(30, 30), 1);
  assert.equal(pageForIndex(159, 30), 5);
});

if (failed) {
  console.error(`list_window_helpers.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("list_window_helpers.mjs: all checks passed");
