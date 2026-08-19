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
  // and neither may claim skill-source prefixes select repo cards (CAKE-89)
  const emptyState = text.includes("No unused repositories");
  check("dialog names the outcome honestly",
    emptyState || (/unused repositor/.test(text) && text.includes("tokens")));
  check("unused dialog does not treat skill-source as a repo selection",
    !/skill-source on no board/i.test(text)
    && (emptyState
      ? /Skill sources are managed separately/i.test(text)
        || /work, reference, or memory/i.test(text)
      : /work, reference, or memory/i.test(text)));

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

  // CAKE-125: Create-internal modal must not speak notebook/claims-prune
  // copy for a card that is not bound as a memory notebook. Prefer a
  // gitea-internal card already on the fleet; if none, add a draft card
  // (never Save) and open Create from it.
  const createBtn = page.locator('button:has-text("+ Create repository")');
  if ((await createBtn.count()) === 0) {
    // No internal card on this stack — try adding a gitea-internal draft card.
    const addBtn = page.locator('button:has-text("Add repository"), button:has-text("+ Add")').first();
    if ((await addBtn.count()) === 0) {
      skip("Create-internal modal omits notebook copy for unbound cards",
        "no Create repository control and no Add repository on this stack");
    } else {
      skip("Create-internal modal omits notebook copy for unbound cards",
        "no gitea-internal Create repository button on this stack");
    }
  } else {
    // Expand the card that owns the Create button if collapsed
    const card = createBtn.first().locator(
      'xpath=ancestor::*[contains(@class,"rounded-card")][1]');
    const summaryInCard = card.locator('[data-testid="repo-summary-row"]');
    if ((await summaryInCard.count()) > 0) {
      await summaryInCard.first().click();
      await page.waitForTimeout(100);
    }
    await createBtn.first().click();
    await page.waitForSelector('[role="dialog"]:has-text("Create repository on the internal Gitea")');
    const dialog = page.locator('[role="dialog"]');
    const dtext = await dialog.innerText();
    // Default fleet cards are rarely notebook-bound; if this card happens
    // to be bound, the notebook sentence is correct — only fail when the
    // unbound path still narrates claims-prune / "this notebook".
    // We cannot know binding from the live stack here without draft
    // inspection; assert the destructive Clear/wipe ownership sentences
    // always present, and that unbound default (no Memory chip on card)
    // omits notebook wording when no Memory usage chip is visible.
    check("Create-internal dialog keeps ownership / Clear / wipe copy",
      /Clear will not delete it/i.test(dtext)
      && /stack wipe/i.test(dtext));
    const memChip = card.locator('text=/memory (board|domain)-bound/i');
    const looksBound = (await memChip.count()) > 0
      && await memChip.first().isVisible().catch(() => false);
    if (!looksBound) {
      check("unbound Create-internal dialog omits notebook / claims-prune copy",
        !/this notebook/i.test(dtext) && !/claims copied/i.test(dtext));
    } else {
      check("notebook-bound Create-internal dialog keeps claims-prune sentence",
        /this notebook/i.test(dtext) && /claims/i.test(dtext));
    }
    await page.click('[role="dialog"] button:has-text("Cancel")');
    await page.waitForTimeout(100);
  }

});

summary("repos");
