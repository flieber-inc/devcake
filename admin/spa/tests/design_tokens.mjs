// DESIGN.md §1: components use only the @theme registers (accent / neutral /
// surfaces / amber / red / green) plus the documented light-mode stone hover
// idiom. Stock Tailwind blue/sky/yellow/… families are residual palette drift
// — they are not retuned in index.css @theme and read as a second brand accent.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const srcRoot = join(here, "../src");

// Families not listed in DESIGN.md §1 and not part of the Runs-table stone
// hover idiom. Numeric suffix required so prose comments ("blue running") can
// still name the concept without tripping the guard until classes are gone.
const OFF_PALETTE = /\b(?:blue|sky|yellow|purple|indigo|cyan|teal|violet|fuchsia|rose|orange)-[0-9]{2,3}\b/g;

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(js|jsx)$/.test(name)) out.push(p);
  }
  return out;
}

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

check("no off-palette Tailwind color families in SPA components", () => {
  const hits = [];
  for (const file of walk(srcRoot)) {
    const text = readFileSync(file, "utf8");
    const rel = relative(join(here, ".."), file);
    for (const m of text.matchAll(OFF_PALETTE)) {
      const line = text.slice(0, m.index).split("\n").length;
      hits.push(`${rel}:${line}: ${m[0]}`);
    }
  }
  assert.equal(hits.length, 0,
    `off-palette classes (DESIGN.md §1):\n  ${hits.join("\n  ")}`);
});

if (failed) {
  console.error(`design_tokens.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("design_tokens.mjs: all checks passed");
