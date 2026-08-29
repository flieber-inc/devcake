// Dev Types roster suite: the roster grid (compact tiles + dashed New
// tile) and the editor modal keep the draft semantics — edits ride the
// page-level Save, never a per-modal save. All dialogs are cancelled and the
// draft is discarded — nothing is ever saved or destroyed.
import { check, checked, gotoFresh, skip, summary, withPage } from "./harness.mjs";

await withPage(async (page) => {
  await gotoFresh(page, "#/config/dev-types");
  await page.waitForSelector("#dev-types");

  // 1: roster grid — the dashed tile is the ONLY create affordance (no
  // redundant header button)
  check("dashed New Dev Type row is the sole create affordance",
    (await page.locator('#dev-types button:has-text("New Dev Type")').count()) === 1);

  const tiles = page.locator('button[aria-label^="Edit dev type"]');
  if (!(await tiles.count())) {
    skip("Dev Type editor flows", "no Dev Types on this stack");
  } else {
    // status column is always informative (founder follow-up 2026-08-02:
    // an empty-when-clean badge slot read as broken) — every row shows
    // credential readiness even with a clean draft
    check("status column shows readiness on every row",
      (await page.locator('#dev-types tr:has(button[aria-label^="Edit dev type"]) td:has-text("ready"), #dev-types tr:has(button[aria-label^="Edit dev type"]) td:has-text("no credentials")').count())
        >= (await tiles.count()));
    // 2: tile opens the editor modal with the full config surface
    await tiles.first().click();
    await checked("tile opens the editor dialog", async () => {
      await page.waitForSelector('[role="dialog"]', { timeout: 8000 });
      return (await page.locator('[role="dialog"]').count()) >= 1;
    });
    check("editor has harness/model fields",
      (await page.locator('[role="dialog"] :text("Harness template")').count()) >= 1 &&
      (await page.locator('[role="dialog"] :text-is("Model")').count()) >= 1);
    check("editor keeps the credentials InstantZone",
      (await page.locator('[role="dialog"] :text("credentials store immediately")').count()) >= 1);
    // first view: skills open, everything operational behind ONE
    // Advanced disclosure — credentials must be folded until expanded
    check("editor has exactly one Advanced disclosure",
      (await page.locator('[role="dialog"] summary:has-text("Advanced")').count()) === 1);
    check("credentials are folded on first view",
      (await page.locator('[role="dialog"] :text("credentials store immediately"):visible').count()) === 0);
    check("header shows credential readiness",
      (await page.locator('[role="dialog"] :text("no credentials"), [role="dialog"] :text("credentials ready")').count()) >= 1);
    check("editor has no per-modal save button",
      (await page.locator('[role="dialog"] button:has-text("Save")').count()) === 0);

    // 3: draft semantics — a model edit rides the page draft, not the modal
    const model = page.locator('[role="dialog"] input[placeholder*="harness default"], [role="dialog"] input[placeholder^="e.g."]').first();
    const before = await model.inputValue();
    await model.fill(`${before}-uicheck`);
    await page.locator('[role="dialog"] button:has-text("Close")').click();
    await checked("closing the editor keeps the edit in the draft (DirtyBar)",
      async () => {
        await page.waitForSelector(':text("Unsaved changes")',
          { timeout: 8000 });
        return (await page.locator(':text("Unsaved changes")').count()) >= 1;
      });
    // roster is a table (2026-08-02 re-decision): the badge is a sibling
    // cell of the edit button, so assert row-scoped, not button-descendant
    check("the edited row is badged",
      (await page.locator('#dev-types tr:has(button[aria-label^="Edit dev type"]) span:text("unsaved changes"):visible').count()) >= 1);
    await page.locator('button:has-text("Discard changes")').click();
    await page.waitForTimeout(300);
    check("discard clears the draft",
      (await page.locator('button:has-text("Discard changes")').count()) === 0);
  }

  // 4: dashed row reaches the create dialog — cancelled
  await page.locator('#dev-types button:has-text("New Dev Type")').last().click();
  await checked("dashed row reaches the create dialog", async () => {
    await page.waitForSelector('[role="dialog"]:has-text("New Dev Type")',
      { timeout: 8000 });
    return (await page.locator(
      '[role="dialog"]:has-text("New Dev Type")').count()) === 1;
  });
  await page.click('[role="dialog"] button:has-text("Cancel")');

  // 5: row ⋯ Clone opens PromptDialog (cancelled — no server write)
  const more = page.locator('#dev-types button[aria-label^="More actions for"]').first();
  if (!(await more.count())) {
    skip("Clone MoreMenu", "no Dev Type rows on this stack");
  } else {
    await more.click();
    await checked("row MoreMenu lists Clone", async () => {
      await page.waitForSelector('[role="menuitem"]:has-text("Clone"), button:has-text("Clone")',
        { timeout: 5000 });
      return (await page.locator(':text-is("Clone")').count()) >= 1;
    });
    await page.locator(':text-is("Clone")').first().click();
    await checked("Clone opens PromptDialog", async () => {
      await page.waitForSelector('[role="dialog"]:has-text("Clone Dev Type")',
        { timeout: 8000 });
      return (await page.locator(
        '[role="dialog"]:has-text("Clone Dev Type")').count()) === 1;
    });
    check("Clone dialog mentions credentials are not copied",
      (await page.locator('[role="dialog"]:has-text("Credentials are not copied")').count()) >= 1);
    await page.click('[role="dialog"] button:has-text("Cancel")');
  }
});

summary("devtypes");
