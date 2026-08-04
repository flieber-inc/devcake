// Dependency chain panel in the mission drawer — read-only render of the
// poll snapshot: three bands (Blocked by / this mission / Blocks), honest
// unresolved + guard-label + cycle treatment, chain navigation with a
// composer-safe drawer remount, project blind-spot line, 390px reachability.
// Fully route-mocked — never a real save.
import { check, checked, gotoFresh, summary, withPage } from "./harness.mjs";

const RAW_ID = "01f0a3c4-dead-beef-aaaa-000000000000";

function row(pmo_id, key, extra = {}) {
  return {
    pmo_id, key,
    instance: "linear1", team: "DEV", kind: "issue",
    title: `${key} title for the chain panel`,
    status: "backlog", labels: ["DEVCAKE"], mission_type: "ONBOARD",
    priority: "medium", repo: "acme/webapp",
    url: `https://linear.app/example/issue/${pmo_id}`,
    updated_at: new Date(Date.now() - 3_600_000).toISOString(),
    schedulable: false, reason: "", blocked_by: [],
    ...extra,
  };
}

const ROWS = [
  row("A1", "DEV-1", { status: "done" }),
  row("A2", "DEV-2", {
    status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-EXECUTE"],
    mission_type: "EXECUTE", blocked_by: ["DEV-1", RAW_ID],
  }),
  row("A3", "DEV-3", { blocked_by: ["DEV-2"], reason: "blocked by DEV-2" }),
  row("A4", "DEV-4", {
    status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-FAILED"],
    mission_type: null,
  }),
  row("A5", "DEV-5", { blocked_by: ["DEV-4"], reason: "blocked by DEV-4" }),
  row("A7", "DEV-7", { blocked_by: ["DEV-8"] }),
  row("A8", "DEV-8", { blocked_by: ["DEV-7"] }),
  row("P1", "PRJ-demo", { kind: "project", mission_type: "ONBOARD" }),
  row("A9", "DEV-9", { status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-EXECUTE"], mission_type: "EXECUTE" }),
];

async function mockRoutes(page) {
  await page.route(/\/api\/v1\/missions(?:\?.*)?$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        missions: ROWS, adoption_mode: "auto", teams: { linear1: "DEV" },
      }),
    }));
  // anchored: never matches /health/live
  await page.route(/\/api\/v1\/health$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        last_poll_at: new Date().toISOString(),
        poll_interval_seconds: 30, poll_degraded: {},
        dependency_cycles: [["DEV-7", "DEV-8"]],
      }),
    }));
}

async function openDrawer(page, key) {
  await page.locator(`[data-testid="mission-list"] [role="button"]:has-text("${key} title")`)
    .first().click();
  await page.waitForSelector('button[aria-label="Close drawer"]', { timeout: 5000 });
}

async function closeDrawer(page) {
  await page.click('button[aria-label="Close drawer"]');
  await page.waitForTimeout(100);
}

