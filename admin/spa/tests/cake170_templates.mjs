// CAKE-170: Manage-templates Duplicate-to-edit + viewport-bounded scroll.
// Route-mocked — no live backend (same idiom as honesty_leftovers.mjs).
import { check, checked, gotoFresh, summary, withPage } from "./harness.mjs";

const LONG_BODY = [
  "## Role",
  "",
  "You are the DevCake Implementer for `{mission_key}`.",
  "",
  "## Steps",
  "",
  ...Array.from({ length: 40 }, (_, i) => `${i + 1}. Do step ${i + 1} carefully.`),
  "",
  "Use `{decomposition_rule}` when depth requires it.",
].join("\n");

const MISSION_TYPES = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];

function builtinCatalog(extraByType = {}) {
  const templates = {};
  for (const mt of MISSION_TYPES) {
    templates[mt] = [
      { name: "Development", template: LONG_BODY, builtin: true },
      ...(extraByType[mt] || []),
    ];
  }
  return {
    variables: Object.fromEntries(
      MISSION_TYPES.map((mt) => [mt, ["mission_key", "decomposition_rule"]]),
    ),
    templates,
    active: Object.fromEntries(MISSION_TYPES.map((mt) => [mt, "Development"])),
    dev_types: {},
    active_dev: {},
  };
}

let catalog = builtinCatalog();

async function mockApis(page) {
  catalog = builtinCatalog();
  await page.route(/\/api\/v1\/config$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        pmos: [],
        repos: [],
        active_prompt_templates: {},
        active_devtype_prompts: {},
        max_decomposition_depth: 3,
        dismissed_alerts: [],
      }),
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
        pmo_instances: {},
        harness_pins: {},
        active_runs: 0,
      }),
    }));
  await page.route(/\/api\/v1\/connections\/registry$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        pmo_systems: [], forges: [],
        secret_shape_prefixes: [], managed_labels_expected: 0,
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
  await page.route(/\/api\/v1\/prompt-templates$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify(catalog),
    }));
  await page.route(/\/api\/v1\/prompt-templates\/[^/]+\/[^/?]+$/, async (route) => {
    if (route.request().method() !== "PUT") {
      return route.continue();
    }
    const url = route.request().url();
    const m = url.match(/\/prompt-templates\/([^/]+)\/([^/?]+)$/);
    const mt = decodeURIComponent(m[1]);
    const name = decodeURIComponent(m[2]);
    const body = JSON.parse(route.request().postData() || "{}");
    const list = catalog.templates[mt] || (catalog.templates[mt] = []);
    const idx = list.findIndex((t) => t.name === name);
    const entry = { name, template: body.template || "", builtin: false };
    if (idx >= 0) list[idx] = entry;
    else list.push(entry);
    await route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ mission_type: mt, name, saved: true }),
    });
  });
}

await withPage(async (page) => {
  await mockApis(page);
  await page.setViewportSize({ width: 1366, height: 768 });
  await gotoFresh(page, "#/fleet/prompts");
  await page.waitForSelector('[data-testid="manage-templates"]', { timeout: 12000 });
  await page.locator('[data-testid="manage-templates"]').click();
  const dlg = page.locator('[role="dialog"]').filter({ hasText: "Manage templates" });
  await dlg.waitFor({ timeout: 8000 });

  await checked("Manage templates dialog is viewport-bounded (max-height ≤ 85vh)",
    async () => {
      const box = await dlg.boundingBox();
      return !!box && box.height <= 768 * 0.85 + 2;
    });

  await checked("Manage templates dialog scrolls internally when tall",
    async () => {
      return await dlg.evaluate((el) => {
        const style = getComputedStyle(el);
        const scrollable = el.scrollHeight > el.clientHeight + 1
          && (style.overflowY === "auto" || style.overflowY === "scroll"
            || style.overflow === "auto" || style.overflow === "scroll");
        return scrollable || el.scrollHeight <= el.clientHeight + 1;
      });
    });

  await checked("built-in shows Duplicate to edit",
    async () => (await dlg.locator('[data-testid="duplicate-to-edit"]').count()) === 1);

  await dlg.locator('[data-testid="duplicate-to-edit"]').click();
  const prompt = page.locator('[role="dialog"]').filter({ hasText: 'Duplicate "Development"' });
  await prompt.waitFor({ timeout: 5000 });

  await checked("Duplicate opens PromptDialog (no window.prompt)",
    async () => (await prompt.locator('button:has-text("Create copy")').count()) === 1);

  const nameInput = prompt.locator("input").first();
  await nameInput.fill("Development-custom");
  await prompt.locator('button:has-text("Create copy")').click();

  await checked("lands on editable Source for the operator copy",
    async () => {
      await dlg.locator('[data-testid="duplicate-active-hint"]').waitFor({ timeout: 8000 });
      const ta = dlg.locator("textarea[aria-label='Template source']");
      await ta.waitFor({ timeout: 5000 });
      const selected = await dlg.locator('select[aria-label="Template name"]').inputValue();
      return selected === "Development-custom" && (await ta.count()) === 1;
    });

  await checked("active-selection hint names page Save",
    async () => {
      const t = await dlg.locator('[data-testid="duplicate-active-hint"]').innerText();
      return /page Save/i.test(t) && /not active/i.test(t);
    });

  await dlg.locator("textarea[aria-label='Template source']")
    .fill(LONG_BODY + "\n\n# operator edit\n");
  await dlg.locator('button:has-text("Save template")').click();
  await page.waitForTimeout(300);

  await checked("Save template stays enabled on the operator copy",
    async () => {
      const btn = dlg.locator('button:has-text("Save template")');
      return (await btn.count()) === 1 && !(await btn.isDisabled());
    });

  // Narrow viewport sanity
  await page.setViewportSize({ width: 390, height: 700 });
  await page.waitForTimeout(100);
  await checked("narrow viewport: dialog still viewport-bounded",
    async () => {
      const box = await dlg.boundingBox();
      return !!box && box.height <= 700 * 0.85 + 2;
    });

  await dlg.locator('button:text-is("Close")').click();
  await page.waitForTimeout(100);
}, { width: 1366, height: 768 });

// Skills View shares Modal — same scroll trap before CAKE-170.
await withPage(async (page) => {
  await mockApis(page);
  const longSkill = Array.from({ length: 50 }, (_, i) => `Line ${i + 1}`).join("\n");
  await page.route(/\/api\/v1\/skills$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        skills: [
          { name: "pr-hygiene", description: "PR hygiene", source: "store",
            builtin: false, files: 1 },
        ],
        store: { enabled: true, ok: true, html_url: null },
      }),
    }));
  await page.route(/\/api\/v1\/skills\/pr-hygiene$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        name: "pr-hygiene",
        description: "PR hygiene",
        source: "store",
        builtin: false,
        files: [{ path: "SKILL.md", content: `# Skill\n\n${longSkill}` }],
      }),
    }));
  await page.setViewportSize({ width: 1366, height: 768 });
  await gotoFresh(page, "#/fleet/skills");
  await page.waitForSelector('button[aria-label="View skill pr-hygiene"]', {
    timeout: 12000,
  });
  await page.locator('button[aria-label="View skill pr-hygiene"]').click();
  const skillDlg = page.locator('[role="dialog"]').first();
  await skillDlg.waitFor({ timeout: 8000 });
  await checked("Skills View dialog is viewport-bounded",
    async () => {
      const box = await skillDlg.boundingBox();
      return !!box && box.height <= 768 * 0.85 + 2;
    });
}, { width: 1366, height: 768 });

summary("cake170_templates");
