// Cross-project responsive regressions. These checks observe public page
// controls and browser geometry; all API reads are deterministic fixtures.
import { check, gotoFresh, summary, withPage } from "./harness.mjs";

const config = {
  schema_version: 4,
  intake_paused: false,
  dismissed_alerts: [],
  repos: [],
  pmos: [],
  forges: [],
  adoption_mode: "auto",
  poll_interval_seconds: 30,
  concurrency: { global_max: 4 },
  dev_timeout_minutes: 60,
  review_loop_warning_every: 3,
  max_decomposition_depth: 2,
  relations_mapper: { enabled: false, interval_minutes: 60, dev_type: null },
};

async function mockApi(page) {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const response = path === "/config"
      ? config
      : path === "/health"
        ? { app: true, redis: true, dagu: true, openobserve: true, intake_paused: false }
        : path === "/runs"
          ? { total: 42, runs: [] }
          : path === "/internal-repos"
            ? { repos: [] }
            : path === "/dev-types"
            ? []
            : path === "/assignments"
              ? {}
              : path === "/harnesses"
                ? {}
                : path === "/connections/registry"
                  ? {
                      pmo_systems: [{ id: "linear", display_name: "Linear" }],
                      forges: [{ id: "github", display_name: "GitHub" }],
                      secret_shape_prefixes: ["ghp_", "lin_api_"],
                      managed_labels_expected: 10,
                    }
                  : path === "/skills"
                    ? { skills: [], store: null }
                    : {};
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(response) });
  });
}

await withPage(async (page) => {
  await mockApi(page);
  await gotoFresh(page, "#/runs");
  await page.waitForSelector('input[aria-label="Filter runs by mission key"]');

  const layout = await page.evaluate(() => {
    const main = document.querySelector("main");
    const input = document.querySelector('input[aria-label="Filter runs by mission key"]');
    const card = input.closest(".rounded-card");
    const controls = [
      input,
      [...card.querySelectorAll("button")].find((button) => button.textContent.includes("newer")),
      [...card.querySelectorAll("button")].find((button) => button.textContent.includes("older")),
    ].filter(Boolean).map((node) => node.getBoundingClientRect());
    const cardRect = card.getBoundingClientRect();
    return {
      mainFits: main.scrollWidth <= main.clientWidth + 1,
      bodyFits: document.body.scrollWidth <= innerWidth + 1,
      controlsFit: controls.every((rect) => rect.left >= cardRect.left - 1 && rect.right <= cardRect.right + 1),
      searchFits: controls[0].width <= cardRect.width,
      card: { left: cardRect.left, right: cardRect.right, width: cardRect.width },
      controls: controls.map(({ left, right, width }) => ({ left, right, width })),
    };
  });
  check("Runs toolbar reflows without page-level horizontal overflow",
    layout.mainFits && layout.bodyFits && layout.controlsFit && layout.searchFits,
    JSON.stringify(layout));
}, { width: 390, height: 844 });

await withPage(async (page) => {
  await mockApi(page);
  await page.addInitScript(() => localStorage.setItem("devcake-sidebar", "collapsed"));
  await gotoFresh(page, "#/config/limits");
  await page.waitForSelector("#limits");
  const chip = page.locator('main a[href="#/config/limits"]:visible', { hasText: "Limits" });
  check("collapsed desktop sidebar keeps Configuration sections reachable", await chip.count() === 1);
  const visible = await chip.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    const main = document.querySelector("main").getBoundingClientRect();
    return rect.left >= main.left && rect.right <= main.right;
  });
  check("desktop Configuration switcher stays inside the content viewport", visible);
}, { width: 1280, height: 720 });

await withPage(async (page) => {
  await mockApi(page);
  for (const route of ["overview", "runs", "repos", "logs"]) {
    await gotoFresh(page, `#/${route}`);
    await page.waitForSelector("main h1");
    const fit = await page.evaluate(() => {
      const main = document.querySelector("main");
      return {
        main: main.scrollWidth <= main.clientWidth + 1,
        body: document.body.scrollWidth <= innerWidth + 1,
      };
    });
    check(`${route} avoids page-level horizontal overflow on mobile`, fit.main && fit.body, JSON.stringify(fit));
  }
}, { width: 390, height: 844 });

summary("responsive");
