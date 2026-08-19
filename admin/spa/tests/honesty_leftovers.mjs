// CAKE-125: route-mocked honesty leftovers — backend-unreachable banner
// copy and Create-internal modal notebook conditioning. No live backend.
import { check, checked, gotoFresh, summary, withPage } from "./harness.mjs";

const INTERNAL_REPO = {
  name: "notes",
  forge: "gitea",
  url: "http://gitea:3000/devcake-repos/notes.git",
  api_base: null,
  default_branch: "main",
  auto_merge: false,
  auto_resolve_merge_conflicts: true,
  merge_retry_window_minutes: 30,
  merge_settle_minutes: 0,
};

const PMO = {
  name: "linear1",
  system: "linear",
  team_key: "DEV",
  api_base: null,
  repos: [],
  reference_repos: [],
  memory_repos: [],
  intake_paused: false,
  discovery_routing: true,
  assignments: {},
  managed: false,
};

async function mockChromeApis(page, { cfg, healthFail = false } = {}) {
  const body = {
    pmos: cfg?.pmos || [PMO],
    repos: cfg?.repos || [INTERNAL_REPO],
    dismissed_alerts: [],
    ...(cfg || {}),
  };
  await page.route(/\/api\/v1\/config$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify(body),
    }));
  await page.route(/\/api\/v1\/dev-types$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify([]),
    }));
  await page.route(/\/api\/v1\/assignments$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({}),
    }));
  await page.route(/\/api\/v1\/harnesses$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({}),
    }));
  if (healthFail) {
    await page.route(/\/api\/v1\/health$/, (route) =>
      route.fulfill({ status: 502, body: "backend down" }));
  } else {
    await page.route(/\/api\/v1\/health$/, (route) =>
      route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          intake_paused: false,
          pmo_instances: {},
          harness_pins: {},
          active_runs: 0,
        }),
      }));
  }
  await page.route(/\/api\/v1\/connections\/registry$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        pmo_systems: [{ id: "linear", display_name: "Linear", supports_priority: true }],
        forges: [
          { id: "github", display_name: "GitHub" },
          { id: "gitea", display_name: "Gitea" },
        ],
        secret_shape_prefixes: ["ghp_"],
        managed_labels_expected: 11,
      }),
    }));
  await page.route(/\/api\/v1\/secrets-check/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ conn: {} }),
    }));
  await page.route(/\/api\/v1\/internal-repos$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ repos: [] }),
    }));
  await page.route(/\/api\/v1\/runs(?:\?.*)?$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        total: 0, total_runs: 0, runs: [], totals: null,
        pmo_refs: [], rate_card: {},
      }),
    }));
}

// ── 1. Banner: honest "actions will fail" wording ──────────────────────────
await withPage(async (page) => {
  await mockChromeApis(page, { healthFail: true });
  await gotoFresh(page, "#/overview");
  await checked("backend-unreachable banner does not claim controls are disabled",
    async () => {
      await page.locator("text=/Backend unreachable/i").first()
        .waitFor({ timeout: 8000 });
      const t = await page.locator("body").innerText();
      return /Actions will fail until it responds/i.test(t)
        && !/Controls are disabled until it responds/i.test(t);
    });
});

// ── 2. Create-internal: unbound card omits notebook / claims-prune ─────────
await withPage(async (page) => {
  await mockChromeApis(page, {
    cfg: {
      pmos: [{ ...PMO, memory_repos: [] }],
      repos: [INTERNAL_REPO],
    },
  });
  await gotoFresh(page, "#/repos");
  await page.waitForSelector('h1:has-text("Repositories")');
  // Expand if collapsed
  const summaryRow = page.locator('[data-testid="repo-summary-row"]');
  if ((await summaryRow.count()) > 0) {
    await summaryRow.first().click();
    await page.waitForTimeout(100);
  }
  await page.waitForSelector('button:has-text("+ Create repository")');
  await page.click('button:has-text("+ Create repository")');
  await page.waitForSelector('[role="dialog"]:has-text("Create repository on the internal Gitea")');
  await checked("unbound Create-internal omits notebook / claims-prune copy",
    async () => {
      const t = await page.locator('[role="dialog"]').innerText();
      return /Clear will not delete it/i.test(t)
        && /stack wipe/i.test(t)
        && !/this notebook/i.test(t)
        && !/claims copied/i.test(t);
    });
  await page.click('[role="dialog"] button:has-text("Cancel")');
});

// ── 3. Create-internal: notebook-bound card keeps claims-prune sentence ────
await withPage(async (page) => {
  await mockChromeApis(page, {
    cfg: {
      pmos: [{ ...PMO, memory_repos: ["notes"] }],
      repos: [INTERNAL_REPO],
    },
  });
  await gotoFresh(page, "#/repos");
  await page.waitForSelector('h1:has-text("Repositories")');
  const summaryRow = page.locator('[data-testid="repo-summary-row"]');
  if ((await summaryRow.count()) > 0) {
    await summaryRow.first().click();
    await page.waitForTimeout(100);
  }
  await page.waitForSelector('button:has-text("+ Create repository")');
  await page.click('button:has-text("+ Create repository")');
  await page.waitForSelector('[role="dialog"]:has-text("Create repository on the internal Gitea")');
  await checked("notebook-bound Create-internal keeps claims-prune sentence",
    async () => {
      const t = await page.locator('[role="dialog"]').innerText();
      return /this notebook/i.test(t) && /claims/i.test(t)
        && /notes stay/i.test(t);
    });
  await page.click('[role="dialog"] button:has-text("Cancel")');
});

summary("honesty_leftovers");
