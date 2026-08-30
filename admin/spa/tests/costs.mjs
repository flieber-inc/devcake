// Runs-page cost UX (ADR-0021 part 3): totals row, PMO/date filters, and
// the Cost Inputs modal reached from the ⋯ menu. Asserts STRUCTURE, never
// specific rate values (the live stack's card may be operator-edited), and
// always CANCELS — this suite never saves rates, never mutates config.
import { check, checked, gotoFresh, skip, summary, withPage } from "./harness.mjs";

await withPage(async (page) => {
  await gotoFresh(page, "#/runs");
  await page.waitForSelector('h1:has-text("Runs")');

  // filter controls render (PMO select + UTC date bounds)
  check("PMO connector filter renders",
    (await page.locator('select[aria-label="Filter by PMO connector"]').count()) === 1);
  check("date-range filters render (UTC-labeled)",
    (await page.locator('input[aria-label="From date (UTC)"]').count()) === 1 &&
    (await page.locator('input[aria-label="To date (UTC, inclusive)"]').count()) === 1);

  // token/cost columns — each header is its sortable button
  for (const col of ["in", "out", "cache r", "cache w", "cost"]) {
    check(`"${col}" column header renders (sortable)`,
      (await page.locator(`th button[title="Sort by ${col}"]`).count()) === 1);
  }

  // totals row: present iff any runs are listed on this stack
  const rows = await page.locator("tbody tr").count();
  const totals = page.locator('[data-testid="runs-totals"]');
  if ((await totals.count()) === 0) {
    skip("totals row over the filtered set", "no runs on this stack");
  } else {
    const text = await totals.innerText();
    check("totals row labels itself as filtered totals",
      /filtered totals/.test(text));
    check("totals row is a single summary line", (await totals.count()) === 1 && rows >= 1);
  }

  // (a) sortable headers — read-only interaction, restored to the default
  const costTh = page.locator('th:has(button[title="Sort by cost"])');
  check("cost header is sortable", (await costTh.count()) === 1);
  await page.click('button[title="Sort by cost"]');
  await page.waitForTimeout(400);
  check("first click sorts cost descending",
    (await costTh.getAttribute("aria-sort")) === "descending");
  await page.click('button[title="Sort by cost"]');
  await page.waitForTimeout(400);
  check("second click flips the direction",
    (await costTh.getAttribute("aria-sort")) === "ascending");
  await page.click('button[title="Sort by started"]');   // back to default
  await page.waitForTimeout(400);

  // (b) aggregate by mission — a view mode, safe to toggle and restore
  const agg = page.locator('label:has-text("Aggregate by mission") input');
  check("aggregate-by-mission checkbox renders", (await agg.count()) === 1);
  await agg.check();
  await page.waitForTimeout(800);
  if ((await page.locator('[data-testid="mission-group"]').count()) === 0) {
    skip("grouped mode clusters runs under mission rows", "no runs on this stack");
  } else {
    check("grouped mode clusters runs under mission rows",
      (await page.locator('[data-testid="mission-group"]').count()) >= 1);
    check("pagination speaks missions when grouped",
      /of \d+ missions/.test(await page.innerText("main")));
  }
  await agg.uncheck();
  await page.waitForTimeout(400);

  // ⋯ → Cost inputs modal: structure only, then Cancel
  await page.click('button[aria-label="More run actions"]');
  const csvItem = page.locator('[role="menuitem"]:has-text("Export to CSV")');
  check("⋯ offers Export to CSV…", (await csvItem.count()) === 1);
  const item = page.locator('[role="menuitem"]:has-text("Cost inputs")');
  check("⋯ offers Cost inputs…", (await item.count()) === 1);
  await item.click();
  await page.waitForSelector('[role="dialog"]:has-text("Cost inputs")');
  // the rate table appears only after the modal's GET /config resolves —
  // wait for it (a failed fetch times out here loudly, as it should)
  await page.waitForSelector('[role="dialog"] th:has-text("Model prefix")');
  check("modal shows the rate table header",
    (await page.locator('[role="dialog"] th:has-text("Model prefix")').count()) === 1);
  check("modal has at least one rate row or the empty state",
    (await page.locator('[role="dialog"] tbody tr').count()) >= 1);
  const modalText = await page.locator('[role="dialog"]').innerText();
  check("modal teaches where to find vendor list prices",
    /published API pricing page/.test(modalText) &&
    /longest prefix wins/.test(modalText));
  check("override checkbox present with honest scope note",
    (await page.locator('[role="dialog"] input[type="checkbox"]').count()) === 1 &&
    modalText.includes("matching row"));
  const prefixInput = page.locator('[role="dialog"] input[aria-label^="Model prefix"]').first();
  if (await prefixInput.count()) {
    await prefixInput.fill("probe-edit");   // editable — then thrown away
    check("rate row is editable", (await prefixInput.inputValue()) === "probe-edit");
  } else {
    skip("rate row is editable", "empty rate card on this stack");
  }
  await page.click('[role="dialog"] button:has-text("Cancel")');
  await page.waitForTimeout(100);
  check("Cancel closes without saving",
    (await page.locator('[role="dialog"]').count()) === 0);
});

