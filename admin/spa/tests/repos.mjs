// Unused-adapter hygiene (2026-08-01 incident): the Repositories header ⋯
// offers "Remove unused repositories…" whose dialog is honest about token
// deletion and lists what it would drop. Every dialog is CANCELLED — this
// suite never mutates the draft, never saves.
import { check, gotoFresh, summary, withPage } from "./harness.mjs";

await withPage(async (page) => {
  await gotoFresh(page, "#/repos");
  await page.waitForSelector('h1:has-text("Repositories")');

  const menu = page.locator('button[aria-label="More repository actions"]');
  check("Repositories header has a ⋯ menu", (await menu.count()) === 1);

  await menu.click();
  const item = page.locator(
    '[role="menuitem"]:has-text("Remove unused repositories")');
  check("⋯ offers Remove unused repositories…", (await item.count()) === 1);

  await item.click();
  await page.waitForSelector('[role="dialog"]');
  const text = await page.locator('[role="dialog"]').innerText();
  // stack-dependent: either the empty state or the removal confirm — both
  // must name their outcome (the confirm must own the token-deletion truth)
  const emptyState = text.includes("No unused repositories");
  check("dialog names the outcome honestly",
    emptyState || (/unused repositor/.test(text) && text.includes("tokens")));

  await page.click('[role="dialog"] button:has-text("Cancel")');
  await page.waitForTimeout(100);
  check("dialog cancelled — nothing changed",
    (await page.locator('[role="dialog"]').count()) === 0);
});

summary("repos");
