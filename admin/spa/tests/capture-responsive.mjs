// Reproducible PR screenshot capture for the responsive admin remediation.
// Runs the real local SPA; only Missions/Runs board data is intercepted so the
// evidence is deterministic and cannot mutate a configured PMO.
import { chromium } from "playwright-core";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const BASE = process.env.UI_BASE || "http://127.0.0.1:5199";
const executablePath = process.env.UI_CHROME;
if (!executablePath) throw new Error("set UI_CHROME to a Chrome/Chromium executable");

const output = resolve("docs/img/admin-responsive-remediation");
await mkdir(output, { recursive: true });

const browser = await chromium.launch({ executablePath });

const labelsFor = {
  backlog: ["DEVCAKE"],
  plan: ["DEVCAKE", "DEVCAKE-PLAN"],
  execute: ["DEVCAKE", "DEVCAKE-EXECUTE"],
  review: ["DEVCAKE", "DEVCAKE-REVIEW"],
  merge: ["DEVCAKE", "DEVCAKE-MERGE"],
  needs_human: ["DEVCAKE", "DEVCAKE-NEEDS-HUMAN"],
  done: ["DEVCAKE"],
};

function mission(column, index) {
  return {
    pmo_id: `${column}-${index}`,
    key: `${column.slice(0, 3).toUpperCase()}-${index}`,
    title: `${column.replace("_", " ")} mission ${index} with enough detail to show the compact laptop card treatment`,
    status: column === "backlog" ? "backlog" : column === "done" ? "done" : "in_progress",
    priority: index % 2 ? "high" : "medium",
    labels: labelsFor[column],
    mission_type: ["plan", "execute", "review"].includes(column) ? column.toUpperCase() : null,
    schedulable: column !== "needs_human",
    reason: column === "needs_human" ? "Waiting for operator guidance" : null,
    repo: "devcake",
    url: `https://linear.example/${column}-${index}`,
    updated_at: `2026-07-18T12:${String(index).padStart(2, "0")}:00Z`,
  };
}

const missions = [
  ...Array.from({ length: 12 }, (_, i) => mission("backlog", i + 1)),
  ...Array.from({ length: 3 }, (_, i) => mission("plan", i + 1)),
  ...Array.from({ length: 4 }, (_, i) => mission("execute", i + 1)),
  ...Array.from({ length: 2 }, (_, i) => mission("review", i + 1)),
  ...Array.from({ length: 2 }, (_, i) => mission("merge", i + 1)),
  ...Array.from({ length: 3 }, (_, i) => mission("needs_human", i + 1)),
  ...Array.from({ length: 4 }, (_, i) => mission("done", i + 1)),
];

const runs = {
  total: 1,
  runs: [{
    run_id: "BAC-12-PLAN-ABC123",
    mission_key: "BAC-12",
    mission_type: "PLAN",
    state: "finished",
    started_at: "2026-07-18T12:00:00Z",
    ended_at: "2026-07-18T12:02:00Z",
    created_at: "2026-07-18T12:00:00Z",
  }],
};

async function pageFor({
  width, height, theme = "light", sidebar = "expanded",
  mockBoard = false, reducedMotion = "no-preference",
}) {
  const context = await browser.newContext({
    viewport: { width, height }, colorScheme: theme, reducedMotion,
  });
  await context.addInitScript(({ theme, sidebar }) => {
    localStorage.setItem("devcake-theme", theme);
    localStorage.setItem("devcake-sidebar", sidebar);
  }, { theme, sidebar });
  const page = await context.newPage();
  if (mockBoard) {
    await page.route("**/api/v1/missions", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ missions, adoption_mode: "auto", teams: { primary: "DEV" } }),
    }));
    await page.route("**/api/v1/runs?*", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(runs),
    }));
    await page.route("**/api/v1/runs/*/log", (route) => route.fulfill({ status: 200, body: "verification log\n" }));
  }
  return { context, page };
}

async function goto(page, hash, selector) {
  await page.goto(`${BASE}/${hash}`);
  await page.reload();
  await page.waitForSelector(selector, { timeout: 15000 });
}

async function captureBoard(name, options, interact) {
  const { context, page } = await pageFor({ ...options, mockBoard: true });
  await goto(page, "#/missions", '[role="region"][aria-label="Mission board"]');
  await interact?.(page);
  const geometry = await page.evaluate(() => {
    const board = document.querySelector('[role="region"][aria-label="Mission board"]');
    const main = document.querySelector("main");
    return {
      lanes: board.querySelectorAll("section[aria-label]").length,
      boardOverflowX: board.scrollWidth - board.clientWidth,
      mainOverflowX: main.scrollWidth - main.clientWidth,
      mainOverflowY: main.scrollHeight - main.clientHeight,
    };
  });
  await page.screenshot({ path: resolve(output, name) });
  console.log(`${name}: ${JSON.stringify(geometry)}`);
  await context.close();
}

