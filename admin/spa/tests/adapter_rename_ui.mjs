// CAKE-156: route-mocked UI — per-card MoreMenu → PromptDialog rename
// dirties the draft and SaveReview shows the name change. No live backend.
import { check, checked, gotoFresh, summary, withPage } from "./harness.mjs";

const REPO = {
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
  repos: ["notes"],
  reference_repos: [],
  memory_repos: [],
  intake_paused: false,
  discovery_routing: true,
  assignments: {},
  managed: false,
};

const MANAGED = {
  ...PMO,
  name: "board",
  system: "gitea_issues",
  team_key: "devcake",
  managed: true,
  repos: [],
};

let liveCfg = {
  pmos: [PMO, MANAGED],
  repos: [REPO],
  crons: [],
  dismissed_alerts: [],
  poll_interval_seconds: 30,
  adoption_mode: "opt_in",
};

async function mockApis(page) {
  await page.route(/\/api\/v1\/config$/, async (route) => {
    if (route.request().method() === "PUT") {
      const patch = route.request().postDataJSON() || {};
      liveCfg = { ...liveCfg, ...patch };
      return route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify(liveCfg),
      });
    }
    return route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify(liveCfg),
    });
  });
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
        pmo_instances: {
          linear1: { intake_paused: false },
          board: { intake_paused: false },
        },
        internal_forge: true,
        harness_pins: {},
        active_runs: 0,
      }),
    }));
  await page.route(/\/api\/v1\/connections\/registry$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        pmo_systems: [
          { id: "linear", display_name: "Linear", supports_priority: true },
          { id: "gitea_issues", display_name: "Gitea Issues", supports_priority: false },
        ],
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

async function renameViaCardMenu(page, cardName, newName) {
  const more = page.locator(`button[aria-label="More actions for ${cardName}"]`).first();
  await more.click();
  await page.locator('[role="menuitem"]:has-text("Rename adapter")').click();
  const dialog = page.locator('[role="dialog"]:has-text("Rename adapter")');
  await dialog.waitFor({ timeout: 8000 });
  const hint = await dialog.locator("span.text-xs, label span").allInnerTexts();
  const hintText = hint.join(" ");
  await dialog.locator("input").fill(newName);
  await dialog.locator('button:has-text("Rename")').click();
  await dialog.waitFor({ state: "detached", timeout: 8000 }).catch(() => {});
  return hintText;
}

// ── 1. Repo: collapsed ⋯ → Rename → DirtyBar → SaveReview → Save ───────────
await withPage(async (page) => {
  liveCfg = {
    pmos: [PMO, MANAGED],
    repos: [REPO],
    crons: [],
    dismissed_alerts: [],
    poll_interval_seconds: 30,
    adoption_mode: "opt_in",
  };
  await mockApis(page);
  await gotoFresh(page, "#/repos");
  await page.waitForSelector('h1:has-text("Repositories")');

  // Force collapsed summary so the sibling ⋯ is exercised.
  const collapse = page.locator('button[aria-label^="Collapse repository"]');
  if (await collapse.count()) await collapse.first().click();
  await page.waitForSelector('[data-testid="repo-summary-row"]');

  // Dialog-side validation (review round): a draft rename has no server
  // round-trip to reject a bad name — the dialog itself must refuse and
  // stay open, never close over a doomed Save.
  await page.locator('button[aria-label="More actions for notes"]').first().click();
  await page.locator('[role="menuitem"]:has-text("Rename adapter")').click();
  const vDialog = page.locator('[role="dialog"]:has-text("Rename adapter")');
  await vDialog.waitFor({ timeout: 8000 });
  await vDialog.locator("input").fill("Not-Valid");
  await vDialog.locator('button:has-text("Rename")').click();
  await checked("invalid name keeps dialog open with the name rule", async () => {
    await page.waitForTimeout(200);
    const open = (await vDialog.count()) >= 1;
    const err = await vDialog.innerText();
    return open && /lowercase letter/.test(err);
  });
  await vDialog.locator('button:has-text("Cancel")').click();
  await vDialog.waitFor({ state: "detached", timeout: 8000 }).catch(() => {});

  const hint = await renameViaCardMenu(page, "notes", "notebook");
  check("repo PromptDialog hint says applies on Save (not immediately)",
    /applies on Save/i.test(hint) && !/Renames immediately/i.test(hint));

  await checked("repo rename dirties the draft (DirtyBar)", async () => {
    await page.waitForSelector(':text("Unsaved changes")', { timeout: 8000 });
    return (await page.locator(':text("Unsaved changes")').count()) >= 1;
  });

  await page.locator('button:has-text("Save changes…")').click();
  await page.waitForSelector('[role="dialog"]:has-text("Review")', { timeout: 8000 });
  const reviewText = await page.locator('[role="dialog"]').innerText();
  check("SaveReview shows repo name notes → notebook",
    /notes/.test(reviewText) && /notebook/.test(reviewText));

  await page.locator('[role="dialog"] button:has-text("Save")').click();
  await checked("repo rename Save completes (saved flash or clean draft)", async () => {
    await page.waitForTimeout(500);
    const saved = (await page.locator(':text("All changes saved")').count()) >= 1;
    const dirty = (await page.locator(':text("Unsaved changes")').count()) >= 1;
    return saved || !dirty;
  });
  check("config PUT carried renamed repo", liveCfg.repos?.[0]?.name === "notebook");
});

// ── 2. Non-managed PMO rename; managed board has no Rename item ────────────
await withPage(async (page) => {
  liveCfg = {
    pmos: [
      { ...PMO, name: "linear1", repos: [] },
      { ...MANAGED },
    ],
    repos: [{ ...REPO, name: "notes" }],
    crons: [{ id: "sweep", reserved: false, pmo: "linear1", enabled: true,
      interval_minutes: 60, description_template: "sweep" }],
    dismissed_alerts: [],
    poll_interval_seconds: 30,
    adoption_mode: "opt_in",
  };
  await mockApis(page);
  await gotoFresh(page, "#/pmo");
  await page.waitForSelector("#pmo");

  // Managed board: no Rename (and no Remove while internal_forge) → no empty ⋯.
  check("managed board has no per-card MoreMenu",
    (await page.locator('button[aria-label="More actions for board"]').count()) === 0);

  // Prefer expanded-card menu (≤3 PMOs start expanded); still covers the seam.
  const hint = await renameViaCardMenu(page, "linear1", "team_a");
  check("PMO PromptDialog hint says applies on Save (not immediately)",
    /applies on Save/i.test(hint) && !/Renames immediately/i.test(hint));

  await checked("PMO rename dirties the draft (DirtyBar)", async () => {
    await page.waitForSelector(':text("Unsaved changes")', { timeout: 8000 });
    return (await page.locator(':text("Unsaved changes")').count()) >= 1;
  });

  await page.locator('button:has-text("Save changes…")').click();
  await page.waitForSelector('[role="dialog"]:has-text("Review")', { timeout: 8000 });
  const reviewText = await page.locator('[role="dialog"]').innerText();
  check("SaveReview shows PMO name linear1 → team_a",
    /linear1/.test(reviewText) && /team_a/.test(reviewText));

  check("SaveReview mentions cascaded cron board target or PMO rename",
    /team_a/.test(reviewText));

  await page.locator('[role="dialog"] button:has-text("Save")').click();
  await checked("PMO rename Save completes", async () => {
    await page.waitForTimeout(500);
    const saved = (await page.locator(':text("All changes saved")').count()) >= 1;
    const dirty = (await page.locator(':text("Unsaved changes")').count()) >= 1;
    return saved || !dirty;
  });
  check("config PUT carried renamed PMO",
    (liveCfg.pmos || []).some((p) => p.name === "team_a"));
  check("cron pmo citation cascaded on Save payload",
    (liveCfg.crons || []).some((c) => c.pmo === "team_a"));
});

// ── 3. Collapsed PMO summary ⋯ opens rename without expand-only dead-end ───
await withPage(async (page) => {
  liveCfg = {
    pmos: [
      { ...PMO, name: "alpha", repos: [] },
      { ...PMO, name: "beta", repos: [] },
      { ...PMO, name: "gamma", repos: [] },
      { ...PMO, name: "delta", repos: [] },
    ],
    repos: [{ ...REPO, name: "notes" }],
    crons: [],
    dismissed_alerts: [],
    poll_interval_seconds: 30,
    adoption_mode: "opt_in",
  };
  await mockApis(page);
  await gotoFresh(page, "#/pmo");
  await page.waitForSelector('[data-testid="pmo-summary-row"]', { timeout: 8000 });
  check("4+ PMOs seed collapsed summary rows",
    (await page.locator('[data-testid="pmo-summary-row"]').count()) >= 4);

  const beforeExpand = await page.locator('button[aria-label^="Collapse PMO"]').count();
  await page.locator('button[aria-label="More actions for alpha"]').first().click();
  await page.locator('[role="menuitem"]:has-text("Rename adapter")').click();
  await page.waitForSelector('[role="dialog"]:has-text("Rename adapter")', { timeout: 8000 });
  const afterExpand = await page.locator('button[aria-label^="Collapse PMO"]').count();
  check("collapsed-row ⋯ opens PromptDialog without expanding the card",
    (await page.locator('[role="dialog"]:has-text("Rename adapter")').count()) === 1
    && afterExpand === beforeExpand);
  await page.locator('[role="dialog"] button:has-text("Cancel")').click();
});

summary("adapter_rename_ui.mjs");
