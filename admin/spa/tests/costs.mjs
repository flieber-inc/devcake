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
  check("override checkbox present with honest scope note",
    (await page.locator('[role="dialog"] input[type="checkbox"]').count()) === 1 &&
    (await page.locator('[role="dialog"]').innerText()).includes("matching row"));
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
  ];
  await page.route(/\/api\/v1\/runs(?:\?.*)?$/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json",
      body: JSON.stringify({ total: 2, total_runs: 2, runs, totals: null,
                             pmo_refs: [], rate_card: {} }) }));
  await gotoFresh(page, "#/runs");
  await page.waitForSelector("table tbody tr");
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

summary("costs");