// ── per-run Stop (2026-08 reviewer round): the MissionDrawer's Stop lifted
// onto the table — rendered ONLY on dispatched/running rows, behind the same
// honest confirm. Mocked rows; the dialog is CANCELLED, nothing is stopped. ──
await withPage(async (page) => {
  const runs = [
    { run_id: "T-9-1-EXECUTE-RUNNIN", mission_key: "T-9", mission_type: "EXECUTE",
      dev_type: "senior-dev", seq: 1, state: "running",
      started_at: new Date(Date.now() - 60000).toISOString(), ended_at: null },
    { run_id: "T-8-1-EXECUTE-DONEXX", mission_key: "T-8", mission_type: "EXECUTE",
      dev_type: "senior-dev", seq: 1, state: "finished",
      started_at: new Date(Date.now() - 120000).toISOString(),
      ended_at: new Date(Date.now() - 60000).toISOString() },
    { run_id: "T-7-1-PLAN-FAILEDXX", mission_key: "T-7", mission_type: "PLAN",
      dev_type: "senior-dev", seq: 1, state: "failed",
      error: "exit 11: model backend down",
      started_at: new Date(Date.now() - 240000).toISOString(),
      ended_at: new Date(Date.now() - 180000).toISOString() },
  ];
  await page.route(/\/api\/v1\/runs(?:\?.*)?$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({ total: 3, total_runs: 3, runs, totals: null,
                             pmo_refs: [], rate_card: {} }) }));
  await gotoFresh(page, "#/runs");
  await page.waitForSelector("table tbody tr");

  // ── 2026-08-18 layout round: run ids are OFF the screen (popup + CSV
  // only — they wrapped at every hyphen), and trace is a per-row icon
  // beside the terminal icon instead of a column. Rows are one line. ──
  check("run id is not printed in any cell",
    !(await page.innerText("tbody")).includes("T-9-1-EXECUTE-RUNNIN"));
  check("stage glyph popup carries the run id",
    (await page.locator('tbody span[role="img"][title*="T-9-1-EXECUTE-RUNNIN"]').count()) === 1);
  check("terminal icon button on every row",
    (await page.locator('td button[title="Open the run terminal"]').count()) === 3);
  check("trace is a per-row icon link, not a column",
    (await page.locator('th:text-is("trace")').count()) === 0 &&
    (await page.locator('a[aria-label="Open traces for run T-9-1-EXECUTE-RUNNIN"]').count()) === 1);
  check("trace icon deep-links OpenObserve traces",
    ((await page.locator('a[aria-label="Open traces for run T-9-1-EXECUTE-RUNNIN"]')
      .getAttribute("href")) || "").includes("/web/traces"));
  check("error rides the state cell as a popup, not a second line",
    (await page.locator('tbody span[title="exit 11: model backend down"]').count()) === 1 &&
    !(await page.innerText("tbody")).includes("exit 11"));

  check("running row carries a Stop button",
    (await page.locator('button[aria-label="Stop run T-9-1-EXECUTE-RUNNIN"]').count()) === 1);
  check("finished row carries NO Stop button",
    (await page.locator('button[aria-label="Stop run T-8-1-EXECUTE-DONEXX"]').count()) === 0);
  await page.click('button[aria-label="Stop run T-9-1-EXECUTE-RUNNIN"]');
  await page.waitForSelector('[role="dialog"]');
  const text = await page.locator('[role="dialog"]').innerText();
  check("stop confirm owns the retry-limit truth",
    text.includes("Stop this run?") && text.includes("counts toward the retry limit"));
  await page.click('[role="dialog"] button:has-text("Cancel")');
  await page.waitForTimeout(100);
  check("cancel closes without stopping",
    (await page.locator('[role="dialog"]').count()) === 0);
});

