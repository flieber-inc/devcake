// Settings-model suite: one config section per view, route-driven nav, the
// unified draft across sections, nav guard, mobile chips. Never confirms a
// Save — every flow ends in Cancel/Discard so live config is untouched.
import { BASE, check, gotoFresh, summary, withPage } from "./harness.mjs";

const POLL = 'input[aria-label="Poll interval (seconds)"]';
const GLOBAL_MAX = 'input[aria-label="Global max Devs"]';

await withPage(async (page) => {
  // 1-2: bare and unknown #/config land on the first section
  await gotoFresh(page, "#/config");
  await page.waitForSelector("#pmo");
  check("bare #/config redirects to #/config/pmo", page.url().endsWith("#/config/pmo"));
  await gotoFresh(page, "#/config/definitely-not-a-section");
  await page.waitForSelector("#pmo");
  check("unknown section redirects to #/config/pmo", page.url().endsWith("#/config/pmo"));

  // 3: exactly one section per view
  check("PMO view renders only the PMO section",
    await page.locator("#pmo").count() === 1 && await page.locator("#limits").count() === 0);
  await gotoFresh(page, "#/config/limits");
  await page.waitForSelector("#limits");
  check("Limits view renders only the Limits section",
    await page.locator("#limits").count() === 1 && await page.locator("#pmo").count() === 0);

  // 4: sidebar sub-nav highlight is route-driven
  const activeLink = page.locator('aside a[href="#/config/limits"]');
  check("sidebar sub-nav highlights the routed section",
    ((await activeLink.getAttribute("class")) || "").includes("font-semibold"));

  // 5: one draft across sections — edit in two sections, count reaches 2
  const dirtyCount = async () => {
    const bar = page.locator('span:has-text("Unsaved changes")').first();
    return (await bar.count()) ? ((await bar.textContent()).match(/\((\d+)\)/) || [])[1] : null;
  };
  await gotoFresh(page, "#/config/pmo");
  const poll = page.locator(POLL);
  const pollBefore = await poll.inputValue();
  await poll.fill(String(Number(pollBefore) + 1));
  await page.waitForSelector('span:has-text("Unsaved changes")');
  check("edit in PMO section marks the draft dirty (1)", (await dirtyCount()) === "1");
  await page.click('aside a[href="#/config/limits"]');   // same-page section switch
  await page.waitForSelector(GLOBAL_MAX);
  const gmax = page.locator(GLOBAL_MAX);
  const gmaxBefore = await gmax.inputValue();
  await gmax.fill(String(Number(gmaxBefore) + 1));
  check("edit in Limits section joins the same draft (2)", (await dirtyCount()) === "2");

  // 6: Save opens the review dialog listing both edits — then Cancel
  await page.click('button:has-text("Save changes…")');
  await page.waitForSelector('[role="dialog"]');
  const review = page.locator('[role="dialog"]');
  check("save review lists both edits",
    (await review.locator("text=Review 2 changes").count()) === 1);
  check("review shows the Poll interval row",
    (await review.locator("text=Poll interval").count()) >= 1);
  await review.locator('button:has-text("Cancel")').click();
  check("cancelling the review keeps the draft dirty", (await dirtyCount()) === "2");

  // 7: nav guard on leaving Config dirty; Esc = Stay
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
});

// 8: mobile — chip row switches sections, active chip highlighted
await withPage(async (page) => {
  await gotoFresh(page, "#/config/skills");
  const chip = page.locator('a[href="#/config/skills"]:visible', { hasText: "Skills" }).first();
  check("mobile chip row shows the active section",
    ((await chip.getAttribute("class")) || "").includes("border-accent"));
  await page.locator('a[href="#/config/limits"]:visible', { hasText: "Limits" }).first().click();
  await page.waitForSelector("#limits");
  check("mobile chip switches sections",
    await page.locator("#limits").count() === 1 && await page.locator("#skills").count() === 0);
}, { width: 390, height: 844 });

summary("settings");
