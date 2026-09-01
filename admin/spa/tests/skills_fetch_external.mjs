// Fetch-external entry on the Skills catalog (founder ask 2026-08-31): the
// page where operators SEE the catalog carries the action that refreshes
// external sources — POST /skills/sources/refresh + catalog reload. Honesty
// rule: a failed fetch surfaces the per-source reasons and never a green ✓.
// The action is independent of the built-in store (ghost button on
// store-disabled stacks).
import { check, checked, gotoFresh, summary, withPage } from "./harness.mjs";

let refreshPosts = 0;
let skillsGets = 0;
let refreshResponse = { ok: true, failures: {} };

async function mockApis(page, { storeEnabled = true, sources = true } = {}) {
  refreshPosts = 0;
  skillsGets = 0;
  await page.route(/\/api\/v1\/config$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        pmos: [], repos: [],
        skill_sources: sources
          ? [{ name: "flskills", forge: "gitlab",
               url: "https://gitlab.com/o/skills",
               default_branch: "main", subdir: "", backed_by: "" }]
          : [],
        crons: [], dismissed_alerts: [],
        poll_interval_seconds: 30, adoption_mode: "opt_in",
      }),
    }));
  for (const [re, body] of [
    [/\/api\/v1\/dev-types$/, []],
    [/\/api\/v1\/assignments$/, {}],
    [/\/api\/v1\/harnesses$/, {}],
    [/\/api\/v1\/health$/, {
      intake_paused: false, pmo_instances: {}, harness_pins: {},
      active_runs: 0, internal_forge: storeEnabled,
    }],
    [/\/api\/v1\/connections\/registry$/, {
      pmo_systems: [{ id: "linear", display_name: "Linear" }],
      forges: [{ id: "github", display_name: "GitHub" },
               { id: "gitlab", display_name: "GitLab" },
               { id: "gitea", display_name: "Gitea" }],
      secret_shape_prefixes: ["ghp_"],
      managed_labels_expected: 11,
    }],
    [/\/api\/v1\/secrets-check/, { conn: {} }],
    [/\/api\/v1\/internal-repos$/, { repos: [] }],
  ]) {
    await page.route(re, (route) =>
      route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify(body),
      }));
  }
  await page.route(/\/api\/v1\/skills$/, (route) => {
    skillsGets += 1;
    return route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        skills: sources
          ? [{ name: "flskills/flieber-engineer", description: "ext",
               source: "external", builtin: false, origin: "flskills" }]
          : [],
        store: storeEnabled
          ? { enabled: true, ok: true, detail: "",
              html_url: "http://gitea/skills" }
          : { enabled: false, ok: false, detail: "disabled", html_url: "" },
      }),
    });
  });
  await page.route(/\/api\/v1\/skills\/sources\/refresh$/, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    refreshPosts += 1;
    return route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify(refreshResponse),
    });
  });
}

await withPage(async (page) => {
  refreshResponse = { ok: true, failures: {} };
  await mockApis(page, { storeEnabled: true, sources: true });
  await gotoFresh(page, "#/fleet/skills");
  await page.waitForSelector("#skills");

  await page.locator('button[aria-label="More skill-store actions"]').click();
  const item = page.locator(
    '[role="menuitem"]:has-text("Fetch skills from external sources")');
  await checked("catalog ⋯ menu offers the fetch-external action", async () =>
    (await item.count()) === 1);

  const getsBefore = skillsGets;
  await item.click();
  await page.waitForSelector('text=✓ external skills fetched',
    { timeout: 8000 });
  check("fetch POSTs the refresh chokepoint once and reloads the catalog",
    refreshPosts === 1 && skillsGets > getsBefore);
});

await withPage(async (page) => {
  // honesty rule: per-source failure reasons, never a green ✓
  refreshResponse = {
    ok: false,
    failures: { flskills: "default-branch probe: returned error: 401" },
  };
  await mockApis(page, { storeEnabled: true, sources: true });
  await gotoFresh(page, "#/fleet/skills");
  await page.waitForSelector("#skills");
  await page.locator('button[aria-label="More skill-store actions"]').click();
  await page.locator(
    '[role="menuitem"]:has-text("Fetch skills from external sources")').click();
  await checked("a failed fetch names the source and shows no green pill", async () => {
    await page.waitForTimeout(400);
    const body = await page.locator("body").innerText();
    return /skill source fetch failed/.test(body)
      && /flskills/.test(body)
      && !/✓ external skills fetched/.test(body);
  });
});

await withPage(async (page) => {
  // store disabled: external sources are independent of the built-in store —
  // the action survives as a lone ghost button (DESIGN §3 single-action rule)
  refreshResponse = { ok: true, failures: {} };
  await mockApis(page, { storeEnabled: false, sources: true });
  await gotoFresh(page, "#/fleet/skills");
  await page.waitForSelector("#skills");
  await checked("store-disabled stack keeps a ghost fetch-external button", async () => {
    const ghost = page.locator(
      'button:text-is("Fetch skills from external sources")');
    if ((await ghost.count()) !== 1) return false;
    await ghost.click();
    await page.waitForSelector('text=✓ external skills fetched',
      { timeout: 8000 });
    return refreshPosts === 1;
  });
});

await withPage(async (page) => {
  // no sources configured: the menu keeps its store-only shape
  refreshResponse = { ok: true, failures: {} };
  await mockApis(page, { storeEnabled: true, sources: false });
  await gotoFresh(page, "#/fleet/skills");
  await page.waitForSelector("#skills");
  await page.locator('button[aria-label="More skill-store actions"]').click();
  await checked("no sources → no fetch-external item", async () =>
    (await page.locator(
      '[role="menuitem"]:has-text("Fetch skills from external sources")')
      .count()) === 0);
});

summary("skills_fetch_external");