// ── CAKE-171: honest absence phrases on null token/cost cells (mocked) ──
await withPage(async (page) => {
  const runs = [
    { run_id: "A-1-1-EXECUTE-WAITXX", mission_key: "A-1", mission_type: "EXECUTE",
      dev_type: "senior-dev", seq: 1, state: "running",
      input_tokens: null, output_tokens: null, cache_read_tokens: null,
      cache_write_tokens: null, cost_usd: null, cost_usd_estimated: null,
      started_at: new Date(Date.now() - 60000).toISOString(), ended_at: null },
    { run_id: "A-2-1-EXECUTE-FAILXX", mission_key: "A-2", mission_type: "EXECUTE",
      dev_type: "senior-dev", seq: 1, state: "failed",
      input_tokens: null, output_tokens: null, cache_read_tokens: null,
      cache_write_tokens: null, cost_usd: null, cost_usd_estimated: null,
      started_at: new Date(Date.now() - 240000).toISOString(),
      ended_at: new Date(Date.now() - 180000).toISOString() },
    { run_id: "A-3-1-EXECUTE-EMPTYX", mission_key: "A-3", mission_type: "EXECUTE",
      dev_type: "senior-dev", seq: 1, state: "finished",
      token_source: "unavailable",
      input_tokens: null, output_tokens: null, cache_read_tokens: null,
      cache_write_tokens: null, cost_usd: null, cost_usd_estimated: null,
      started_at: new Date(Date.now() - 120000).toISOString(),
      ended_at: new Date(Date.now() - 60000).toISOString() },
    { run_id: "A-4-1-EXECUTE-ZEROXX", mission_key: "A-4", mission_type: "EXECUTE",
      dev_type: "senior-dev", seq: 1, state: "finished",
      token_source: "end_event",
      input_tokens: 0, output_tokens: 0, cache_read_tokens: 0,
      cache_write_tokens: 0, cost_usd: 0, cost_usd_estimated: null,
      started_at: new Date(Date.now() - 300000).toISOString(),
      ended_at: new Date(Date.now() - 240000).toISOString() },
  ];
  await page.route(/\/api\/v1\/runs(?:\?.*)?$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({
        total: 4, total_runs: 4, runs,
        totals: {
          runtime_seconds: 180,
          input_tokens: 0, output_tokens: 0,
          cache_read_tokens: 0, cache_write_tokens: null,
          cost_usd: 0, cost_usd_estimated: null, cost_usd_effective: 0,
          total_tokens_effective: 0,
        },
        // Non-zero rate_count keeps this suite isolated from empty-card precedence
        pmo_refs: [], rate_card: {
          rate_card_id: "builtin-v2", override_native: false, rate_count: 2,
        },
      }) }));
  await gotoFresh(page, "#/runs");
  await page.waitForSelector("table tbody tr");
  const body = await page.innerText("tbody");

  check("running null cells say available after the run ends",
    body.includes("available after the run ends"));
  check("failed null cells say not extracted (run failed)",
    body.includes("not extracted (run failed)"));
  check("finished + unavailable source says not extracted (unavailable)",
    body.includes("not extracted (unavailable)"));
  check("measured zero still renders as 0 / $0.00, not an absence phrase",
    /\b0\b/.test(body) && body.includes("$0.00"));
  check("null aggregate cache-write column says not extracted (not a fabricated 0)",
    (await page.locator('[data-testid="runs-totals"] [aria-label="not extracted"]').count()) >= 1);
});


// ── CAKE-174: empty rate card cost copy composed with CAKE-171 by state ──
await withPage(async (page) => {
  const runs = [
    { run_id: "E-1-1-EXECUTE-RUNNNG", mission_key: "E-1", mission_type: "EXECUTE",
      dev_type: "senior-dev", seq: 1, state: "running",
      input_tokens: null, output_tokens: null, cache_read_tokens: null,
      cache_write_tokens: null, cost_usd: null, cost_usd_estimated: null,
      model: "claude-opus-4-6",
      started_at: new Date(Date.now() - 60000).toISOString(), ended_at: null },
    { run_id: "E-2-1-EXECUTE-FAILED", mission_key: "E-2", mission_type: "EXECUTE",
      dev_type: "senior-dev", seq: 1, state: "failed",
      input_tokens: null, output_tokens: null, cache_read_tokens: null,
      cache_write_tokens: null, cost_usd: null, cost_usd_estimated: null,
      model: "claude-opus-4-6",
      started_at: new Date(Date.now() - 240000).toISOString(),
      ended_at: new Date(Date.now() - 180000).toISOString() },
    { run_id: "E-3-1-EXECUTE-NORATE", mission_key: "E-3", mission_type: "EXECUTE",
      dev_type: "senior-dev", seq: 1, state: "finished",
      token_source: "end_event",
      input_tokens: 1000, output_tokens: 100, cache_read_tokens: 0,
      cache_write_tokens: null, cost_usd: null, cost_usd_estimated: null,
      model: "grok-4.5-build",
      started_at: new Date(Date.now() - 120000).toISOString(),
      ended_at: new Date(Date.now() - 60000).toISOString() },
  ];
  await page.route(/\/api\/v1\/runs(?:\?.*)?$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({
        total: 3, total_runs: 3, runs,
        totals: null, pmo_refs: [],
        rate_card: { rate_card_id: "builtin-v3", override_native: false, rate_count: 0 },
      }) }));
  await gotoFresh(page, "#/runs");
  await page.waitForSelector("table tbody tr");
  const rows = page.locator("table tbody tr");
  const runningCost = await rows.nth(0).locator("td").nth(-2).innerText();
  const failedCost = await rows.nth(1).locator("td").nth(-2).innerText();
  const finishedCost = await rows.nth(2).locator("td").nth(-2).innerText();
  check("empty card + running cost still says available after the run ends",
    runningCost.includes("available after the run ends"));
  check("empty card + failed cost still says not extracted (run failed)",
    failedCost.includes("not extracted (run failed)"));
  check("empty card + finished unpriced cost shows no-rate-card taxonomy",
    finishedCost.includes("no rate card — add rates under Cost inputs"));
});

summary("costs");
