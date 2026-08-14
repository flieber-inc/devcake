// Pure-node checks for the config-draft diff/rebase seam (no browser) —
// pins the 2026-08-12 phantom-diff bug closed: adding a PMO card, saving,
// and refetching must converge to a CLEAN draft (no [object Object] rows,
// no permanent "Unsaved changes" bar).
import assert from "node:assert/strict";
import { diffLeaves, applyEditsOnto, getIn } from "../src/lib/objectPath.js";
import { metaFor, describeDiff } from "../src/lib/configLabels.js";
import { newPmoCard, newRepoCard } from "../src/lib/cards.js";
import { draftErrors } from "../src/lib/draftErrors.js";

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

// Server-shaped cards (config GET returns every model field).
const BOARD = {
  name: "board", system: "gitea_issues", team_key: "devcake-pmo/missions",
  api_base: "http://gitea:3000", repos: [], reference_repos: [],
  intake_paused: false, assignments: {}, managed: true,
};
const snap = (pmos, extra = {}) =>
  ({ cfg: { pmos, poll_interval_seconds: 30, ...extra }, devTypes: {}, assignments: {} });

// The add→save→refetch cycle, as the hook drives it.
const scaffold = newPmoCard("linearb", "linear");
const prevServer = snap([BOARD]);
const prevDraft = snap([BOARD, scaffold]);
// what the server returns after the save: the scaffold, enriched/canonical
const LINEARB_SAVED = { ...scaffold, api_base: null, team_key: "TEAM" };
const freshAfterSave = snap([BOARD, LINEARB_SAVED]);

check("add card: unequal-length instance lists diff as ONE atomic row", () => {
  const edits = diffLeaves(prevServer, prevDraft);
  assert.equal(edits.length, 1);
  assert.equal(edits[0].path, "cfg.pmos");
});

check("post-save rebase converges: clean diff, no phantom rows", () => {
  const edits = diffLeaves(prevServer, prevDraft);
  const next = applyEditsOnto(freshAfterSave, edits);
  assert.deepEqual(diffLeaves(freshAfterSave, next), []);
});

check("post-save rebase: the server's canonical card wins over the scaffold", () => {
  const edits = diffLeaves(prevServer, prevDraft);
  const next = applyEditsOnto(freshAfterSave, edits);
  assert.equal(getIn(next, "cfg.pmos.1.team_key"), "TEAM");
});

check("failed save: the addition stays dirty (edits win)", () => {
  const edits = diffLeaves(prevServer, prevDraft);
  const next = applyEditsOnto(prevServer, edits);   // fresh == old server
  const diff = diffLeaves(prevServer, next);
  assert.equal(diff.length, 1);
  assert.equal(diff[0].path, "cfg.pmos");
});

check("plain leaf edits still re-apply onto a fresh snapshot", () => {
  const edited = snap([BOARD], { poll_interval_seconds: 45 });
  const edits = diffLeaves(prevServer, edited);
  const fresh = snap([BOARD]);                       // server still at 30
  const next = applyEditsOnto(fresh, edits);
  assert.equal(getIn(next, "cfg.poll_interval_seconds"), 45);
});

check("rename inside a same-length list still wins over fresh", () => {
  const renamed = snap([{ ...BOARD, name: "renamed" }]);
  const edits = diffLeaves(prevServer, renamed);
  const next = applyEditsOnto(prevServer, edits);
  assert.equal(getIn(next, "cfg.pmos.0.name"), "renamed");
});

check("scaffolds carry every server-model field (pmo: 11, repo: 8)", () => {
  assert.deepEqual(Object.keys(newPmoCard("x", "linear")).sort(),
    ["api_base", "assignments", "discovery_routing", "intake_paused",
     "managed", "memory_repos", "name", "reference_repos", "repos",
     "system", "team_key"]);
  assert.deepEqual(Object.keys(newRepoCard("x")).sort(),
    ["api_base", "auto_merge", "auto_resolve_merge_conflicts",
     "default_branch", "forge", "merge_retry_window_minutes", "name",
     "url"]);
});

check("skill sources: name shape, dupes, repo-name collision", () => {
  const src = { name: "shelf", forge: "github", url: "u",
                default_branch: "", subdir: "" };
  const d = snap([BOARD], {
    repos: [{ name: "shelf" }],
    skill_sources: [src, { ...src, name: "Bad!" }],
  });
  const errs = draftErrors(d);
  assert.match(errs["cfg.skill_sources.0.name"] || "", /collides/);
  assert.match(errs["cfg.skill_sources.1.name"] || "", /invalid/);
  const d2 = snap([BOARD], { skill_sources: [src, { ...src }] });
  assert.match(draftErrors(d2)["cfg.skill_sources.1.name"] || "", /duplicate/);
});

