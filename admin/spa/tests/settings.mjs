// Settings-model suite: one config section per view, route-driven nav, the
// unified draft ACROSS PAGES (Configuration + the PMO page under Adapters —
// 2026-08-02 nav reorg), legacy-route redirects, nav guard, mobile chips.
// Never confirms a Save — every flow ends in Cancel/Discard so live config
// is untouched.
import { BASE, check, gotoFresh, summary, withPage } from "./harness.mjs";

const POLL = 'input[aria-label="Poll interval (seconds)"]';
const GLOBAL_MAX = 'input[aria-label="Global max Devs"]';

await withPage(async (page) => {
  // 1-2: bare and unknown #/config land on the first section (dev-types)
  await gotoFresh(page, "#/config");
  await page.waitForSelector("#dev-types");
  check("bare #/config redirects to #/config/dev-types",
    page.url().endsWith("#/config/dev-types"));
  await gotoFresh(page, "#/config/definitely-not-a-section");
  await page.waitForSelector("#dev-types");
  check("unknown section redirects to #/config/dev-types",
    page.url().endsWith("#/config/dev-types"));

  // 3: legacy-route redirects — old bookmarks land on the new homes
  await gotoFresh(page, "#/config/pmo");
  await page.waitForSelector("#pmo");
  check("#/config/pmo redirects to the PMO page", page.url().endsWith("#/pmo"));
  await gotoFresh(page, "#/config/traffic");
  await page.waitForSelector("#traffic");
  check("#/config/traffic redirects to the merged Limits & traffic view",
    page.url().endsWith("#/config/limits"));
  await gotoFresh(page, "#/config/assignments");
  await page.waitForSelector("#mission-types");
  check("#/config/assignments redirects to Mission Types",
    page.url().endsWith("#/config/mission-types"));
  await gotoFresh(page, "#/logs");
  await page.waitForSelector('h1:has-text("Consoles")');
  check("#/logs redirects to Consoles", page.url().endsWith("#/consoles"));
  check("Consoles offers OpenObserve and Dagu",
    (await page.locator('a:has-text("Open OpenObserve")').count()) === 1 &&
    (await page.locator('a:has-text("Open Dagu")').count()) === 1);

  // 4: exactly one section per view; the merged view carries BOTH cards
  await gotoFresh(page, "#/config/dev-types");
  await page.waitForSelector("#dev-types");
  check("Dev Types view renders only the Dev Types section",
    await page.locator("#dev-types").count() === 1 &&
    await page.locator("#skills").count() === 0);
  await gotoFresh(page, "#/config/limits");
  await page.waitForSelector("#limits");
  check("Limits & traffic view renders both merged sections",
    await page.locator("#limits").count() === 1 &&
    await page.locator("#traffic").count() === 1 &&
    await page.locator("#dev-types").count() === 0);

  // 5: sidebar sub-nav highlight is route-driven
  const activeLink = page.locator('aside a[href="#/config/limits"]');
  check("sidebar sub-nav highlights the routed section",
    ((await activeLink.getAttribute("class")) || "").includes("font-semibold"));

  // 6: ONE draft across PAGES — edit on #/pmo, then on #/config/limits.
  // Navigate via sidebar links, never gotoFresh (a reload drops the draft);
  // the config sub-nav is invisible from #/pmo, so hop through the
  // Configuration NavItem.
  const dirtyCount = async () => {
    const bar = page.locator('span:has-text("Unsaved changes")').first();
    return (await bar.count()) ? ((await bar.textContent()).match(/\((\d+)\)/) || [])[1] : null;
  };
  await gotoFresh(page, "#/pmo");
  await page.waitForSelector("#pmo");
  const poll = page.locator(POLL);
  const pollBefore = await poll.inputValue();
  await poll.fill(String(Number(pollBefore) + 1));
  await page.waitForSelector('span:has-text("Unsaved changes")');
  check("edit on the PMO page marks the draft dirty (1)", (await dirtyCount()) === "1");
  await page.click('aside a[href="#/config"]');          // pmo → config: no guard
  await page.waitForSelector("#dev-types");
  check("pmo → config hop raises no nav guard",
    (await page.locator("text=unsaved change").count()) === 0 ||
    (await page.locator('[role="dialog"]').count()) === 0);
  await page.click('aside a[href="#/config/limits"]');
  await page.waitForSelector(GLOBAL_MAX);
  const gmax = page.locator(GLOBAL_MAX);
  const gmaxBefore = await gmax.inputValue();
  await gmax.fill(String(Number(gmaxBefore) + 1));
  check("edit in Limits & traffic joins the same draft (2)", (await dirtyCount()) === "2");

  // 7: Save opens the review dialog listing both edits — then Cancel
  await page.click('button:has-text("Save changes…")');
  await page.waitForSelector('[role="dialog"]');
  const review = page.locator('[role="dialog"]');
  check("save review lists both edits",
    (await review.locator("text=Review 2 changes").count()) === 1);
  check("review shows the Poll interval row",
    (await review.locator("text=Poll interval").count()) >= 1);
  await review.locator('button:has-text("Cancel")').click();
  check("cancelling the review keeps the draft dirty", (await dirtyCount()) === "2");

  // 8: nav guard on leaving Config dirty; Esc = Stay
  await page.click('aside a[href="#/runs"]');
  await page.waitForSelector("text=You have 2 unsaved changes");
  check("nav guard appears when leaving Config dirty", true);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(150);
  check("Esc on the nav guard stays on Config",
    page.url().includes("#/config/") &&
    (await page.locator("text=You have 2 unsaved changes").count()) === 0);

  // cleanup: discard the draft, verify clean
  await page.click('button:has-text("Discard changes")');
  await page.waitForTimeout(150);
  check("discard clears the dirty bar",
    (await page.locator("text=Unsaved changes").count()) === 0);
  check("discard restored the edited value",
    (await page.locator(GLOBAL_MAX).inputValue()) === gmaxBefore);

  // 9: the guard also fires leaving the PMO page dirty (the new regex leg)
  await page.click('aside a[href="#/pmo"]');
  await page.waitForSelector("#pmo");
  const poll2 = page.locator(POLL);
  await poll2.fill(String(Number(await poll2.inputValue()) + 1));
  await page.waitForSelector('span:has-text("Unsaved changes")');
  await page.click('aside a[href="#/runs"]');
  await page.waitForSelector("text=You have 1 unsaved change");
  check("nav guard also fires leaving the PMO page dirty", true);
  await page.click('button:has-text("Discard & leave")');
  await page.waitForTimeout(200);
  check("discard-and-leave lands on Runs with a clean draft",
    page.url().includes("#/runs") &&
    (await page.locator("text=Unsaved changes").count()) === 0);
});

// 10: mobile — chip row switches sections, active chip highlighted; no
// PMO/Traffic chips remain
await withPage(async (page) => {
  await gotoFresh(page, "#/config/skills");
  const chip = page.locator('a[href="#/config/skills"]:visible', { hasText: "Skills" }).first();
  check("mobile chip row shows the active section",
    ((await chip.getAttribute("class")) || "").includes("border-accent"));
  check("mobile chip row has no PMO or Traffic chips",
    (await page.locator('a[href="#/config/pmo"]:visible').count()) === 0 &&
    (await page.locator('a[href="#/config/traffic"]:visible').count()) === 0);
  await page.locator('a[href="#/config/limits"]:visible', { hasText: "Limits" }).first().click();
  await page.waitForSelector("#limits");
  check("mobile chip switches to the merged view",
    await page.locator("#limits").count() === 1 &&
    await page.locator("#traffic").count() === 1 &&
    await page.locator("#skills").count() === 0);
}, { width: 390, height: 844 });

summary("settings");
