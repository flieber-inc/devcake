// CAKE-90: three REVIEW_LEDGER failure paths must surface honest UI error
// state instead of silent rejection or the wrong error channel.
// Fully route-mocked — no live backend required.
import { check, checked, gotoFresh, summary, withPage } from "./harness.mjs";

const REPO = {
  name: "demo",
  forge: "github",
  url: "https://github.com/acme/demo",
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
  repos: ["demo"],
  reference_repos: [],
  memory_repos: [],
  intake_paused: false,
  discovery_routing: true,
  assignments: {},
  managed: false,
};

async function mockDraftApis(page, { configExtras = {} } = {}) {
  const cfg = {
    pmos: [PMO],
    repos: [REPO],
    dismissed_alerts: [],
    ...configExtras,
  };
  await page.route(/\/api\/v1\/config$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify(cfg),
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
  await page.route(/\/api\/v1\/health$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        intake_paused: false,
        pmo_instances: { linear1: { intake_paused: false } },
        harness_pins: {},
      }),
    }));
  await page.route(/\/api\/v1\/connections\/registry$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        pmo_systems: [{ id: "linear", display_name: "Linear", supports_priority: true }],
        forges: [{ id: "github", display_name: "GitHub" }],
        secret_shape_prefixes: ["ghp_"],
        managed_labels_expected: 11,
      }),
    }));
  await page.route(/\/api\/v1\/secrets-check/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        conn: {
          "repo:demo:token": { present: true, updated_at: "2026-08-01T00:00:00Z" },
          "repo:demo:token_ro": { present: false },
          "repo:demo:reviewer_token": { present: false },
          "pmo:linear1:api_key": { present: true, updated_at: "2026-08-01T00:00:00Z" },
        },
      }),
    }));
  await page.route(/\/api\/v1\/internal-repos$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ repos: [], ui_url: null }),
    }));
}

// ── 1. SecretField: PUT/DELETE rejection shows field / dialog error ─────────
await withPage(async (page) => {
  await mockDraftApis(page);
  await page.route(/\/api\/v1\/secrets\/repo\/demo\/token(?:\?.*)?$/, async (route) => {
    const method = route.request().method();
    if (method === "PUT" || method === "DELETE") {
      return route.fulfill({
        status: 503, contentType: "application/json",
        body: JSON.stringify({ detail: "secret store unavailable" }),
      });
    }
    return route.fulfill({ status: 405, body: "method not allowed" });
  });

  await gotoFresh(page, "#/repos");
  await page.waitForSelector('h1:has-text("Repositories")');
  // draft names are empty on first paint, so cards often mount collapsed —
  // expand before looking for secret controls (same pattern as repos.mjs)
  if ((await page.locator('button[aria-label^="Collapse repository"]').count()) === 0) {
    await page.locator('[data-testid="repo-summary-row"]').first().click();
    await page.waitForTimeout(100);
  }
  await page.waitForSelector('input[aria-label="Access token"]', { timeout: 10000 });

  // Prefer Remove path (stored secret) — ConfirmDialog must carry the error.
  // Do NOT use bare button:has-text("Remove") — the repo card also has one.
  const removeBtn = page.locator('span:has-text("✓ stored")')
    .locator("xpath=..")
    .locator('button:has-text("Remove")');
  await checked("SecretField Remove control is present for a stored token",
    async () => (await removeBtn.count()) >= 1);
  if ((await removeBtn.count()) >= 1) {
    await removeBtn.first().click();
    await page.waitForSelector('[role="dialog"]:has-text("Remove the stored value")');
    await page.locator('[role="dialog"] button:has-text("Remove")').click();
    await checked("SecretField remove rejection surfaces in ConfirmDialog",
      async () => {
        // Dialog stays open with error= when catch is wired; silent finally
        // closes it — either way, finish within 5s.
        const deadline = Date.now() + 5000;
        while (Date.now() < deadline) {
          const dlg = page.locator('[role="dialog"]');
          if ((await dlg.count()) === 0) return false;
          const t = await dlg.innerText();
          if (/secret store unavailable|503/.test(t)) return true;
          await page.waitForTimeout(100);
        }
        return false;
      });
    if ((await page.locator('[role="dialog"]').count()) > 0) {
      await page.click('[role="dialog"] button:has-text("Cancel")');
      await page.waitForTimeout(100);
    }
  }

  // Set/Replace path: type a value, force PUT failure, expect field-local error.
  const tokenInput = page.locator('input[aria-label="Access token"]');
  await tokenInput.fill("ghp_testtoken_notreal_xxxxxxxxxxxx");
  await page.locator('button:has-text("Replace"), button:has-text("Set")').first().click();
  await checked("SecretField submit rejection shows field-local error",
    async () => {
      const deadline = Date.now() + 5000;
      while (Date.now() < deadline) {
        const main = await page.innerText("main");
        if (/secret store unavailable|503/.test(main)) return true;
        await page.waitForTimeout(100);
      }
      return false;
    });
});

