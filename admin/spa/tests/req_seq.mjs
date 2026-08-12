// Pure-node checks for the stale-response guard (2026-08-12 audit): an old
// request resolving AFTER a newer one must be dropped, so a config rebase
// never rolls the server baseline backward and a filtered runs query never
// overwrites fresh rows with stale ones.
import assert from "node:assert/strict";
import { makeReqSeq } from "../src/lib/reqSeq.js";

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

check("the latest token wins; an earlier one is stale", () => {
  const seq = makeReqSeq();
  const a = seq.next();
  const b = seq.next();
  assert.equal(seq.isLatest(b), true);
  assert.equal(seq.isLatest(a), false, "the older request must be dropped");
});

check("out-of-order resolution: only the newest commit lands", () => {
  const seq = makeReqSeq();
  const committed = [];
  // simulate two loads issued A then B, resolving B then A
  const a = seq.next();
  const b = seq.next();
  if (seq.isLatest(b)) committed.push("B");   // resolves first
  if (seq.isLatest(a)) committed.push("A");   // resolves second — stale
  assert.deepEqual(committed, ["B"], "the stale A resolution must not commit");
});

check("a single in-flight request commits normally", () => {
  const seq = makeReqSeq();
  const t = seq.next();
  assert.equal(seq.isLatest(t), true);
});

if (failed) {
  console.error(`req_seq.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("req_seq.mjs: all checks passed");