await withPage(async (page) => {
  await mockRoutes(page);
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('[data-testid="mission-list"] [role="button"]');

  // DEV-2: one resolved + one raw blocker, one dependent
  await openDrawer(page, "DEV-2");
  await checked("panel renders with both bands for DEV-2", async () => {
    await page.waitForSelector('[data-testid="dep-panel"]', { timeout: 5000 });
    // band headings are CSS-uppercased — innerText returns the rendered form
    const blockers = (await page.locator('[data-testid="dep-blockers"]').innerText()).toLowerCase();
    const deps = (await page.locator('[data-testid="dep-dependents"]').innerText()).toLowerCase();
    return blockers.includes("blocked by (2)") && deps.includes("blocks (1)");
  });
  check("done blocker says it no longer blocks",
    (await page.locator('[data-testid="dep-blockers"]').innerText())
      .includes("Done — no longer blocking"));
  check("raw vendor id renders as an inert unresolved node",
    (await page.locator('[data-testid="dep-blockers"]').innerText())
      .includes("no longer in any snapshot")
    && (await page.locator(`[data-testid="dep-blockers"] button:has-text("${RAW_ID}")`).count()) === 0);
  check("the current mission band is marked",
    (await page.locator('[data-testid="dep-current"]').innerText())
      .toLowerCase().includes("this mission"));
  check("Manage in PMO links the mission's own PMO page",
    (await page.locator('[data-testid="dep-panel"] a:has-text("Manage in PMO")')
      .getAttribute("href")) === "https://linear.app/example/issue/A2");
  check("the panel is read-only — helper line owns the truth",
    (await page.locator('[data-testid="dep-panel"]').innerText())
      .includes("DevCake never adds or removes relations"));

  // chain navigation: composer text must never survive into another mission
  await page.fill('textarea[aria-label="Steering comment"]', "half-typed guidance");
  await page.locator('[data-testid="dep-blockers"] button:has-text("DEV-1")').click();
  await checked("clicking a chain node swaps the drawer to that mission", async () => {
    await page.waitForSelector('[role="dialog"] span.font-mono:has-text("DEV-1")',
      { timeout: 5000 });
    return true;
  });
  check("the composer resets on chain navigation (remount)",
    (await page.locator('textarea[aria-label="Steering comment"]').inputValue()) === "");
  await closeDrawer(page);

  // guard-labelled blocker: amber, names the label, states the deadlock
  await openDrawer(page, "DEV-5");
  await checked("FAILED blocker names its guard label and the deadlock", async () => {
    await page.waitForSelector('[data-testid="dep-blockers"]', { timeout: 5000 });
    return (await page.locator('[data-testid="dep-blockers"]').innerText())
      .includes("failed (DEVCAKE-FAILED) — blocks until a human acts");
  });
  await closeDrawer(page);

  // cycle: amber panel line with the honest "never start" copy
  await openDrawer(page, "DEV-7");
  await checked("cycle members get the will-never-start line", async () => {
    await page.waitForSelector('[data-testid="dep-panel"]', { timeout: 5000 });
    return (await page.locator('[data-testid="dep-panel"]').innerText())
      .includes("will never start until a relation is deleted in your PMO");
  });
  await closeDrawer(page);

  // transitive chain behind a disclosure
  await openDrawer(page, "DEV-3");
  await checked("Full chain discloses the transitive blocker", async () => {
    await page.waitForSelector('[data-testid="dep-panel"] summary', { timeout: 5000 });
    const summaryText = await page.locator('[data-testid="dep-panel"] summary').innerText();
    await page.locator('[data-testid="dep-panel"] summary').click();
    const disclosed = await page.locator('[data-testid="dep-panel"] details').innerText();
    return summaryText.includes("Full chain (1 more)") && disclosed.includes("DEV-1");
  });
  await closeDrawer(page);

  // projects: the blind-spot line, never fake "no dependencies"
  await openDrawer(page, "PRJ-demo");
  await checked("project drawer states the relations blind spot", async () => {
    await page.waitForSelector('[data-testid="dep-panel"]', { timeout: 5000 });
    const txt = await page.locator('[data-testid="dep-panel"]').innerText();
    return txt.includes("Projects carry no dependency relations")
      && (await page.locator('[data-testid="dep-blockers"]').count()) === 0;
  });
  await closeDrawer(page);

  // edge-free mission: no panel at all
  await openDrawer(page, "DEV-9");
  check("edge-free mission renders no panel",
    (await page.locator('[data-testid="dep-panel"]').count()) === 0);
  await closeDrawer(page);
});

// 390×844: the panel must not push Send guidance out of reach, and the
// drawer body must not scroll horizontally
await withPage(async (page) => {
  await mockRoutes(page);
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('[data-testid="mission-list"] [role="button"]');
  await openDrawer(page, "DEV-2");
  await page.waitForSelector('[data-testid="dep-panel"]', { timeout: 5000 });
  const { headingTop, bodyOver } = await page.evaluate(() => {
    const h = [...document.querySelectorAll('[role="dialog"] h3')]
      .find((el) => el.textContent.includes("Send guidance"));
    const body = document.querySelector('[role="dialog"] .overflow-y-auto');
    return {
      headingTop: h ? h.getBoundingClientRect().top : Infinity,
      bodyOver: body ? Math.ceil(body.scrollWidth) - body.clientWidth : 999,
    };
  });
  check(`Send guidance stays reachable at 390×844 (top ${Math.round(headingTop)}px)`,
    headingTop < 844);
  check(`drawer body has no horizontal overflow at 390 (${bodyOver}px)`,
    bodyOver <= 1);
});

summary("missions_deps");
