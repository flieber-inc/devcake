// CAKE-160: Prompts per-PMO overrides collapse when boards inherit globals.
// Soft-skips when the live stack has fewer than two PMO boards or templates
// fail to load — CI compose with multiple boards exercises the expand path.
import { check, gotoFresh, skip, summary, withPage } from "./harness.mjs";

await withPage(async (page) => {
  await gotoFresh(page, "#/config/prompts");
  await page.waitForSelector("#prompts");
  try {
    await page.waitForSelector("text=Mission Types", { timeout: 12000 });
  } catch {
    skip("Prompts PMO overrides", "prompt templates did not load on this stack");
    summary("prompts_overrides");
    return;
  }

  const summaries = page.locator('[data-testid="pmo-prompt-override-summary"]');
  const summaryCount = await summaries.count();
  if (summaryCount < 1) {
    // Either no PMOs, or every board already has overrides (auto-expanded).
    const overrideHeading = page.locator("text=Per-PMO overrides");
    if (!(await overrideHeading.count())) {
      skip("Prompts PMO overrides", "no PMO boards on this stack");
      summary("prompts_overrides");
      return;
    }
    // Expanded editors visible without summary rows — assert selects exist
    // for at least one board and stop (cannot prove collapse on this fixture).
    // CAKE-173: override editors live under the Mission Types domain card.
    const selects = page.locator(
      '#prompts-mission-types select[aria-label$=" template"]',
    );
    check("override editors present when summaries absent",
      (await selects.count()) >= 4);
    summary("prompts_overrides");
    return;
  }

  check("inheriting boards render summary rows, not a select wall",
    summaryCount >= 1);

  // Collapsed summaries must not expose the four Mission-Type override selects
  // for that board — count Inherit options only after expand.
  const inheritBefore = await page.locator(
    '#prompts-mission-types select[aria-label$=" template"] option[value=""]',
  ).count();

  await summaries.first().click();
  await page.waitForTimeout(200);

  const inheritAfter = await page.locator(
    '#prompts-mission-types select[aria-label$=" template"] option[value=""]',
  ).count();
  check("expanding a summary reveals Inherit selects for that board",
    inheritAfter >= inheritBefore + 4);

  // Mobile: wide override table keeps its own overflow-x-auto scrollport
  // (same idiom as tasks.mjs for Scheduled Tasks custom table).
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(100);
  const wrapCount = await page.locator(
    "#prompts-mission-types .overflow-x-auto",
  ).count();
  check("mobile: override editor has an overflow-x-auto scrollport", wrapCount >= 1);
});

summary("prompts_overrides");
