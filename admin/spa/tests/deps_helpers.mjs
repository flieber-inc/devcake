// Pure-node checks for the dependency-chain derivation (no browser) —
// resolution honesty (same-instance preference, ambiguous ⇒ unresolved,
// raw ids stay raw), inversion, cycle-safe traversal, and status copy.
import assert from "node:assert/strict";
import {
  buildDepIndex, chainAround, inCycle, nodeId, nodeStatus, parseCycles, resolveKey,
} from "../src/lib/deps.js";

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

const row = (instance, pmo_id, key, extra = {}) => ({
  instance, pmo_id, key,
  kind: "issue", title: key, status: "backlog", labels: ["DEVCAKE"],
  blocked_by: [], mission_type: "ONBOARD", ...extra,
});

check("nodeId is instance-qualified (gitea per-repo ids collide)", () => {
  assert.notEqual(nodeId(row("a", "1", "x/y#1")), nodeId(row("b", "1", "x/y#1")));
});

check("resolveKey: unique match resolves regardless of instance", () => {
  const rows = [row("linear1", "L1", "DEV-1")];
  const idx = buildDepIndex(rows);
  assert.equal(resolveKey(idx, "DEV-1", "gitea1"), rows[0]);
});

check("resolveKey: cross-instance collision prefers the same instance", () => {
  const a = row("g1", "1", "x/y#1");
  const b = row("g2", "9", "x/y#1");
  const idx = buildDepIndex([a, b]);
  assert.equal(resolveKey(idx, "x/y#1", "g1"), a);
  assert.equal(resolveKey(idx, "x/y#1", "g2"), b);
});

check("resolveKey: ambiguous foreign match is unresolved, never guessed", () => {
  const idx = buildDepIndex([row("g1", "1", "x/y#1"), row("g2", "9", "x/y#1")]);
  assert.equal(resolveKey(idx, "x/y#1", "linear1"), null);
});

check("resolveKey: a raw vendor id stays unresolved", () => {
  const idx = buildDepIndex([row("l1", "L1", "DEV-1")]);
  assert.equal(resolveKey(idx, "01f0a3c4-dead-beef", "l1"), null);
});

check("chainAround: inversion — dependents come from other rows' blocked_by", () => {
  const a = row("l1", "A", "DEV-1", { status: "done" });
  const b = row("l1", "B", "DEV-2", { blocked_by: ["DEV-1"] });
  const c = row("l1", "C", "DEV-3", { blocked_by: ["DEV-2"] });
  const idx = buildDepIndex([a, b, c]);
  const chain = chainAround(idx, b);
  assert.deepEqual(chain.blockers.map((x) => x.row), [a]);
  assert.deepEqual(chain.dependents, [c]);
  assert.deepEqual(chain.upstream, []);
  assert.deepEqual(chain.downstream, []);
});

check("chainAround: transitive ring lands in upstream/downstream, not direct", () => {
  const a = row("l1", "A", "DEV-1");
  const b = row("l1", "B", "DEV-2", { blocked_by: ["DEV-1"] });
  const c = row("l1", "C", "DEV-3", { blocked_by: ["DEV-2"] });
  const d = row("l1", "D", "DEV-4", { blocked_by: ["DEV-3"] });
  const idx = buildDepIndex([a, b, c, d]);
  const chain = chainAround(idx, c);
  assert.deepEqual(chain.blockers.map((x) => x.row), [b]);
  assert.deepEqual(chain.dependents, [d]);
  assert.deepEqual(chain.upstream, [a]);
  assert.deepEqual(chain.downstream, []);
});

check("chainAround: terminates on a cycle instead of hanging", () => {
  const a = row("l1", "A", "DEV-1", { blocked_by: ["DEV-2"] });
  const b = row("l1", "B", "DEV-2", { blocked_by: ["DEV-1"] });
  const idx = buildDepIndex([a, b]);
  const chain = chainAround(idx, a);
  assert.equal(chain.blockers[0].row, b);
  assert.deepEqual(chain.dependents, [b]);
  assert.deepEqual(chain.upstream, []);
});

check("parseCycles handles bare and instance-prefixed forms", () => {
  const members = parseCycles([["DEV-7", "linear1:DEV-8"]]);
  assert.ok(inCycle(row("linear1", "X", "DEV-7"), members));
  assert.ok(inCycle(row("linear1", "Y", "DEV-8"), members));
  assert.ok(!inCycle(row("linear1", "Z", "DEV-9"), members));
});

check("nodeStatus: done blocker says it no longer blocks, dimmed", () => {
  const s = nodeStatus(row("l1", "A", "DEV-1", { status: "done" }), "auto",
    { asBlocker: true });
  assert.equal(s.text, "Done — no longer blocking");
  assert.equal(s.tone, "satisfied");
});

check("nodeStatus: guard-labelled blocker names the label and the deadlock", () => {
  const s = nodeStatus(
    row("l1", "A", "DEV-1", { status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-FAILED"] }),
    "auto", { asBlocker: true });
  assert.equal(s.text, "failed (DEVCAKE-FAILED) — blocks until a human acts");
  assert.equal(s.tone, "guard");
});

check("nodeStatus: open blocker carries its stage", () => {
  const s = nodeStatus(
    row("l1", "A", "DEV-1", { status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-EXECUTE"] }),
    "auto", { asBlocker: true });
  assert.equal(s.text, "execute — open");
  assert.equal(s.tone, "open");
});

if (failed) {
  console.log(`deps_helpers: ${failed} failed`);
  process.exitCode = 1;
} else {
  console.log("deps_helpers: all passed");
}
