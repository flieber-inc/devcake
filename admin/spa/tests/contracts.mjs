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

import { columnOf, needsHumanReason } from "../src/lib/board.js";
import { newPmoCard, newRepoCard } from "../src/lib/cards.js";

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

check("PMO card scaffold equals the server-model defaults", () => {
  const { name: _n, system: _s, ...rest } = newPmoCard("x", "linear");
  assert.deepEqual(rest, contracts.pmo_card_defaults);
});

check("repo card scaffold equals the server-model defaults", () => {
  const { name: _n, url: _u, ...rest } = newRepoCard("x");
  assert.deepEqual(rest, contracts.repo_card_defaults);
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
