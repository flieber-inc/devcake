// Action-hierarchy suite (DESIGN.md §3): one primary per header/card, the
// rest behind a ⋯ MoreMenu whose items still reach their confirm/prompt
// dialogs, and full keyboard operation of the menu. All dialogs are
// cancelled — nothing destructive ever runs.
import { check, gotoFresh, skip, summary, withPage } from "./harness.mjs";

await withPage(async (page) => {
  // 1: no bare secondary/destructive buttons on the redesigned surfaces
  for (const [hash, waitFor] of [
    ["#/config/dev-types", "#dev-types"],
    ["#/config/skills", "#skills"],
    ["#/runs", 'h1:has-text("Runs")'],
  ]) {
    await gotoFresh(page, hash);
    await page.waitForSelector(waitFor);
    for (const label of ["Rename", "Delete", "Clear runs"]) {
      check(`${hash} has no bare "${label}" button`,
        (await page.locator(`button:visible:text-is("${label}")`).count()) === 0);
    }
  }

  // 2-3: Dev Type card ⋯ reaches the Rename prompt and Delete confirm
  await gotoFresh(page, "#/config/dev-types");
  await page.waitForSelector("#dev-types");
  const devMenu = page.locator('button[aria-label^="More actions for"]').first();
  if (!(await devMenu.count())) {
    skip("Dev Type ⋯ menu flows", "no Dev Type cards on this stack");
  } else {
    await devMenu.click();
    await page.click('[role="menuitem"]:has-text("Rename")');
    await page.waitForSelector('[role="dialog"]:has-text("Rename Dev Type")');
    check("⋯ → Rename reaches the prompt dialog", true);
    await page.click('[role="dialog"] button:has-text("Cancel")');

    await devMenu.click();
    await page.click('[role="menuitem"]:has-text("Delete Dev Type")');
    await page.waitForSelector('[role="dialog"]:has-text("Delete dev type")');
    check("⋯ → Delete reaches the confirm dialog", true);
    await page.click('[role="dialog"] button:has-text("Cancel")');

    // 4: keyboard operation — open, arrows move focus, Esc returns to trigger
    await devMenu.focus();
    await page.keyboard.press("Enter");
    await page.waitForSelector('[role="menu"]');
    const focusedItem = () => page.evaluate(() => {
      const el = document.activeElement;
      return el?.getAttribute("role") === "menuitem" ? el.textContent.trim() : null;
    });
    const first = await focusedItem();
    check("opening the menu focuses the first item", first !== null);
    await page.keyboard.press("ArrowDown");
    const second = await focusedItem();
    check("ArrowDown moves focus to the next item", second !== null && second !== first);
    await page.keyboard.press("ArrowUp");
    check("ArrowUp moves focus back", (await focusedItem()) === first);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(100);
    check("Esc closes the menu and refocuses the trigger",
      (await page.locator('[role="menu"]').count()) === 0 &&
      (await page.evaluate(() =>
        document.activeElement?.getAttribute("aria-label")?.startsWith("More actions for"))));
  }

  // 5: Runs header — primary link + destructive action behind ⋯, confirmed
  await gotoFresh(page, "#/runs");
  await page.waitForSelector('h1:has-text("Runs")');
  check("Runs header keeps Open Dagu as the visible primary",
    (await page.locator('a:has-text("Open Dagu")').count()) === 1);
  await page.click('button[aria-label="More run actions"]');
  await page.click('[role="menuitem"]:has-text("Clear run history")');
  await page.waitForSelector('[role="dialog"]:has-text("Clear all run history?")');
  check("⋯ → Clear run history still confirms", true);
  await page.click('[role="dialog"] button:has-text("Cancel")');

  // 6: Skills header — one primary; secondary actions in ⋯ or a lone ghost
  await gotoFresh(page, "#/config/skills");
  await page.waitForSelector("#skills");
  const addSkill = await page.locator('button:has-text("Add skill")').count();
  if (!addSkill) {
    skip("Skills header hierarchy", "skill store disabled on this stack");
  } else {
    const menu = await page.locator('button[aria-label="More skill-store actions"]').count();
    const ghost = await page.locator('button:text-is("Restore built-in skills")').count();
    check("Skills header: primary + (⋯ menu XOR lone ghost restore)",
      addSkill === 1 && ((menu === 1 && ghost === 0) || (menu === 0 && ghost === 1)));
  }
});

summary("hierarchy");