// ── 2. Test connection: HTTP rejection writes testResult (✗ line) ───────────
await withPage(async (page) => {
  await mockDraftApis(page);
  await page.route(/\/api\/v1\/connections\/forge\/demo\/test$/, (route) =>
    route.fulfill({
      status: 502, contentType: "application/json",
      body: JSON.stringify({ detail: "forge unreachable" }),
    }));
  await page.route(/\/api\/v1\/connections\/pmo\/linear1\/test$/, (route) =>
    route.fulfill({
      status: 502, contentType: "application/json",
      body: JSON.stringify({ detail: "pmo unreachable" }),
    }));

  await gotoFresh(page, "#/repos");
  await page.waitForSelector('h1:has-text("Repositories")');
  if ((await page.locator('button[aria-label^="Collapse repository"]').count()) === 0) {
    await page.locator('[data-testid="repo-summary-row"]').first().click();
    await page.waitForTimeout(100);
  }
  await page.waitForSelector('button:has-text("Test connection")');
  await page.click('button:has-text("Test connection")');
  await checked("Repos Test connection rejection shows ✗ message",
    async () => {
      await page.locator("text=/forge unreachable|502/").first()
        .waitFor({ timeout: 5000 });
      const t = await page.innerText("main");
      return t.includes("✗") && /forge unreachable|502/.test(t);
    });

  await gotoFresh(page, "#/pmo");
  await page.waitForSelector('h1:has-text("PMO"), h1:has-text("Adapters"), #pmo', {
    timeout: 10000,
  }).catch(() => {});
  if ((await page.locator('button:has-text("Test connection")').count()) === 0) {
    const summary = page.locator('[data-testid="pmo-summary-row"]').first();
    if (await summary.count()) await summary.click();
    await page.waitForTimeout(100);
  }
  await page.waitForSelector('button:has-text("Test connection")');
  await page.click('button:has-text("Test connection")');
  await checked("PMO Test connection rejection shows ✗ message",
    async () => {
      await page.locator("text=/pmo unreachable|502/").first()
        .waitFor({ timeout: 5000 });
      const t = await page.innerText("main");
      return t.includes("✗") && /pmo unreachable|502/.test(t);
    });
});

// ── 3. CSV export failure: dedicated channel, does not pollute Stop-all ─────
await withPage(async (page) => {
  await page.route(/\/api\/v1\/runs(?:\?.*)?$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        total: 0, total_runs: 0, runs: [], totals: null,
        pmo_refs: [], rate_card: {},
      }),
    }));
  await page.route(/\/api\/v1\/runs\.csv/, (route) =>
    route.fulfill({
      status: 500, contentType: "application/json",
      body: JSON.stringify({ detail: "export boom" }),
    }));
  // draft APIs still fire from App chrome
  await mockDraftApis(page);

  await gotoFresh(page, "#/runs");
  await page.waitForSelector('h1:has-text("Runs")');
  await page.click('button[aria-label="More run actions"]');
  await page.click('[role="menuitem"]:has-text("Export to CSV")');

  await checked("CSV export failure banner names export (not Stop failed)",
    async () => {
      await page.locator("text=/export boom|Export failed|export failed/i").first()
        .waitFor({ timeout: 5000 });
      const main = await page.innerText("main");
      const namesExport = /Export failed|export failed|CSV/i.test(main)
        && /export boom|500/.test(main);
      const mislabeled = /Stop failed \(nothing was deleted\):\s*(?:500 )?export boom/i
        .test(main);
      return namesExport && !mislabeled;
    });

  await page.click('button[aria-label="More run actions"]');
  await page.click('[role="menuitem"]:has-text("Stop all runs")');
  await page.waitForSelector('[role="dialog"]:has-text("Stop all in-flight runs")');
  await checked("Stop-all ConfirmDialog does not carry the export error",
    async () => {
      const t = await page.locator('[role="dialog"]').innerText();
      return !/export boom/i.test(t) && !/Export failed/i.test(t);
    });
  await page.click('[role="dialog"] button:has-text("Cancel")');
});

summary("error_surface");
