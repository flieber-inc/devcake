// stalled_dispatches: the Overview alert and the board badge derive from the
// same helpers, so a stall reads the same in both places.
import assert from "node:assert/strict";
import deriveAlerts from "../src/lib/alerts.js";
import { stallAge, stallIndex, stallSeverity, stallWords } from "../src/lib/stalls.js";

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

const row = (over = {}) => ({
  instance: "dev", pmo_id: "p1", key: "AIDEV-220", kind: "upstream",
  subject: "AIDEV-219", text: "upstream activity unavailable — dispatch deferred",
  since: "2026-09-02T18:50:00+00:00", seconds: 8 * 86400, severity: "critical", ...over,
});

console.log("stalled dispatches");

check("age reads in minutes, hours, days", () => {
  assert.equal(stallAge(90), "2 min");
  assert.equal(stallAge(7200), "2 h");
  assert.equal(stallAge(8 * 86400), "8 d");
});

check("kinds have plain words; unknown falls back", () => {
  assert.equal(stallWords(row()), "upstream context missing");
  assert.equal(stallWords(row({ kind: "harness" })), "Dev image not receipted");
  assert.equal(stallWords(row({ kind: "zzz" })), "cannot start");
});

check("one stalled mission is a critical, non-dismissable alert naming it", () => {
  const alerts = deriveAlerts({ stalled_dispatches: [row()] });
  const hit = alerts.find((a) => a.id === "stalled-dispatches");
  assert.ok(hit, "alert missing");
  assert.equal(hit.severity, "critical");
  assert.equal(hit.dismissable, undefined);
  assert.match(hit.title, /AIDEV-220 cannot start/);
  assert.match(hit.body, /upstream context missing \(AIDEV-219\) for 8 d/);
  assert.match(hit.body, /status comment/);
});

check("a young stall is a warning; many are counted", () => {
  const alerts = deriveAlerts({ stalled_dispatches: [
    row({ severity: "warning", seconds: 2400 }),
    row({ pmo_id: "p2", key: "AIDEV-221", severity: "warning", seconds: 2400 }),
  ] });
  const hit = alerts.find((a) => a.id === "stalled-dispatches");
  assert.equal(hit.severity, "warning");
  assert.match(hit.title, /2 missions cannot start/);
});

check("no stalls, no alert", () => {
  assert.ok(!deriveAlerts({ stalled_dispatches: [] }).find((a) => a.id === "stalled-dispatches"));
  assert.ok(!deriveAlerts({}).find((a) => a.id === "stalled-dispatches"));
});

check("the board index is instance-qualified and severity aggregates", () => {
  const idx = stallIndex([row(), row({ instance: "cs", pmo_id: "p1", severity: "warning" })]);
  assert.equal(idx.get("dev:p1").key, "AIDEV-220");
  assert.equal(idx.get("cs:p1").severity, "warning");
  assert.equal(idx.get("x:p9"), undefined);
  assert.equal(stallSeverity([row({ severity: "warning" })]), "warning");
  assert.equal(stallSeverity([row({ severity: "warning" }), row()]), "critical");
});

if (failed) { console.log(`${failed} check(s) failed`); process.exit(1); }
