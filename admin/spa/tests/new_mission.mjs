// New Mission composer (ADR-0030): header hierarchy (ONE primary action),
// per-adapter field honesty (priority hidden where the adapter ignores it),
// adopt toggle only under opt_in, confirm copy assembled from the actual
// request with a NON-danger confirm, POST body shape, partial-failure
// disclosure, success flash + the follow-up poll. Fully route-mocked —
// nothing is really created.
import { check, checked, gotoFresh, summary, withPage } from "./harness.mjs";

const ROWS = [{
  pmo_id: "X1", key: "DEV-1", instance: "linear1", team: "DEV", kind: "issue",
  title: "An existing mission", status: "backlog", labels: ["DEVCAKE"],
  mission_type: "ONBOARD", priority: "medium", repo: null,
  url: "https://linear.app/x/1", updated_at: new Date().toISOString(),
  schedulable: true, reason: "", blocked_by: [],
}];

async function mockRoutes(page, { adoption = "opt_in", createResult = null,
                                  posted = [], polls = [] } = {}) {
  await page.route(/\/api\/v1\/missions(?:\?.*)?$/, (route) => {
    if (route.request().method() === "POST") {
      posted.push(JSON.parse(route.request().postData() || "{}"));
      return route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify(createResult || {
          key: "DEV-42", pmo_id: "NEW1", instance: "linear1",
          url: "https://linear.app/x/42", adopted: true,
          attachment_failures: [], feed_posted: false,
        }),
      });
    }
    return route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ missions: ROWS, adoption_mode: adoption,
                             teams: { linear1: "DEV", board1: "devcake-pmo/missions" } }),
    });
  });
  await page.route(/\/api\/v1\/config$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ pmos: [
        { name: "linear1", system: "linear", team_key: "DEV" },
        { name: "board1", system: "gitea_issues",
          team_key: "devcake-pmo/missions" },
      ] }),
    }));
  await page.route(/\/api\/v1\/connections\/registry$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ pmo_systems: [
        { id: "linear", display_name: "Linear", supports_priority: true },
        { id: "gitea_issues", display_name: "Gitea Issues",
          supports_priority: false },
      ], forges: [], secret_shape_prefixes: [], managed_labels_expected: 11 }),
    }));
  await page.route(/\/api\/v1\/poll\/run$/, (route) => {
    polls.push(1);
    return route.fulfill({ status: 200, contentType: "application/json",
                           body: JSON.stringify({ ok: true }) });
  });
}

await withPage(async (page) => {
  const posted = [], polls = [];
  await mockRoutes(page, { posted, polls });
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('[data-testid="mission-list"] [role="button"]');

  // header hierarchy: exactly one visually primary action (DESIGN.md §3)
  const primaries = await page.evaluate(() => {
    const header = document.querySelector("h1")?.closest("div")?.parentElement;
    const scope = header || document;
    return [...scope.querySelectorAll("button")]
      .filter((b) => /bg-accent(?!-)/.test(b.className))
      .map((b) => b.textContent.trim());
  });
  check("header has exactly one primary action — New mission",
    primaries.length === 1 && primaries[0].includes("New mission"),
    JSON.stringify(primaries));
  check("Poll now survives as a ghost (never a menu burial)",
    (await page.locator('button:has-text("Poll now")').count()) === 1);

  // the composer opens; adopt toggle visible under opt_in
  await page.click('button:has-text("New mission")');
  await page.waitForSelector('h4:has-text("New mission")');
  await checked("adopt toggle renders under opt_in", async () => {
    await page.waitForSelector(':text("Start work on next poll")',
      { timeout: 5000 });
    return true;
  });
  // priority visible for the Linear instance…
  check("priority renders for a priority-honoring adapter",
    (await page.locator('select[aria-label="Mission priority"]').count()) === 1);
  // …and hidden for the gitea_issues instance (its create ignores priority)
  await page.selectOption('select[aria-label="PMO instance"]', "board1");
  await page.waitForTimeout(100);
  check("priority hides for a forge-issue adapter",
    (await page.locator('select[aria-label="Mission priority"]').count()) === 0);
  await page.selectOption('select[aria-label="PMO instance"]', "linear1");

  // confirm copy is assembled from the ACTUAL request, non-danger button
  await page.fill('input[aria-label="Mission title"]', "Ship the thing");
  await page.fill('textarea[aria-label="Mission description"]', "the brief");
  await page.click('button:has-text("Create mission…")');
  await page.waitForSelector('h4:has-text("Create mission in")');
  const confirmText = await page.evaluate(() =>
    [...document.querySelectorAll('[role="dialog"]')].map((d) => d.textContent)
      .join(" "));
  check("confirm names the title, team, and system",
    confirmText.includes('"Ship the thing"') && confirmText.includes("DEV")
    && confirmText.includes("Linear"));
  check("confirm states the adoption consequence",
    confirmText.includes("DEVCAKE label"));
  const confirmBtn = page.locator('button:text-is("Create mission")').last();
  check("the confirm button is NOT the danger kind (creative action)",
    !/danger|bg-red/.test(await confirmBtn.evaluate((b) => b.className)));

  // submit → POST body shape → flash → follow-up poll
  await confirmBtn.click();
  await checked("success flash names what exists where", async () => {
    await page.waitForSelector(':text("DEV-42 created in linear1")',
      { timeout: 5000 });
    return true;
  });
  check("POST body carries instance/title/adopt/priority",
    posted.length === 1 && posted[0].instance === "linear1"
    && posted[0].title === "Ship the thing" && posted[0].adopt === true
    && posted[0].priority === "medium"
    && Array.isArray(posted[0].attachments));
  check("a follow-up poll fires after creation", polls.length >= 1);
});

// opt_out: the adopt toggle would be a lie — hidden
await withPage(async (page) => {
  await mockRoutes(page, { adoption: "opt_out" });
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('[data-testid="mission-list"] [role="button"]');
  await page.click('button:has-text("New mission")');
  await page.waitForSelector('h4:has-text("New mission")');
  await page.waitForTimeout(200);
  check("adopt toggle hidden under opt_out",
    (await page.locator(':text("Start work on next poll")').count()) === 0);
});

// partial attachment failure: disclosed by name, mission still celebrated
await withPage(async (page) => {
  const posted = [], polls = [];
  await mockRoutes(page, {
    posted, polls,
    createResult: {
      key: "DEV-43", pmo_id: "NEW2", instance: "linear1",
      url: "https://linear.app/x/43", adopted: true,
      attachment_failures: [{ name: "spec.pdf", error: "asset store 500" }],
      feed_posted: true,
    },
  });
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('[data-testid="mission-list"] [role="button"]');
  await page.click('button:has-text("New mission")');
  await page.waitForSelector('h4:has-text("New mission")');
  await page.fill('input[aria-label="Mission title"]', "Partial");
  await page.click('button:has-text("Create mission…")');
  await page.waitForSelector('h4:has-text("Create mission in")');
  await page.locator('button:text-is("Create mission")').last().click();
  await checked("partial failure is disclosed with the mission's success", async () => {
    await page.waitForSelector(':text("DEV-43 created in linear1")',
      { timeout: 5000 });
    const flash = await page.evaluate(() =>
      document.body.textContent || "");
    return flash.includes("1 attachment failed (spec.pdf)");
  });
});

summary("new_mission");
