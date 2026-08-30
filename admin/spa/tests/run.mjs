// Suite runner: boots vite (unless UI_BASE points elsewhere), reads
// ADMIN_USER/ADMIN_PASSWORD from the repo-root .env so the dev proxy can
// authenticate against the nginx container, runs every suite, aggregates.
//   npm run check:ui              → vite on :5199 against the live backend
//   UI_BASE=http://127.0.0.1:8080 npm run check:ui   → prod container direct
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const spa = join(here, "..");
const SUITES = [
  // Pure-node helper suites first (no browser, no backend): previously only
  // reachable via `npm run test:helpers`, which nothing invoked — including
  // the safeHref XSS guard (2026-08 evaluation). Now every check:ui run
  // carries them.
  "markdown_helpers.mjs",
  "format_helpers.mjs",
  "deps_helpers.mjs",
  "board_helpers.mjs",
  "config_draft.mjs",
  "contracts.mjs",
  "req_seq.mjs",
  "mission_identity.mjs",
  "alerts.mjs",
  "design_tokens.mjs",
  "list_window_helpers.mjs",
  "cake162_polish.mjs",
  "cake162_ui.mjs",
  "settings.mjs",
  "tasks.mjs",
  "hierarchy.mjs",
  "devtypes.mjs",
  "assignments.mjs",
  "redesign.mjs",
  "missions.mjs",
  "missions_deps.mjs",
  "new_mission.mjs",
  "profiles.mjs",
  "pmo_intake.mjs",
  "markdown.mjs",
  "prompts_overrides.mjs",
  "repos.mjs",
  "costs.mjs",
  "error_surface.mjs",
  "honesty_leftovers.mjs",
  "adapter_rename_ui.mjs",
];

// pick up admin credentials from the repo-root .env when not already set
try {
  const env = readFileSync(join(spa, "..", "..", ".env"), "utf8");
  for (const key of ["ADMIN_USER", "ADMIN_PASSWORD"]) {
    if (!process.env[key]) {
      const m = env.match(new RegExp(`^${key}=(.*)$`, "m"));
      if (m) process.env[key] = m[1].trim().replace(/^["']|["']$/g, "");
    }
  }
} catch { /* no .env — the proxy runs unauthenticated */ }

let vite = null;
const base = process.env.UI_BASE || "http://127.0.0.1:5199";

if (!process.env.UI_BASE) {
  // --host 127.0.0.1 forces IPv4 binding; without it, macOS vite listens on
  // ::1 only while the harness probes 127.0.0.1, and the readiness check
  // silently hits its 30s timeout.
  vite = spawn("npx", ["vite", "--host", "127.0.0.1", "--port", "5199", "--strictPort"], {
    cwd: spa, stdio: "ignore", env: process.env,
  });
  const deadline = Date.now() + 30000;
  let up = false;
  while (Date.now() < deadline) {
    try { up = (await fetch(base)).ok; if (up) break; }
    catch { await new Promise((r) => setTimeout(r, 300)); }
  }
  if (!up) {
    console.error("vite did not come up on :5199 within 30s");
    vite.kill();
    process.exit(1);
  }
}

let failed = false;
for (const suite of SUITES) {
  console.log(`\n── ${suite}`);
  const code = await new Promise((resolve) => {
    spawn(process.execPath, [join(here, suite)], { stdio: "inherit", env: process.env })
      .on("close", resolve);
  });
  if (code !== 0) failed = true;
}

vite?.kill();
process.exit(failed ? 1 : 0);
