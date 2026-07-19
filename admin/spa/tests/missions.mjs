// Missions responsive suite: deterministic, read-only browser coverage for the
// board workspace. API responses are intercepted so the suite never mutates a
// configured PMO and every lifecycle lane is populated consistently.
import { check, gotoFresh, summary, withPage } from "./harness.mjs";

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
    title: `${column.replace("_", " ")} mission ${index} with enough detail to exercise compact cards`,
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
  mission("plan", 1),
  mission("execute", 1),
  mission("review", 1),
  mission("merge", 1),
  mission("needs_human", 1),
  mission("done", 1),
];

const config = {
  schema_version: 4,
  intake_paused: false,
  dismissed_alerts: [],
  repos: [],
  pmos: [],
  forges: [],
  relations_mapper: { enabled: false, interval_minutes: 60, dev_type: null },
};

async function mockApi(page) {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const response = path === "/missions"
      ? { missions, adoption_mode: "auto", teams: { primary: "DEV" } }
      : path === "/runs"
        ? {
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
          }
        : path === "/config"
        ? config
        : path === "/health"
          ? { app: true, redis: true, dagu: true, openobserve: true, intake_paused: false }
          : path === "/dev-types"
            ? []
            : path === "/assignments"
              ? {}
              : path === "/harnesses"
                ? {}
                : {};
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(response) });
  });
}

await withPage(async (page) => {
  await mockApi(page);
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('section[aria-label="Done"]');

  const board = page.locator('[role="region"][aria-label="Mission board"]');
  check("Missions exposes one labelled board region", await board.count() === 1);

  const labels = ["Backlog", "Plan", "Execute", "Review", "Merge wait", "Needs human", "Done"];
  check("all seven lifecycle lanes render", await Promise.all(
    labels.map((label) => page.locator(`section[aria-label="${label}"]`).count()),
  ).then((counts) => counts.every((count) => count === 1)));

  const fit = await page.evaluate(() => {
    const region = document.querySelector('[role="region"][aria-label="Mission board"]');
    if (!region) return null;
    const boardRect = region.getBoundingClientRect();
    const lanes = [...region.querySelectorAll("section[aria-label]")].map((lane) => lane.getBoundingClientRect());
    const main = document.querySelector("main");
    return {
      boardFits: region.scrollWidth <= region.clientWidth + 1,
      lanesFit: lanes.length === 7 && lanes.every((r) => r.left >= boardRect.left - 1 && r.right <= boardRect.right + 1),
      mainFits: main.scrollHeight <= main.clientHeight + 1 && main.scrollWidth <= main.clientWidth + 1,
      bodyFits: document.body.scrollHeight <= window.innerHeight + 1 && document.body.scrollWidth <= window.innerWidth + 1,
      boardBottomVisible: boardRect.bottom <= main.getBoundingClientRect().bottom + 1,
    };
  });
  check("1280px board fits all seven lanes without horizontal scrolling", !!fit?.boardFits && !!fit?.lanesFit, JSON.stringify(fit));
  check("Missions has no page-level scrolling", !!fit?.mainFits && !!fit?.bodyFits, JSON.stringify(fit));
  check("board bottom remains inside the visible workspace", !!fit?.boardBottomVisible, JSON.stringify(fit));

  const backlog = page.locator('[aria-label="Backlog missions"]');
  const laneScroll = await backlog.evaluate((node) => {
    const before = node.scrollTop;
    node.scrollTop = 120;
    return {
      overflows: node.scrollHeight > node.clientHeight,
      moved: node.scrollTop > before,
      mainTop: document.querySelector("main").scrollTop,
    };
  }).catch(() => null);
  check("busy lanes scroll independently of the page",
    !!laneScroll?.overflows && !!laneScroll?.moved && laneScroll?.mainTop === 0,
    JSON.stringify(laneScroll));
}, { width: 1280, height: 720 });

await withPage(async (page) => {
  await mockApi(page);
  await page.addInitScript(() => localStorage.setItem("devcake-sidebar", "expanded"));
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('[role="region"][aria-label="Mission board"]');

  const mobile = await page.evaluate(() => {
    const aside = document.querySelector("aside").getBoundingClientRect();
    const board = document.querySelector('[role="region"][aria-label="Mission board"]');
    const firstLane = board.querySelector("section[aria-label]").getBoundingClientRect();
    const main = document.querySelector("main");
    const before = board.scrollLeft;
    board.scrollLeft = 120;
    return {
      sidebarWidth: aside.width,
      boardOverflows: board.scrollWidth > board.clientWidth,
      boardMoved: board.scrollLeft > before,
      laneFits: firstLane.width <= board.clientWidth + 1,
      mainFits: main.scrollHeight <= main.clientHeight + 1 && main.scrollWidth <= main.clientWidth + 1,
      bodyFits: document.body.scrollHeight <= innerHeight + 1 && document.body.scrollWidth <= innerWidth + 1,
    };
  });
  check("mobile forces the sidebar rail even when expanded is stored", mobile.sidebarWidth <= 60, JSON.stringify(mobile));
  check("mobile keeps horizontal navigation inside the board", mobile.boardOverflows && mobile.boardMoved && mobile.laneFits, JSON.stringify(mobile));
  check("mobile Missions does not create page overflow", mobile.mainFits && mobile.bodyFits, JSON.stringify(mobile));
}, { width: 390, height: 844 });

