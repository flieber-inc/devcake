// Cross-language contract replay (ADR-0034; 2026-08-12 audit): the three
// backend mirrors — board derivation, instance-name rule, card scaffolds —
// are pinned against docs/contracts/spa-contracts.json, GENERATED from the
// Python source of truth (app/tests/gen_spa_contracts.py). This side turns
// red when an SPA lib drifts; pytest turns red when the backend changes
// without regenerating. Pure node, no browser.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  ACTION_PRECONDITIONS, columnOf, contextActions, needsHumanReason,
} from "../src/lib/board.js";
import { newPmoCard, newRepoCard, newSkillSourceCard } from "../src/lib/cards.js";
import {
  ATTEMPT_RESET_POLICIES, CONTINUATION_POLICIES,
} from "../src/lib/configEnums.js";
import { CONNECTION_FIELDS } from "../src/lib/connectionFields.js";
import { MISSION_STAGES } from "../src/lib/missionStages.js";
import { STOPPED_STATES, TERMINAL_STATES } from "../src/lib/runStates.js";

const here = dirname(fileURLToPath(import.meta.url));
const contracts = JSON.parse(
  readFileSync(join(here, "../../../docs/contracts/spa-contracts.json")));

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

// both mirror regexes live in draftErrors.js since the hermetic split
// (2026-08-13): the validation seam must stay importable with no
// node_modules, so the literals moved to the dependency-free module
check("instance-name regex matches config._INSTANCE_NAME_RE", () => {
  const src = readFileSync(join(here, "../src/lib/draftErrors.js"), "utf8");
  assert.ok(src.includes(`/${contracts.instance_name_re}/`),
    "draftErrors.js INSTANCE_NAME_RE drifted from the contract");
  const re = new RegExp(`^${contracts.instance_name_re}$`);
  assert.ok(re.test("linear"), "accepts a valid instance name");
  assert.ok(!re.test("Bad-Name"), "rejects uppercase / hyphen");
});

check("harness-var regex matches config.HARNESS_VAR_PATTERN", () => {
  const src = readFileSync(join(here, "../src/lib/draftErrors.js"), "utf8");
  assert.ok(src.includes(`/^${contracts.harness_var_pattern}$/`),
    "draftErrors' inline harness-var regex drifted from the contract");
  const re = new RegExp(`^${contracts.harness_var_pattern}$`);
  assert.ok(re.test("ANTHROPIC_API_KEY"), "accepts a harness var");
  assert.ok(!re.test("not-a-var"), "rejects lowercase / hyphen");
});

check("cron-id regex matches config._CRON_ID_RE", () => {
  const src = readFileSync(join(here, "../src/lib/draftErrors.js"), "utf8");
  assert.ok(src.includes(`/${contracts.cron_id_re}/`),
    "draftErrors.js CRON_ID_RE drifted from the contract");
  // cron_id_re already carries ^…$ (same shape as instance_name_re)
  const re = new RegExp(contracts.cron_id_re);
  assert.ok(re.test("memory-curator"), "accepts a valid cron id");
  assert.ok(re.test("nightly_sync"), "accepts underscore");
  assert.ok(!re.test("Bad-Id"), "rejects uppercase");
  assert.ok(!re.test("1starts-digit"), "rejects leading digit");
});

check("runStates.js TERMINAL_STATES matches spa-contracts", () => {
  assert.deepEqual([...TERMINAL_STATES].sort(),
    [...contracts.run_terminal_states].sort());
});

check("runStates.js STOPPED_STATES matches spa-contracts", () => {
  assert.deepEqual([...STOPPED_STATES].sort(),
    [...contracts.run_stopped_states].sort());
});

check("configEnums CONTINUATION_POLICIES matches spa-contracts", () => {
  assert.deepEqual([...CONTINUATION_POLICIES].sort(),
    [...contracts.continuation_policies].sort());
});

check("configEnums ATTEMPT_RESET_POLICIES matches spa-contracts", () => {
  assert.deepEqual([...ATTEMPT_RESET_POLICIES].sort(),
    [...contracts.attempt_reset_policies].sort());
});

check("missionStages.js MISSION_STAGES matches spa-contracts", () => {
  assert.deepEqual(MISSION_STAGES, contracts.mission_stages);
});

check("connectionFields.js CONNECTION_FIELDS matches spa-contracts", () => {
  assert.deepEqual(CONNECTION_FIELDS, contracts.connection_fields);
});

check("board.js ACTION_PRECONDITIONS matches spa-contracts", () => {
  assert.deepEqual(ACTION_PRECONDITIONS, contracts.action_preconditions);
});

check("contextActions offers each action when preconditions hold", () => {
  for (const [action, spec] of Object.entries(contracts.action_preconditions)) {
    const labels = [...spec.require_present];
    // Satisfy require_absent by omitting those labels; for park (absent
    // SKIP) an empty set is enough. For mutual exclusion, SKIP present
    // implies unpark not park.
    const row = { labels, status: "in_progress" };
    const ids = contextActions(row).map((it) => it.id);
    assert.ok(ids.includes(action),
      `${action} missing for labels=[${labels}] (got ${ids})`);
  }
});

check("contextActions withholds actions when required labels missing", () => {
  const ids = contextActions({ labels: ["DEVCAKE"], status: "backlog" })
    .map((it) => it.id);
  assert.ok(!ids.includes("retry"));
  assert.ok(!ids.includes("resume"));
  assert.ok(!ids.includes("force_freshness"));
  assert.ok(!ids.includes("unpark"));
  assert.ok(ids.includes("park"), "park when SKIP absent");
});

