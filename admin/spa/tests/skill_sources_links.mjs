// CAKE-165 — two-way links between Skill sources and the built-in store.
// Public seam: hash route + visible navigation/modals (mocked store-enabled).
import { checked, gotoFresh, summary, withPage } from "./harness.mjs";

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

async function mockApis(page, { storeEnabled = true } = {}) {
  const body = {
    pmos: [PMO],
    repos: [],
    skill_sources: [],
    dismissed_alerts: [],
    poll_interval_sec: 30,
    adoption_mode: "manual",
    attach_merged_changeset_to_pmo: false,
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
  await page.route(/\/api\/v1\/health$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        intake_paused: false,
        pmo_instances: {},
        harness_pins: {},
        active_runs: 0,
        internal_forge: true,
      }),
    }));
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
  await page.route(/\/api\/v1\/skills$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        skills: [
          {
            name: "pr-hygiene",
            description: "PR discipline",
            source: "store",
            builtin: true,
            origin: null,
          },
        ],
        store: storeEnabled
          ? { enabled: true, ok: true, detail: "", html_url: "http://gitea/skills" }
          : { enabled: false, ok: false, detail: "disabled", html_url: "" },
      }),
    }));
}

await withPage(async (page) => {
  await mockApis(page, { storeEnabled: true });

  // 1: Skill sources CTA → Fleet Skills with Add-skill auto-open
  await gotoFresh(page, "#/skill-sources");
  await page.waitForSelector("#skills-sources");

  const cta = page.locator('a[href="#/fleet/skills?add=1"]');
  await checked("Skill sources shows built-in-store CTA to #/fleet/skills?add=1", async () => {
    if ((await cta.count()) !== 1) return false;
    const text = ((await cta.evaluate((el) => el.closest("p")?.textContent
      || el.parentElement?.textContent || el.textContent)) || "");
    return /Keeping skills in DevCake itself\?/i.test(text)
      && /built-in store/i.test(text)
      && !/\blocal\b/i.test(text);
  });

  await cta.click();
  await page.waitForSelector("#skills", { timeout: 8000 });
  await checked("CTA lands on Fleet → Skills (flag cleared from URL)", async () => {
    const u = page.url();
    return u.includes("#/fleet/skills") && !u.includes("add=1");
  });
  await checked("Add-skill modal opens automatically after CTA", async () => {
    await page.waitForSelector('[role="dialog"] h4:has-text("Add skill")', { timeout: 8000 });
    return (await page.locator('[role="dialog"] h4:has-text("Add skill")').count()) === 1;
  });

  // 2: Reverse link inside Add-skill → Skill sources
  const reverse = page.locator('[role="dialog"] a[href="#/skill-sources"]');
  await checked("Add-skill modal links to Skill sources (external repository copy)", async () => {
    if ((await reverse.count()) < 1) return false;
    const text = ((await reverse.first().evaluate((el) =>
      el.closest("p")?.textContent || el.parentElement?.textContent || el.textContent)) || "");
    return /Skills live in an external repository\?/i.test(text)
      && /skill source/i.test(text)
      && !/\blocal\b/i.test(text);
  });

  await reverse.first().click();
  await page.waitForSelector("#skills-sources", { timeout: 8000 });
  await checked("Modal reverse link lands on Skill sources", async () => {
    return page.url().endsWith("#/skill-sources")
      && (await page.locator("#skills-sources").count()) === 1;
  });

  // 3: Deep-link peel — query flag must not redirect away from Skills
  await gotoFresh(page, "#/fleet/skills?add=1");
  await page.waitForSelector("#skills", { timeout: 8000 });
  await checked("#/fleet/skills?add=1 stays on Skills (router peels query)", async () => {
    return (await page.locator("#skills").count()) === 1
      && !page.url().includes("#/fleet/dev-types");
  });
  await checked("Deep-link auto-opens Add skill and clears add=1", async () => {
    await page.waitForSelector('[role="dialog"] h4:has-text("Add skill")', { timeout: 8000 });
    return (await page.locator('[role="dialog"] h4:has-text("Add skill")').count()) === 1
      && !page.url().includes("add=1");
  });
});

await withPage(async (page) => {
  // Store disabled: deep-link stays on Skills but does not force the modal
  await mockApis(page, { storeEnabled: false });
  await gotoFresh(page, "#/fleet/skills?add=1");
  await page.waitForSelector("#skills", { timeout: 8000 });
  await checked("store-disabled deep-link stays on Skills without Add-skill modal", async () => {
    await page.waitForTimeout(400);
    return (await page.locator("#skills").count()) === 1
      && (await page.locator('button:has-text("Add skill")').count()) === 0
      && (await page.locator('[role="dialog"] h4:has-text("Add skill")').count()) === 0;
  });
});

summary("skill_sources_links");
