// DESIGN.md §1: components use only the @theme registers (accent / neutral /
// surfaces / amber / red / green) plus the documented light-mode stone hover
// idiom. Stock Tailwind blue/sky/yellow/… families are residual palette drift
// — they are not retuned in index.css @theme and read as a second brand accent.
//
// CAKE-158 also pins hex contracts: light stays warm; dark remaps under
// html.dark to a cool violet-tinted near-black so the brand accent reads
// electric instead of muddy.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const spaRoot = join(here, "..");
const srcRoot = join(spaRoot, "src");
const indexCss = join(srcRoot, "index.css");
const assetsRoot = join(srcRoot, "assets");

// Families not listed in DESIGN.md §1 and not part of the Runs-table stone
// hover idiom. Numeric suffix required so prose comments ("blue running") can
// still name the concept without tripping the guard until classes are gone.
const OFF_PALETTE = /\b(?:blue|sky|yellow|purple|indigo|cyan|teal|violet|fuchsia|rose|orange|slate|gray|zinc|emerald|lime|pink)-[0-9]{2,3}\b/g;

/** Cream / warm logo stops that must not appear on dark-mode artwork. */
const CREAM_LOGO_STOPS = ["#fffaf0", "#eee3d4", "#d9b99a", "#faf5eb", "#eee6d9", "#dac9b5"];

// Light @theme warm anchors — must not drift (founder likes light).
const LIGHT_PINS = {
  "--color-surface": "#faf9f7",
  "--color-surface-raised": "#ffffff",
  "--color-neutral-900": "#2a2018",
  "--color-neutral-950": "#1c1610",
  "--color-accent-400": "#a28ef1",
  "--color-accent-500": "#7b61dc",
  "--color-accent-600": "#6042cc",
};

// Dark-only surface tokens (cool violet-tinted near-black).
const DARK_SURFACE_PINS = {
  "--color-surface-dark": "#0f0e14",
  "--color-surface-raised-dark": "#17161f",
};

// Cool neutral ramp remapped under html.dark (same temperature as grounds).
const DARK_NEUTRAL_PINS = {
  "--color-neutral-50": "#f7f6fb",
  "--color-neutral-100": "#eeedf4",
  "--color-neutral-200": "#d8d6e2",
  "--color-neutral-300": "#b5b2c2",
  "--color-neutral-400": "#8a8699",
  "--color-neutral-500": "#6a6678",
  "--color-neutral-600": "#524e5f",
  "--color-neutral-700": "#3e3b4a",
  "--color-neutral-800": "#2a2834",
  "--color-neutral-900": "#1a1822",
  "--color-neutral-950": "#12111a",
};

// Lifted accent stops for dark text/chips — brand 400/500/600 stay identity.
const DARK_ACCENT_PINS = {
  "--color-accent-200": "#e4dcfc",
  "--color-accent-300": "#d2c4fa",
  "--color-accent-700": "#7a63e0",
  "--color-accent-800": "#6550c4",
  "--color-accent-900": "#52409e",
  "--color-accent-950": "#34265f",
};

// Dark-tuned amber so operator-note / warning banners are not sickly on cool ground.
const DARK_AMBER_PINS = {
  "--color-amber-200": "#e8d9a0",
  "--color-amber-300": "#dcc878",
  "--color-amber-800": "#6a5420",
  "--color-amber-900": "#55431c",
  "--color-amber-950": "#2c2412",
};

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(js|jsx)$/.test(name)) out.push(p);
  }
  return out;
}

