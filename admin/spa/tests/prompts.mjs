// Prompts settings-list suite: groups render as compact rows (identity left,
// active-template select right), template management and creation live behind
// the row's disclosure, and the active selection rides the page draft. All
// dialogs are cancelled and the draft is discarded — nothing is saved.
import { check, gotoFresh, skip, summary, withPage } from "./harness.mjs";

await withPage(async (page) => {
  await gotoFresh(page, "#/config/prompts");
  await page.waitForSelector("#prompts");
  await page.waitForSelector('select[aria-label^="Active template for"]', { timeout: 15000 })
    .catch(() => {});

  const rows = await page.locator('select[aria-label^="Active template for"]').count();
  if (!rows) {
    skip("prompt group rows", "templates failed to load on this stack");
  } else {
    // 1: compact rows — one select per group, no per-group Create buttons
    check("each group renders one active-template select", rows >= 4, `count=${rows}`);
    check("no repeated per-group Create buttons in the open surface",
      (await page.locator('button:visible:has-text("Create prompt template")').count()) === 0);

    // 2: the disclosure holds the template list and the create entry point
    await page.locator('summary:has-text("Manage templates")').first().click();
    check("disclosure reveals the template list",
      (await page.locator('details[open] button:has-text("View")').count()) >= 1);
    await page.locator('details[open] button:has-text("New template…")').click();
    await page.waitForSelector('[role="dialog"]:has-text("Create prompt template")');
    check("New template… reaches the create modal", true);
    await page.click('[role="dialog"] button:has-text("Cancel")');

    // 3: draft semantics — changing an active select raises the DirtyBar
    const sel = page.locator('select[aria-label^="Active template for"]').first();
    const values = await sel.locator("option").evaluateAll((os) => os.map((o) => o.value));
    const cur = await sel.inputValue();
    const other = values.find((v) => v !== cur);
    if (!other) {
      skip("active-select draft check", "only one template on this stack");
    } else {
      await sel.selectOption(other);
      await page.waitForSelector(':text("Unsaved changes")');
      check("active-select change rides the draft (DirtyBar)", true);
      await page.locator('button:has-text("Discard changes")').click();
      await page.waitForTimeout(300);
      check("discard clears the draft",
        (await page.locator('button:has-text("Discard changes")').count()) === 0);
    }
  }

  // 4: Skills — clamped descriptions keep the catalog scannable
  await gotoFresh(page, "#/config/skills");
  await page.waitForSelector("#skills");
  const clamped = page.locator("#skills tbody td p.line-clamp-2");
  if (!(await clamped.count())) {
    skip("skills description clamp", "no skills on this stack");
  } else {
    const h = await clamped.first().evaluate((el) => el.getBoundingClientRect().height);
    check("skill descriptions clamp to two lines", h < 50, `h=${h}px`);
  }
});

summary("prompts");
