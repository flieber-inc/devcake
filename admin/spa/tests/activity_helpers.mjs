// Hermetic checks for the status bar's reading of /activity (docs/11 §0):
// idle vs busy vs stalled vs waiting, the one-line summary, elapsed text.
import assert from "node:assert/strict";
import { formatElapsed, phaseLabel, summarizeActivity } from "../src/lib/activity.js";

let failed = 0;
const check = (name, fn) => {
  try { fn(); console.log(`  ✓ ${name}`); }
  catch (e) { failed += 1; console.log(`  ✗ ${name}: ${e.message}`); }
};

const NOW = Date.parse("2026-01-10T12:00:40Z");

check("idle reads how long, what ran last, and how long that took", () => {
  const s = summarizeActivity({
    items: [], idle_since: "2026-01-10T12:00:10Z",
    recent: [{ kind: "poll.cycle", subject: "cycle 9", elapsed_s: 1.8 }],
    poll_skips: {},
  }, NOW);
  assert.equal(s.state, "idle");
  assert.equal(s.line, "idle for 30 s · last: poll cycle 9 in 2 s");
});

check("busy lists the first three phases and counts the rest", () => {
  const s = summarizeActivity({
    items: [
      { kind: "poll.instance", subject: "board", elapsed_s: 4, overdue: false },
      { kind: "mirror.sync", subject: "325 mirrors", elapsed_s: 3, overdue: false, detail: { done: 212, total: 325 } },
      { kind: "mission.dispatch", subject: "T-12 EXECUTE", elapsed_s: 2, overdue: false },
      { kind: "run.finalize", subject: "L-T-3-2-EXECUTE-AAAAAA", elapsed_s: 1, overdue: false },
    ],
    idle_since: null, recent: [], poll_skips: {},
  }, NOW);
  assert.equal(s.state, "busy");
  assert.equal(s.line, "4 in flight — polling board · syncing mirrors 212/325 · dispatching T-12 EXECUTE · +1 more");
  assert.deepEqual(s.items.map((i) => i.elapsed), ["4 s", "3 s", "2 s", "1 s"]);
});

check("an overdue phase makes the bar stalled and says so", () => {
  const s = summarizeActivity({
    items: [{ kind: "config.apply", subject: "settings save", elapsed_s: 200, overdue: true, detail: { state: "waiting for the poll cycle" } }],
    idle_since: null, recent: [], poll_skips: {},
  }, NOW);
  assert.equal(s.state, "stalled");
  assert.match(s.line, /settings save — waiting for the poll cycle/);
  assert.match(s.line, /one phase is overdue/);
  assert.equal(s.items[0].overdue, true);
});

check("a skipped poll segment reads as waiting, not frozen", () => {
  const s = summarizeActivity({
    items: [], idle_since: "2026-01-10T12:00:35Z", recent: [],
    poll_skips: { board: { at: "2026-01-10T12:00:30Z", reason: "request budget: reserved for critical calls", retry_after_s: 40.4 } },
  }, NOW);
  assert.equal(s.state, "waiting");
  assert.match(s.line, /1 board waiting/);
  assert.equal(s.skips[0].label, "board: poll segment skipped — request budget: reserved for critical calls (retry after 40 s)");
});

check("labels and durations", () => {
  assert.equal(phaseLabel({ kind: "pmo.budget.wait", subject: "t/u", detail: { wait_s: 61.4 } }), "waiting for tracker quota (61 s)");
  assert.equal(phaseLabel({ kind: "steward.launch", subject: "discovery" }), "launching the steward (discovery)");
  assert.equal(phaseLabel({ kind: "something.new", subject: "x" }), "something.new x");
  assert.equal(formatElapsed(59.4), "59 s");
  assert.equal(formatElapsed(125), "2 m 5 s");
  assert.equal(formatElapsed(7322), "2 h 2 m");
  assert.equal(summarizeActivity(null).state, "unknown");
});

if (failed) { console.log(`${failed} check(s) failed`); process.exit(1); }
console.log("activity_helpers.mjs: all checks passed");