/** Extract `--name: #hex` assignments from a CSS block string (case-insensitive hex). */
function propsIn(block) {
  const map = new Map();
  for (const m of block.matchAll(/(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\b/g)) {
    map.set(m[1], m[2].toLowerCase());
  }
  return map;
}

/** Slice the `{ … }` body of a CSS rule matched by `re` (brace-balanced). */
function blockFor(css, re, label) {
  const m = css.match(re);
  assert.ok(m, `expected ${label} in index.css`);
  const open = css.indexOf("{", m.index);
  assert.ok(open >= 0, `expected '{' after ${label}`);
  let depth = 0;
  for (let j = open; j < css.length; j++) {
    if (css[j] === "{") depth++;
    else if (css[j] === "}") {
      depth--;
      if (depth === 0) return css.slice(open + 1, j);
    }
  }
  assert.fail(`unclosed block for ${label}`);
}

function assertPins(label, actual, expected) {
  for (const [name, hex] of Object.entries(expected)) {
    assert.equal(
      actual.get(name),
      hex.toLowerCase(),
      `${label}: ${name} want ${hex.toLowerCase()}, got ${actual.get(name) ?? "(missing)"}`,
    );
  }
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
    const rel = relative(spaRoot, file);
    for (const m of text.matchAll(OFF_PALETTE)) {
      const line = text.slice(0, m.index).split("\n").length;
      hits.push(`${rel}:${line}: ${m[0]}`);
    }
  }
  assert.equal(hits.length, 0,
    `off-palette classes (DESIGN.md §1):\n  ${hits.join("\n  ")}`);
});

check("light @theme warm surface/neutral/accent anchors stay pinned", () => {
  const css = readFileSync(indexCss, "utf8");
  const theme = propsIn(blockFor(css, /@theme\s*\{/, "@theme"));
  assertPins("light @theme", theme, LIGHT_PINS);
});

check("dark surfaces are cool violet-tinted near-black", () => {
  const css = readFileSync(indexCss, "utf8");
  // Prefer html.dark remaps; fall back to @theme for dark-only surface tokens.
  const darkBlock = propsIn(blockFor(css, /html\.dark\s*\{/, "html.dark"));
  const theme = propsIn(blockFor(css, /@theme\s*\{/, "@theme"));
  const merged = new Map([...theme, ...darkBlock]);
  assertPins("dark surfaces", merged, DARK_SURFACE_PINS);
  // Warm charcoal must be gone from the effective dark ground.
  assert.notEqual(merged.get("--color-surface-dark"), "#141110");
  assert.notEqual(merged.get("--color-surface-raised-dark"), "#1d1917");
});

check("html.dark remaps neutrals to the cool ramp", () => {
  const css = readFileSync(indexCss, "utf8");
  const dark = propsIn(blockFor(css, /html\.dark\s*\{/, "html.dark"));
  assertPins("html.dark neutrals", dark, DARK_NEUTRAL_PINS);
});

check("html.dark lifts accent stops for text/chips (400/500/600 untouched in @theme)", () => {
  const css = readFileSync(indexCss, "utf8");
  const theme = propsIn(blockFor(css, /@theme\s*\{/, "@theme"));
  assert.equal(theme.get("--color-accent-400"), "#a28ef1");
  assert.equal(theme.get("--color-accent-500"), "#7b61dc");
  assert.equal(theme.get("--color-accent-600"), "#6042cc");
  const dark = propsIn(blockFor(css, /html\.dark\s*\{/, "html.dark"));
  assertPins("html.dark accent lift", dark, DARK_ACCENT_PINS);
});

check("html.dark retunes amber for cool ground", () => {
  const css = readFileSync(indexCss, "utf8");
  const dark = propsIn(blockFor(css, /html\.dark\s*\{/, "html.dark"));
  assertPins("html.dark amber", dark, DARK_AMBER_PINS);
});

check("dark-mode logo assets are white-violet (no cream stops)", () => {
  const whiteAssets = [
    "devcake-mark-white-transparent.svg",
    "devcake-wordmark-white-transparent.svg",
  ];
  for (const name of whiteAssets) {
    const path = join(assetsRoot, name);
    assert.ok(statSync(path).isFile(), `missing dark logo asset: ${name}`);
    const text = readFileSync(path, "utf8").toLowerCase();
    for (const stop of CREAM_LOGO_STOPS) {
      assert.ok(
        !text.includes(stop.toLowerCase()),
        `${name} still contains cream stop ${stop}`,
      );
    }
    assert.ok(text.includes("#ffffff"), `${name} must use pure white (#ffffff)`);
  }
});

if (failed) {
  console.error(`design_tokens.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("design_tokens.mjs: all checks passed");
