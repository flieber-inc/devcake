// Redesign suite: masthead answers first, oven strip, manual dark mode,
// click-popover help, and the RunTerminal dialog behavior (when the stack
// has runs). Read-only — no config edits, no destructive actions.
import { check, gotoFresh, skip, summary, withPage } from "./harness.mjs";

await withPage(async (page) => {
  // 1: Overview opens with the answer masthead + eyebrow, not a status dump
  await gotoFresh(page, "#/overview");
  await page.waitForSelector("main h1");
  const h1 = (await page.locator("main h1").first().textContent()).trim();
  check("masthead is an answer sentence",
    /(needs? you|critical warning|DevCake)/.test(h1), `got "${h1}"`);

  // 2: the four-stage oven strip renders every pipeline stage
  for (const stage of ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"]) {
    check(`oven strip shows ${stage}`,
      (await page.locator(`a[href="#/runs"]:has-text("${stage}")`).count()) === 1);
  }

  // 3: dark mode is manual (.dark on <html>) and the toggle drives it
  await page.click('button[aria-label="Dark theme"]');
  check("Dark theme toggle sets .dark on the root",
    await page.evaluate(() => document.documentElement.classList.contains("dark")));
  await page.click('button[aria-label="Light theme"]');
  check("Light theme toggle removes .dark",
    await page.evaluate(() => !document.documentElement.classList.contains("dark")));
  await page.click('button[aria-label="System theme"]');   // leave as found

  // 4: help is a click-popover (works on touch/keyboard), Esc closes it
  await gotoFresh(page, "#/config/pmo");
  await page.waitForSelector("#pmo");
  const help = page.locator('button[aria-label="Help"]').first();
  await help.click();
  check("help ? opens a popover on click",
    (await page.locator('[role="tooltip"]:visible').count()) === 1);
  await page.keyboard.press("Escape");
  check("Esc closes the help popover",
    (await page.locator('[role="tooltip"]:visible').count()) === 0);

  // 5: RunTerminal — focus-trapped dialog, Esc closes, focus returns
  await gotoFresh(page, "#/runs");
  // the empty-state row renders before the first /runs fetch resolves —
  // give real rows time to arrive before concluding the stack has none
  const runBtn = page.locator('td button[title="Open the run terminal"]').first();
  const hasRuns = await runBtn.waitFor({ timeout: 8000 }).then(() => true, () => false);
  if (!hasRuns) {
    skip("RunTerminal dialog behavior", "no runs recorded on this stack");
  } else {
    await runBtn.click();
    await page.waitForSelector('[role="dialog"][aria-label^="Run terminal"]');
    check("run id opens the terminal dialog", true);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(100);
    check("Esc closes the terminal",
      (await page.locator('[role="dialog"]').count()) === 0);
    check("focus returns to the run-id button",
      await page.evaluate(() =>
        document.activeElement?.getAttribute("title") === "Open the run terminal"));
  }
});

summary("redesign");
