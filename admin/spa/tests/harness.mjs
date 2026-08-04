// Shared harness for the behavioral UI suite (DESIGN.md §8): playwright-core
// driving the locally cached Chromium headless shell — never downloads a
// browser. Target the vite dev server by default; point UI_BASE at
// http://127.0.0.1:8080 (with ADMIN_USER/ADMIN_PASSWORD exported) to run the
// same checks against the prod container.
import { chromium } from "playwright-core";
import { existsSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const BASE = process.env.UI_BASE || "http://127.0.0.1:5199";

function chromePath() {
  if (process.env.UI_CHROME) return process.env.UI_CHROME;
  // Playwright's browser cache lives under a different root on macOS than on
  // Linux, and the headless-shell exe has a different basename on each.
  const roots = [
    join(homedir(), ".cache", "ms-playwright"),                    // Linux
    join(homedir(), "Library", "Caches", "ms-playwright"),         // macOS
  ];
  const layouts = [
    ["chrome-linux", "headless_shell"],
    ["chrome-headless-shell-mac-arm64", "chrome-headless-shell"],
    ["chrome-headless-shell-mac-x64", "chrome-headless-shell"],
  ];
  for (const root of roots) {
    if (!existsSync(root)) continue;
    const revs = readdirSync(root)
      .filter((d) => d.startsWith("chromium_headless_shell-"))
      .sort().reverse();
    for (const rev of revs) {
      for (const [dir, exe] of layouts) {
        const p = join(root, rev, dir, exe);
        if (existsSync(p)) return p;
      }
    }
  }
  throw new Error(
    "no cached Chromium headless shell under ~/.cache/ms-playwright or " +
    "~/Library/Caches/ms-playwright — set UI_CHROME to a Chrome/Chromium executable",
  );
}

let passes = 0, failures = 0, skips = 0;

export function check(name, ok, detail = "") {
  if (ok) { passes += 1; console.log(`  ✓ ${name}`); }
  else { failures += 1; console.error(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`); }
}

// Predicate-with-containment (2026-08 evaluation): a throwing predicate —
// selector timeout, detached element — records a NAMED failure instead of
// crashing the file and silently skipping every later check.
export async function checked(name, fn, detail = "") {
  try {
    check(name, await fn(), detail);
  } catch (e) {
    check(name, false, String(e).split("\n")[0]);
  }
}

export function skip(name, why) {
  skips += 1;
  console.log(`  - ${name} (skipped: ${why})`);
}

export async function withPage(fn, { width = 1280, height = 900 } = {}) {
  const browser = await chromium.launch({ executablePath: chromePath() });
  const auth = process.env.ADMIN_USER && process.env.ADMIN_PASSWORD
    ? { username: process.env.ADMIN_USER, password: process.env.ADMIN_PASSWORD }
    : undefined;
  const ctx = await browser.newContext({
    viewport: { width, height },
    httpCredentials: auth,
    colorScheme: "light",
  });
  const page = await ctx.newPage();
  // Native window.confirm/prompt/alert are banned (DESIGN.md §5) — any one
  // that fires is an instant failure, wherever it came from.
  page.on("dialog", (d) => {
    check(`no native dialog (got ${d.type()}: "${d.message()}")`, false);
    d.dismiss().catch(() => {});
  });
  try {
    await fn(page);
  } catch (e) {
    // An escape from the suite body is a recorded failure, not a crash: the
    // suite's summary() still runs, later suites are unaffected, and the
    // report names what broke instead of a bare stack trace (2026-08
    // evaluation — a throw used to abort the file and silently skip every
    // remaining check).
    check("suite body ran to completion", false, String(e).split("\n")[0]);
  } finally {
    await browser.close();
  }
}

// Fresh-mount navigation: hash-only page.goto does NOT remount the app
// (DESIGN.md §8), so land on the URL and reload for a clean mount.
export async function gotoFresh(page, hash) {
  await page.goto(`${BASE}/${hash}`);
  await page.reload();
  await page.waitForSelector("main", { timeout: 10000 });
}

export function summary(suite) {
  const tail = skips ? `, ${skips} skipped` : "";
  console.log(`${suite}: ${passes} passed, ${failures} failed${tail}`);
  process.exitCode = failures ? 1 : 0;
}