await captureBoard("01-missions-1366-light.png", { width: 1366, height: 768, theme: "light" });
await captureBoard("02-missions-1366-dark.png", { width: 1366, height: 768, theme: "dark" });
await captureBoard("03-missions-1280-tall-lane.png", { width: 1280, height: 720, theme: "light" }, async (page) => {
  await page.locator('[aria-label="Backlog missions"]').evaluate((node) => { node.scrollTop = 300; });
});
await captureBoard("04-missions-390-light.png", { width: 390, height: 844, theme: "light" });
await captureBoard("05-new-mission-390-dark.png", { width: 390, height: 844, theme: "dark" }, async (page) => {
  await page.getByRole("button", { name: "New mission" }).click();
  await page.getByRole("dialog", { name: "New mission" }).waitFor();
});
await captureBoard("06-mission-menu-390.png", { width: 390, height: 844, theme: "light" }, async (page) => {
  await page.getByRole("button", { name: /Open mission BAC-/ }).first().click();
  await page.getByRole("dialog", { name: /Mission BAC-/ }).waitFor();
  await page.getByRole("button", { name: /Actions for BAC-/ }).click();
  await page.getByRole("menu", { name: /Actions for BAC-/ }).waitFor();
});
await captureBoard("07-mission-drawer-1366.png", { width: 1366, height: 768, theme: "light" }, async (page) => {
  await page.getByRole("button", { name: /Open mission BAC-/ }).first().click();
  await page.getByRole("dialog", { name: /Mission BAC-/ }).waitFor();
});

{
  const { context, page } = await pageFor({ width: 1024, height: 768, sidebar: "collapsed" });
  await goto(page, "#/config/limits", "#limits");
  const chip = page.locator('main a[href="#/config/limits"]:visible', { hasText: "Limits" });
  const observation = {
    chipVisible: await chip.count() === 1,
    mainOverflowX: await page.locator("main").evaluate((node) => node.scrollWidth - node.clientWidth),
  };
  await page.screenshot({ path: resolve(output, "08-config-1024-collapsed.png") });
  console.log(`08-config-1024-collapsed.png: ${JSON.stringify(observation)}`);
  await context.close();
}

{
  const { context, page } = await pageFor({ width: 390, height: 844, sidebar: "expanded" });
  await goto(page, "#/runs", 'input[aria-label="Filter runs by mission key"]');
  const observation = await page.locator("main").evaluate((node) => ({
    mainOverflowX: node.scrollWidth - node.clientWidth,
    sidebarWidth: document.querySelector("aside").getBoundingClientRect().width,
  }));
  await page.screenshot({ path: resolve(output, "09-runs-390.png") });
  console.log(`09-runs-390.png: ${JSON.stringify(observation)}`);
  await context.close();
}

{
  const { context, page } = await pageFor({
    width: 375, height: 520, theme: "dark", mockBoard: true,
  });
  await goto(page, "#/missions", '[role="region"][aria-label="Mission board"]');
  await page.getByRole("button", { name: "New mission" }).click();
  const dialog = page.getByRole("dialog", { name: "New mission" });
  const observation = await dialog.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return {
      top: rect.top,
      bottom: rect.bottom,
      viewportHeight: innerHeight,
      internalScroll: node.scrollHeight > node.clientHeight,
    };
  });
  console.log(`probe short mobile dialog: ${JSON.stringify(observation)}`);
  await context.close();
}

{
  const { context, page } = await pageFor({ width: 390, height: 844, mockBoard: true });
  await goto(page, "#/missions", '[role="region"][aria-label="Mission board"]');
  await page.getByRole("button", { name: /Open mission BAC-/ }).first().click();
  const drawer = page.getByRole("dialog", { name: /Mission BAC-/ });
  await page.getByRole("button", { name: /Actions for BAC-/ }).click();
  await page.getByRole("menuitem", { name: /^Park/ }).click();
  const confirm = page.getByRole("dialog", { name: "Park this mission?" });
  await page.keyboard.press("Escape");
  const confirmProbe = {
    confirmVisible: await confirm.isVisible(),
    drawerVisible: await drawer.isVisible(),
  };
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await page.locator('tr[title="Open this run\'s terminal"]').click();
  const terminal = page.getByRole("dialog", { name: /Run terminal BAC-12/ });
  await terminal.waitFor();
  await page.keyboard.press("Escape");
  const terminalProbe = {
    terminalClosed: await terminal.count() === 0,
    drawerVisible: await drawer.isVisible(),
  };
  console.log(`probe stacked overlays: ${JSON.stringify({ confirmProbe, terminalProbe })}`);
  await context.close();
}

{
  const { context, page } = await pageFor({
    width: 1280, height: 720, reducedMotion: "reduce", sidebar: "expanded",
  });
  await goto(page, "#/overview", "main h1");
  const transition = await page.locator("aside").evaluate((node) => ({
    property: getComputedStyle(node).transitionProperty,
    duration: getComputedStyle(node).transitionDuration,
  }));
  console.log(`probe reduced motion sidebar transition: ${JSON.stringify(transition)}`);
  await context.close();
}

await browser.close();