await withPage(async (page) => {
  await mockApi(page);
  await gotoFresh(page, "#/missions");
  const trigger = page.getByRole("button", { name: "New mission" });
  await trigger.focus();
  await trigger.click();
  const dialog = page.getByRole("dialog");
  await dialog.waitFor();
  const bounds = await dialog.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    const main = document.querySelector("main");
    return {
      top: rect.top,
      bottom: rect.bottom,
      viewport: innerHeight,
      scrollable: node.scrollHeight > node.clientHeight && getComputedStyle(node).overflowY !== "visible",
      mainFits: main.scrollHeight <= main.clientHeight + 1,
      bodyFits: document.body.scrollHeight <= innerHeight + 1,
    };
  });
  check("New Mission stays inside a short mobile viewport",
    bounds.top >= 0 && bounds.bottom <= bounds.viewport && bounds.scrollable,
    JSON.stringify(bounds));
  check("opening a mobile dialog does not create page scroll", bounds.mainFits && bounds.bodyFits, JSON.stringify(bounds));
  await page.keyboard.press("Escape");
  await page.waitForTimeout(100);
  const focus = await page.evaluate(() => ({
    tag: document.activeElement?.tagName,
    label: document.activeElement?.getAttribute("aria-label"),
    text: document.activeElement?.textContent?.trim(),
  }));
  check("closing New Mission restores focus", focus.text === "New mission", JSON.stringify(focus));
}, { width: 390, height: 480 });

await withPage(async (page) => {
  await mockApi(page);
  await gotoFresh(page, "#/missions");
  const trigger = page.getByRole("button", { name: /Open mission BAC-/ }).first();
  await trigger.focus();
  await trigger.click();
  const drawer = page.getByRole("dialog", { name: /Mission BAC-/ });
  await drawer.waitFor();
  check("Mission drawer has an accessible mission label", true);
  const bounds = await drawer.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return {
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      left: rect.left,
      width: rect.width,
      viewportWidth: innerWidth,
      viewportHeight: innerHeight,
    };
  });
  check("mobile Mission drawer uses the full viewport",
    bounds.top <= 1 && bounds.bottom >= bounds.viewportHeight - 1 &&
      bounds.left <= 1 && bounds.right >= bounds.viewportWidth - 1,
    JSON.stringify(bounds));
  await page.getByRole("button", { name: /Actions for BAC-/ }).click();
  const menu = page.getByRole("menu", { name: /Actions for BAC-/ });
  const menuBounds = await menu.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
  });
  check("drawer action menu stays inside the mobile viewport",
    menuBounds.left >= 8 && menuBounds.right <= 382 && menuBounds.top >= 8 && menuBounds.bottom <= 836,
    JSON.stringify(menuBounds));
  await page.keyboard.press("Escape");
  await page.waitForTimeout(100);
  const menuTriggerFocused = await page.evaluate(() =>
    document.activeElement?.getAttribute("aria-label")?.startsWith("Actions for BAC-"));
  check("Escape closes the menu without closing its drawer",
    await menu.count() === 0 && await drawer.isVisible() && menuTriggerFocused);

  await page.getByRole("button", { name: /Actions for BAC-/ }).click();
  await page.getByRole("menuitem", { name: /^Park/ }).click();
  const confirm = page.getByRole("dialog", { name: "Park this mission?" });
  await confirm.waitFor();
  await page.keyboard.press("Escape");
  await page.waitForTimeout(100);
  check("Escape is consumed by a confirmation stacked over the drawer",
    await confirm.isVisible() && await drawer.isVisible());
  await confirm.getByRole("button", { name: "Cancel" }).click();

  await page.locator('tr[title="Open this run\'s terminal"]').click();
  const terminal = page.getByRole("dialog", { name: /Run terminal BAC-12/ });
  await terminal.waitFor();
  await page.keyboard.press("Escape");
  await page.waitForTimeout(100);
  check("Escape closes only the top terminal overlay",
    await terminal.count() === 0 && await drawer.isVisible());

  await page.getByRole("button", { name: "Close drawer" }).click();
  await page.waitForTimeout(100);
  check("closing the Mission drawer restores card focus",
    await page.evaluate(() => document.activeElement?.getAttribute("aria-label")?.startsWith("Open mission BAC-")));
}, { width: 390, height: 844 });

await withPage(async (page) => {
  await mockApi(page);
  await page.addInitScript(() => localStorage.setItem("devcake-sidebar", "expanded"));
  await gotoFresh(page, "#/missions");
  const sidebar = page.locator("aside");
  check("Missions temporarily collapses the desktop sidebar", (await sidebar.boundingBox()).width <= 60);

  await page.locator('aside a[href="#/runs"]').click();
  await page.waitForSelector('input[aria-label="Filter runs by mission key"]');
  await page.waitForTimeout(250);
  check("leaving Missions restores the desktop sidebar preference", (await sidebar.boundingBox()).width >= 200);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(250);
  check("resizing below the desktop breakpoint collapses the sidebar", (await sidebar.boundingBox()).width <= 60);
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.waitForTimeout(250);
  check("resizing back restores the desktop sidebar preference", (await sidebar.boundingBox()).width >= 200);
}, { width: 1280, height: 720 });

summary("missions");