check("pmo card fields have real labels, not raw paths", () => {
  assert.match(metaFor("cfg.pmos.1.intake_paused").label, /Intake paused/);
  assert.match(metaFor("cfg.pmos.1.discovery_routing").label,
    /Discovery routing/);
  assert.match(metaFor("cfg.pmos.1.managed").label, /Managed/);
  assert.notEqual(metaFor("cfg.pmos.1.assignments").group, "Other");
});

check("no diff row ever renders [object Object]", () => {
  const rows = describeDiff([
    { path: "cfg.pmos.1.assignments", old: {}, new: undefined },
    { path: "cfg.some.unknown", old: { a: 1 }, new: undefined },
  ]);
  for (const r of rows) {
    assert.ok(!`${r.oldText}${r.newText}`.includes("[object Object]"),
      `${r.path}: ${r.oldText} → ${r.newText}`);
  }
});

// container-limits Save guard (2026-08-13 audit): a cleared numeric box is
// stored as null by LimitsSection — Number("") === 0 would silently mean
// UNLIMITED — and must block Save via the draft's one validation seam.
check("cleared container-limit field blocks Save with an inline error", () => {
  const d = snap([BOARD],
    { container_limits: { memory_mb: null, cpus: 2, pids: 0 } });
  const errs = draftErrors(d);
  assert.match(errs["cfg.container_limits.memory_mb"], /0 for unlimited/);
  assert.equal(Object.keys(errs).length, 1);
});

check("explicit container-limit zeros (= unlimited) save cleanly", () => {
  const d = snap([BOARD],
    { container_limits: { memory_mb: 0, cpus: 0, pids: 0 } });
  assert.deepEqual(draftErrors(d), {});
});
// ── scheduled tasks (cfg.crons) — draftErrors mirrors config.py ──────────
const CURATOR = {
  id: "memory-curator", name: "Memory Curator", enabled: false,
  interval_minutes: 60, pmo: null, entry_stage: "EXECUTE",
  description_template: "drain", reserved: true,
};
const TASK = {
  id: "nightly", name: "N", enabled: false, interval_minutes: 60,
  pmo: "board", entry_stage: "EXECUTE", description_template: "x",
  reserved: false,
};
const cronSnap = (crons) => snap([BOARD], { crons });

check("valid custom task + reserved row produce no cron errors", () => {
  const errs = draftErrors(cronSnap([CURATOR, TASK]));
  assert.ok(!Object.keys(errs).some((k) => k.startsWith("cfg.crons")));
});

check("bad task id slug blocks Save inline", () => {
  const errs = draftErrors(cronSnap([CURATOR, { ...TASK, id: "Bad Id!" }]));
  assert.match(errs["cfg.crons.1.id"] || "", /invalid/);
});

check("duplicate task ids block Save (memory-curator included)", () => {
  const errs = draftErrors(
    cronSnap([CURATOR, { ...TASK, id: "memory-curator" }]));
  assert.match(errs["cfg.crons.1.id"] || "", /duplicate/);
});

check("custom task without a target board blocks Save", () => {
  const errs = draftErrors(cronSnap([CURATOR, { ...TASK, pmo: "" }]));
  assert.match(errs["cfg.crons.1.pmo"] || "", /target board/);
});

check("custom task targeting a removed board blocks Save", () => {
  const errs = draftErrors(cronSnap([CURATOR, { ...TASK, pmo: "gone" }]));
  assert.match(errs["cfg.crons.1.pmo"] || "", /removed board/);
});

check("reserved row is exempt from the target check", () => {
  const errs = draftErrors(cronSnap([CURATOR]));
  assert.equal(errs["cfg.crons.0.pmo"], undefined);
});

check("cron rows label under Scheduled Tasks in the save review", () => {
  const meta = metaFor("cfg.crons.1.description_template");
  assert.equal(meta.group, "Scheduled Tasks");
  assert.match(meta.label, /Ticket text/);
  assert.equal(metaFor("cfg.crons").group, "Scheduled Tasks");
  assert.equal(metaFor("cfg.steward.enabled").group, "Scheduled Tasks");
  assert.equal(metaFor("cfg.memory_auto_merge").group, "Limits");
});
if (failed) {
  console.error(`config_draft: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("config_draft: all checks passed");
