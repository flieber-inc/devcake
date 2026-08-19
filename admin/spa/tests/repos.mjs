// Unused-adapter hygiene (2026-08-01 incident): the Repositories header ⋯
// offers "Remove unused repositories…" whose dialog is honest about token
// deletion and lists what it would drop. Every dialog is CANCELLED — this
// suite never mutates the draft, never saves.
import { check, gotoFresh, skip, summary, withPage } from "./harness.mjs";

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

  // the name filter appears past 5 repos (2026-08 reviewer round — it used
  // to wait for the 30-card render cap); on a small fleet the input stays
  // hidden (a leftover filter could otherwise hide cards invisibly)
  const cards = await page.locator('#repository .rounded-card.border').count();
  check("repo filter appears only past 5 repos",
    (cards > 5) ===
    ((await page.locator('input[aria-label="Filter repositories by name"]').count()) === 1));

  // 2026-08 reviewer round: collapsed summary rows. ≤3 repos start
  // expanded; collapsing shows a one-line summary; clicking it re-expands.
  // Pure view state — no draft edit, nothing to save.
  // >3 repos start collapsed, ≤3 expanded — normalize to ONE expanded card
  // first so the round-trip below runs on any fleet size.
  // CI compose boots with an empty repositories list (no seeded cards), so
  // skip the collapse round-trip when there is nothing to collapse.
  const collapseBtn = page.locator('button[aria-label^="Collapse repository"]');
  const summaryRow = page.locator('[data-testid="repo-summary-row"]');
  if ((await collapseBtn.count()) === 0 && (await summaryRow.count()) === 0) {
    skip("collapsing a repo card leaves a summary row", "no repository cards on this stack");
    skip("clicking the summary row re-expands a card", "no repository cards on this stack");
  } else {
    if ((await collapseBtn.count()) === 0) {
      await summaryRow.first().click();
      await page.waitForTimeout(100);
    }
    const before = await summaryRow.count();
    await collapseBtn.first().click();
    await page.waitForTimeout(100);
    check("collapsing a repo card leaves a summary row",
      (await summaryRow.count()) === before + 1);
    await summaryRow.first().click();
    await page.waitForTimeout(100);
    check("clicking the summary row re-expands a card",
      (await collapseBtn.count()) >= 1);
  }
});

summary("repos");
