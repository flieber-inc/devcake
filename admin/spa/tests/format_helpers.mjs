// Pure-node checks for the Runs-page number formatters (no browser) —
// literal expectations, never a re-derivation of the formatter's math.
import assert from "node:assert/strict";
import { tokens, usd, durationSeconds, shortHarnessVersion } from "../src/lib/format.js";

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

check("tokens: null and undefined render as em dash", () => {
  assert.equal(tokens(null), "—");
  assert.equal(tokens(undefined), "—");
});
check("tokens: zero is a real value, not unknown", () =>
  assert.equal(tokens(0), "0"));
check("tokens: tiny counts never read as nothing", () => {
  assert.equal(tokens(1), "<0.01M");
  assert.equal(tokens(9_999), "<0.01M");
});
check("tokens: millions with two decimals + M suffix", () => {
  assert.equal(tokens(10_000), "0.01M");
  assert.equal(tokens(2_430_000), "2.43M");
  assert.equal(tokens(2_428_288), "2.43M");
  assert.equal(tokens(95_488), "0.10M");
  assert.equal(tokens(12_345_678), "12.35M");
});

check("usd: null renders as em dash, never $0.00", () =>
  assert.equal(usd(null), "—"));
check("usd: two decimals by default, four on demand", () => {
  assert.equal(usd(1.084), "$1.08");
  assert.equal(usd(5.6), "$5.60");
  assert.equal(usd(1.0752, 4), "$1.0752");
  assert.equal(usd(0), "$0.00");
});
check("usd: sub-cent spend never rounds to free", () =>
  assert.equal(usd(0.0042), "<$0.01"));

check("shortHarnessVersion: pulls the semver token off a CLI --version line", () => {
  assert.equal(shortHarnessVersion(""), "");
  assert.equal(shortHarnessVersion(null), "");
  assert.equal(shortHarnessVersion("2.1.229 (Claude Code)"), "2.1.229");
  assert.equal(shortHarnessVersion("grok 1.0.4 (d846eb93d9)"), "1.0.4");
  assert.equal(shortHarnessVersion("codex-cli 0.147.0"), "0.147.0");
  assert.equal(shortHarnessVersion("0.84.2"), "0.84.2");
  assert.equal(shortHarnessVersion("unknown"), "unknown");
});

check("durationSeconds: matches the duration() display grammar", () => {
  assert.equal(durationSeconds(null), "—");
  assert.equal(durationSeconds(42), "42s");
  assert.equal(durationSeconds(247), "4m 07s");
  assert.equal(durationSeconds(11_520), "3h 12m");
  assert.equal(durationSeconds(0), "0s");
});

if (failed) {
  console.error(`format_helpers: ${failed} failed`);
  process.exit(1);
}
console.log("format_helpers: all passed");