check("park/unpark mutual exclusion preserved", () => {
  const withSkip = contextActions({
    labels: ["DEVCAKE-SKIP"], status: "backlog",
  }).map((it) => it.id);
  assert.ok(withSkip.includes("unpark"));
  assert.ok(!withSkip.includes("park"));
  const without = contextActions({
    labels: ["DEVCAKE"], status: "backlog",
  }).map((it) => it.id);
  assert.ok(without.includes("park"));
  assert.ok(!without.includes("unpark"));
});

check("MERGE offers force_freshness", () => {
  const ids = contextActions({
    labels: ["DEVCAKE", "DEVCAKE-MERGE"], status: "in_progress",
  }).map((it) => it.id);
  assert.ok(ids.includes("force_freshness"));
});

check("PMO card scaffold equals the server-model defaults", () => {
  const { name: _n, system: _s, ...rest } = newPmoCard("x", "linear");
  assert.deepEqual(rest, contracts.pmo_card_defaults);
});

check("repo card scaffold equals the server-model defaults", () => {
  const { name: _n, url: _u, ...rest } = newRepoCard("x");
  assert.deepEqual(rest, contracts.repo_card_defaults);
});

check("skill-source card scaffold equals the server-model defaults", () => {
  const { name: _n, ...rest } = newSkillSourceCard("x");
  assert.deepEqual(rest, contracts.skill_source_card_defaults);
});

// Adapter registry FALLBACK is a pinned mirror of GET /connections/registry
// (ADR-0034 / CAKE-23). Pytest owns the Python↔JSON equality; this side
// guards the SPA import path so a reintroduced inline second table is red.
check("registry.js cold-start FALLBACK is the pinned JSON mirror", () => {
  const src = readFileSync(join(here, "../src/lib/registry.js"), "utf8");
  assert.ok(
    src.includes('from "./registry_fallback.json"'),
    "registry.js must import registry_fallback.json (not an inline FALLBACK table)",
  );
  assert.ok(
    !/\bconst\s+FALLBACK\s*=\s*\{/.test(src),
    "registry.js must not reintroduce an inline FALLBACK object",
  );
  const fallback = JSON.parse(
    readFileSync(join(here, "../src/lib/registry_fallback.json"), "utf8"));
  assert.ok(Array.isArray(fallback.pmo_systems) && fallback.pmo_systems.length >= 4);
  assert.ok(Array.isArray(fallback.forges) && fallback.forges.length >= 3);
  for (const s of fallback.pmo_systems) {
    assert.equal(typeof s.experimental, "boolean", `${s.id}.experimental`);
    assert.equal(typeof s.supports_priority, "boolean", `${s.id}.supports_priority`);
  }
  assert.equal(typeof fallback.managed_labels_expected, "number");
});

// ── board derivation replay ─────────────────────────────────────────────────
// Vector = [status_i, adoption_i, [labels], reason_i]; the mapping from the
// Python derivation ROW to the board column/badge is test data HERE (small,
// reviewed, single place) — the precedence itself is what the vectors pin.
const STAGE = { "DEVCAKE-PLAN": "plan", "DEVCAKE-EXECUTE": "execute",
                "DEVCAKE-REVIEW": "review" };
const COLUMN_BY_REASON = {
  "not adopted (opt-in mode, no DEVCAKE label)": null,
  "LABEL_CONFLICT — ≥2 stage labels; human must fix": "needs_human",
  "DEVCAKE-SKIP — human opt-out": "needs_human",
  "DEVCAKE-FAILED — needs human attention": "needs_human",
  "DEVCAKE-NEEDS-HUMAN — awaiting human action": "needs_human",
  "awaiting merge (merge sweep handles)": "merge",
  "backlog, no stage label": "backlog",
  "in_progress without stage label — not DevCake's": null,
};
const BADGE_BY_REASON = {
  "LABEL_CONFLICT — ≥2 stage labels; human must fix": "conflict",
  "DEVCAKE-SKIP — human opt-out": "parked",
  "DEVCAKE-FAILED — needs human attention": "failed",
  "DEVCAKE-NEEDS-HUMAN — awaiting human action": "needs human",
};

check("columnOf replays every backend derivation vector", () => {
  const { statuses, adoption_modes, reasons, vectors } = contracts.board;
  let checked = 0;
  for (const [si, ai, labels, ri] of vectors) {
    const status = statuses[si];
    const adoption = adoption_modes[ai];
    const reason = reasons[ri];
    const hasAnyDevcake = labels.some(
      (l) => l === "DEVCAKE" || l.startsWith("DEVCAKE-"));
    let expected;
    if (reason === "terminal — ignored") {
      expected = hasAnyDevcake ? "done" : null;    // board visibility rule
    } else if (reason === "stage label") {
      expected = STAGE[labels.find((l) => STAGE[l])];
    } else {
      assert.ok(reason in COLUMN_BY_REASON, `unmapped reason: ${reason}`);
      expected = COLUMN_BY_REASON[reason];
    }
    const got = columnOf({ status, labels }, adoption);
    assert.equal(got, expected,
      `status=${status} adoption=${adoption} labels=[${labels}] ` +
      `reason=${JSON.stringify(reason)}: columnOf=${got} expected=${expected}`);
    checked += 1;
  }
  assert.equal(checked, vectors.length);
});

check("needsHumanReason badge matches the derivation row", () => {
  const { reasons, vectors } = contracts.board;
  for (const [, , labels, ri] of vectors) {
    const reason = reasons[ri];
    if (!(reason in BADGE_BY_REASON)) continue;
    assert.equal(needsHumanReason(labels), BADGE_BY_REASON[reason],
      `labels=[${labels}] reason=${JSON.stringify(reason)}`);
  }
});

if (failed) {
  console.error(`contracts.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("contracts.mjs: all checks passed");
