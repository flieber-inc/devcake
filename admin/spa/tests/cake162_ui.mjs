// CAKE-162 browser smoke — route-mocked (no live backend). Pins heading
// dedupe, chip-strip wrap (no horizontal scroll), SettingRow proximity,
// and SelectionChips add-list at 160-repo scale.
import { check, checked, gotoFresh, summary, withPage } from "./harness.mjs";

function repo(i) {
  return {
    name: `repo-${String(i).padStart(3, "0")}`,
    forge: "gitea",
    url: `http://gitea:3000/org/repo-${i}.git`,
    api_base: null,
    default_branch: "main",
    auto_merge: false,
    auto_resolve_merge_conflicts: true,
    merge_retry_window_minutes: 30,
    merge_settle_minutes: 0,
  };
}

const REPOS = Array.from({ length: 160 }, (_, i) => repo(i + 1));
const PMO = {
  name: "linear1",
  system: "linear",
  team_key: "DEV",
  api_base: null,
  repos: ["repo-001", "repo-002"],
  reference_repos: [],
  memory_repos: [],
  intake_paused: false,
  discovery_routing: true,
  assignments: {},
  managed: false,
};

async function mockChromeApis(page) {
  const body = {
    pmos: [PMO],
    repos: REPOS,
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
}

await withPage(async (page) => {
  await mockChromeApis(page);
  await gotoFresh(page, "#/repos");
  await page.waitForSelector("#repository");

  await checked("Repos page has exactly one Repositories h1", async () => {
    const n = await page.locator('h1:has-text("Repositories")').count();
    return n === 1;
  });
  await checked("Repos Section title is Forge connections, not a second Repositories", async () => {
    const h3 = page.locator("#repository h3");
    const t = ((await h3.textContent()) || "").trim();
    return t.startsWith("Forge connections")
      && (await page.locator('#repository h3:has-text("Repositories")').count()) === 0;
  });
}, { width: 1440, height: 900 });

await withPage(async (page) => {
  await mockChromeApis(page);
  await gotoFresh(page, "#/pmo");
  await page.waitForSelector("#pmo");

  await checked("PMO page has exactly one PMO h1", async () => {
    return (await page.locator('h1:has-text("PMO")').count()) === 1;
  });
  await checked("PMO Section title is Watched teams", async () => {
    const t = ((await page.locator("#pmo h3").first().textContent()) || "").trim();
    return t.startsWith("Watched teams");
  });

  // Expand first card to reach repo pickers
  const expand = page.locator('button[data-testid="pmo-summary-row"]').first();
  if (await expand.count()) await expand.click();
  await page.waitForSelector('#pmo input[aria-label^="Search "]', { timeout: 8000 });

  await checked("160-repo PMO pickers show search + add-list (not chip catalog only)", async () => {
    const searches = await page.locator('#pmo input[aria-label^="Search "]').count();
    const lists = await page.locator('#pmo [data-testid="selection-chips-add-list"]').count();
    return searches >= 3 && lists >= 3;
  });
  await checked("add-list is vertically bounded (max-height scroll, not page-width chip strip)", async () => {
    return page.locator('#pmo [data-testid="selection-chips-add-list"]').first()
      .evaluate((el) => {
        const style = getComputedStyle(el);
        const maxH = parseFloat(style.maxHeight);
        return Number.isFinite(maxH) && maxH > 0 && el.scrollHeight >= el.clientHeight;
      });
  });
}, { width: 1440, height: 900 });

await withPage(async (page) => {
  await mockChromeApis(page);
  await gotoFresh(page, "#/config/skills");
  await page.waitForSelector('a[href="#/config/skills"]');

  await checked("Config chip strip does not scroll horizontally at 390px", async () => {
    const strip = page.locator("main .sticky.top-0").first();
    await strip.waitFor();
    return strip.evaluate((el) => el.scrollWidth <= el.clientWidth + 1);
  });
}, { width: 390, height: 844 });

await withPage(async (page) => {
  await mockChromeApis(page);
  await gotoFresh(page, "#/repos");
  await page.waitForSelector("#repository");

  await checked("AdapterTabs strip does not scroll horizontally at 390px", async () => {
    const strip = page.locator("main .sticky.top-0").first();
    await strip.waitFor();
    return strip.evaluate((el) => el.scrollWidth <= el.clientWidth + 1);
  });
}, { width: 390, height: 844 });

await withPage(async (page) => {
  await mockChromeApis(page);
  await gotoFresh(page, "#/pmo");
  await page.waitForSelector("#pmo");
  const expand = page.locator('button[data-testid="pmo-summary-row"]').first();
  if (await expand.count()) await expand.click();
  // Discovery routing SettingRow is always on an expanded PMO card
  await page.waitForSelector('#pmo :text("Discovery routing")', { timeout: 8000 });

  await checked("SettingRow control sits near its label (no full-pane gulf)", async () => {
    return page.evaluate(() => {
      const row = [...document.querySelectorAll("#pmo div")].find((d) => {
        const cls = d.className || "";
        return cls.includes("sm:flex-row") && cls.includes("sm:items-center")
          && cls.includes("py-3") && d.textContent.includes("Discovery routing");
      });
      if (!row) return false;
      if ((row.className || "").includes("justify-between")) return false;
      const kids = [...row.children];
      if (kids.length < 2) return false;
      const a = kids[0].getBoundingClientRect();
      const b = kids[1].getBoundingClientRect();
      // Control should start shortly after the label — not at the far right of a 72rem pane
      const gap = b.left - a.right;
      return gap >= 0 && gap < 120;
    });
  });
}, { width: 1440, height: 900 });

summary("cake162_ui");
