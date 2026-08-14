// Scheduled Tasks (#/config/scheduled-tasks): DevCake tasks (built-in
// Steward + Memory Curator) over Custom tasks (operator cron rows).
// Iron rules: never confirms a Save, cancels every destructive dialog,
// and NEVER clicks Run now (it creates real tickets on a live stack).
import { check, checked, gotoFresh, skip, summary, withPage } from "./harness.mjs";

await withPage(async (page) => {
  await gotoFresh(page, "#/config/scheduled-tasks");
  await page.waitForSelector("#scheduled-tasks");
  // a backend that predates PLAN_MEMORY serves a config without `crons` —
  // the Curator panel and custom-task flows then have nothing to bind to
  // (environment-dependent, same doctrine as pmo_intake's no-board skip)
  const hasCrons = await page.evaluate(() =>
    fetch("/api/v1/config").then((r) => r.json())
      .then((c) => Array.isArray(c.crons)).catch(() => false));

  // 1: both cards render; nothing else does
  check("view renders DevCake tasks + Custom tasks, not Limits",
    (await page.locator("#scheduled-tasks").count()) === 1 &&
    (await page.locator("#scheduled-tasks-custom").count()) === 1 &&
    (await page.locator("#limits").count()) === 0);

  // 2: built-ins — both panels, no Delete anywhere in card 1
  if (hasCrons)
    check("Relations Steward and Memory Curator panels are present",
      (await page.locator('#scheduled-tasks span:has-text("Relations Steward")').count()) >= 1 &&
      (await page.locator('#scheduled-tasks span:has-text("Memory Curator")').count()) >= 1);
  else
    skip("Memory Curator panel checks", "backend predates crons (redeploy pending)");
  check("built-in tasks cannot be deleted",
    (await page.locator('#scheduled-tasks button:has-text("Delete")').count()) === 0);
  if (hasCrons)
    check("Curator status names the fan-out target",
      (await page.locator('#scheduled-tasks p:has-text("every Curator board")').count()) >= 1);
  check("Steward Dev Type select moved in",
    (await page.locator('select[aria-label="Relations Steward Dev Type"]').count()) === 1);
  check("both built-ins expose full-width instruction editors",
    (await page.locator('textarea[aria-label="Relations Steward instructions"]').count()) === 1 &&
    (hasCrons
      ? (await page.locator('textarea[aria-label="Memory Curator instructions"]').count()) === 1
      : true));
  check("Run now buttons carry the saved-settings badge",
    (await page.locator('#scheduled-tasks span:has-text("runs with saved settings")').count()) === (hasCrons ? 2 : 1));

  // 3: steward dirty-gating — an unsaved edit disables Run now
  await checked("dirty steward edits disable Run Relations Steward now", async () => {
    const interval = page.locator('input[aria-label="Relations Steward interval (minutes)"]');
    const before = await interval.inputValue();
    await interval.fill(String(Number(before) + 7));
    await page.waitForSelector('span:has-text("Unsaved changes")');
    const btn = page.locator('button[aria-label="Run Relations Steward now"]');
    if (!(await btn.isDisabled())) throw new Error("Run now stayed enabled while dirty");
    await page.click('button:has-text("Discard changes")');
    await page.waitForTimeout(150);
    return true;
  });

  // 4: curator edits ride the draft and label under Scheduled Tasks
  if (!hasCrons) skip("Curator draft/save-review checks", "backend predates crons");
  if (hasCrons) await checked("Curator toggle drafts + save review groups it", async () => {
    await page.locator('[role="switch"][aria-label="Memory Curator periodic service"]').click();
    await page.waitForSelector('span:has-text("Unsaved changes")');
    await page.click('button:has-text("Save changes…")');
    await page.waitForSelector('[role="dialog"]');
    const review = page.locator('[role="dialog"]');
    if ((await review.locator('text=Scheduled Tasks').count()) < 1)
      throw new Error("save review lacks the Scheduled Tasks group");
    if ((await review.locator('text=Periodic service').count()) < 1)
      throw new Error("save review lacks the Periodic service row");
    await review.locator('button:has-text("Cancel")').click();
    await page.click('button:has-text("Discard changes")');
    await page.waitForTimeout(150);
    return true;
  });

  // 5: create flow — dashed row, slug validation, dup refusal, editor
  if (!hasCrons) skip("custom-task create flow", "backend predates crons");
  if (hasCrons) await checked("create flow validates ids and drafts the row", async () => {
    await page.click('button:has-text("New scheduled task")');
    await page.waitForSelector('[role="dialog"]');
    const dialog = page.locator('[role="dialog"]');
    const input = dialog.locator("input");
    await input.fill("Bad Id!");
    await dialog.locator('button:has-text("Add to draft")').click();
    if ((await dialog.locator("text=invalid").count()) < 1)
      throw new Error("bad slug not refused");
    await input.fill("memory-curator");
    await dialog.locator('button:has-text("Add to draft")').click();
    if ((await dialog.locator("text=taken").count()) < 1)
      throw new Error("memory-curator not refused as duplicate");
    await input.fill("uicheck-tmp");
    await dialog.locator('button:has-text("Add to draft")').click();
    // create → straight into the editor modal, edits ride the page Save
    await page.waitForSelector('text=applied by the page-level Save');
    await page.locator('button:has-text("Close")').click();
    // drafted row: mono id, Run now disabled (never saved)
    if ((await page.locator('#scheduled-tasks-custom div:has-text("uicheck-tmp")').count()) < 1)
      throw new Error("drafted row not in the table");
    const run = page.locator('button[aria-label="Run uicheck-tmp now"]');
    if (!(await run.isDisabled()))
      throw new Error("Run now enabled on a never-saved row");
    // delete dialog states consequences; Cancel keeps the row
    await page.locator('button[aria-label="Delete uicheck-tmp"]').click();
    await page.waitForSelector('[role="dialog"]');
    if ((await page.locator('[role="dialog"] >> text=In-flight tickets are not canceled').count()) < 1)
      throw new Error("delete dialog lacks the honest consequence line");
    await page.locator('[role="dialog"] button:has-text("Cancel")').click();
    await page.click('button:has-text("Discard changes")');
    await page.waitForTimeout(150);
    return true;
  });
  check("discard leaves the draft clean",
    (await page.locator('text=Unsaved changes').count()) === 0);

  // 6: table idiom — Runs-style thead, badge in the header, no Add button
  const thead = page.locator("#scheduled-tasks-custom thead");
  check("custom table uses the Runs thead idiom",
    ((await thead.getAttribute("class")) || "").includes("uppercase"));
  check("custom card header: instant badge, no twin Add button",
    (await page.locator('#scheduled-tasks-custom span:has-text("Run now is instant")').count()) === 1 &&
    (await page.locator('#scheduled-tasks-custom button:has-text("Add cron")').count()) === 0);
  const customRows = await page.locator("#scheduled-tasks-custom tbody tr").count();
  const emptyLine = await page.locator('#scheduled-tasks-custom td:has-text("No custom tasks yet")').count();
  check("empty state renders exactly when there are no custom tasks",
    (emptyLine === 1) === (customRows === 1) || customRows > 1);
});

// 7: mobile 390px — chip navigates; wide content scrolls in its own box
await withPage(async (page) => {
  await gotoFresh(page, "#/config/limits");
  await page.waitForSelector("#limits");
  await page.locator('a[href="#/config/scheduled-tasks"]:visible').first().click();
  await page.waitForSelector("#scheduled-tasks");
  check("mobile chip navigates to Scheduled Tasks",
    (await page.locator("#scheduled-tasks-custom").count()) === 1);
  const wrap = page.locator("#scheduled-tasks-custom .overflow-x-auto");
  check("custom table scrolls horizontally inside its own container",
    (await wrap.count()) === 1);
}, { width: 390, height: 844 });

summary("tasks");
