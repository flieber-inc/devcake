// Dev Types roster suite: the Buzz-style grid (compact tiles + dashed New
// tile) and the editor modal keep the draft semantics — edits ride the
// page-level Save, never a per-modal save. All dialogs are cancelled and the
// draft is discarded — nothing is ever saved or destroyed.
import { check, gotoFresh, skip, summary, withPage } from "./harness.mjs";

await withPage(async (page) => {
  await gotoFresh(page, "#/config/dev-types");
  await page.waitForSelector("#dev-types");

  // 1: roster grid — tiles + the dashed New tile, no bare secondary actions
  check("dashed New Dev Type tile in the grid",
    (await page.locator('#dev-types button:has-text("New Dev Type")').count()) >= 2); // header + tile

  const tiles = page.locator('button[aria-label^="Edit dev type"]');
  if (!(await tiles.count())) {
    skip("Dev Type editor flows", "no Dev Types on this stack");
  } else {
    // 2: tile opens the editor modal with the full config surface
    await tiles.first().click();
    await page.waitForSelector('[role="dialog"]');
    check("tile opens the editor dialog", true);
    check("editor has harness/model fields",
      (await page.locator('[role="dialog"] :text("Harness template")').count()) >= 1 &&
      (await page.locator('[role="dialog"] :text-is("Model")').count()) >= 1);
    check("editor keeps the credentials InstantZone",
      (await page.locator('[role="dialog"] :text("credentials store immediately")').count()) >= 1);
    check("editor has the Advanced disclosure",
      (await page.locator('[role="dialog"] summary:has-text("Advanced")').count()) === 1);
    check("editor has no per-modal save button",
      (await page.locator('[role="dialog"] button:has-text("Save")').count()) === 0);

    // 3: draft semantics — a model edit rides the page draft, not the modal
    const model = page.locator('[role="dialog"] input[placeholder*="harness default"], [role="dialog"] input[placeholder^="e.g."]').first();
    const before = await model.inputValue();
    await model.fill(`${before}-uicheck`);
    await page.locator('[role="dialog"] button:has-text("Close")').click();
    await page.waitForSelector(':text("Unsaved changes")');
    check("closing the editor keeps the edit in the draft (DirtyBar)", true);
    check("the edited tile is badged",
      (await page.locator('button[aria-label^="Edit dev type"] :text("unsaved changes")').count()) >= 1);
    await page.locator('button:has-text("Discard changes")').click();
    await page.waitForTimeout(300);
    check("discard clears the draft",
      (await page.locator('button:has-text("Discard changes")').count()) === 0);
  }

  // 4: dashed tile reaches the create dialog — cancelled
  await page.locator('#dev-types button:has-text("New Dev Type")').last().click();
  await page.waitForSelector('[role="dialog"]:has-text("New Dev Type")');
  check("dashed tile reaches the create dialog", true);
  await page.click('[role="dialog"] button:has-text("Cancel")');
});

summary("devtypes");
