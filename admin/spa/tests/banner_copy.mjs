// Hermetic pin for the backend-unreachable banner sentence (CAKE-125).
// The banner must not claim controls are disabled — only the intake switch
// is gated; Poll now / New mission / Runs stay clickable and fail per-request.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const appSrc = readFileSync(join(root, "src/App.jsx"), "utf8");

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

check("banner says actions will fail (not that controls are disabled)", () => {
  assert.match(appSrc, /Actions will fail until it responds/);
  assert.doesNotMatch(appSrc, /Controls are disabled until it responds/);
});

if (failed) {
  console.error(`banner_copy.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("banner_copy.mjs: all checks passed");
