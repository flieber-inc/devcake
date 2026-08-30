// Settings-model suite: one section per Fleet/Settings view, route-driven
// nav, the unified draft ACROSS PAGES (Connections + Fleet + Settings —
// CAKE-159), legacy-route redirects, nav guard, mobile chips.
// Never confirms a Save — every flow ends in Cancel/Discard so live config
// is untouched.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { BASE, check, checked, gotoFresh, summary, withPage } from "./harness.mjs";

const POLL = 'input[aria-label="Poll interval (seconds)"]';
const GLOBAL_MAX = 'input[aria-label="Global max Devs"]';
const _here = dirname(fileURLToPath(import.meta.url));
const _contracts = JSON.parse(
  readFileSync(join(_here, "../../../docs/contracts/spa-contracts.json")));
const ATTEMPT_RESET_POLICIES = _contracts.attempt_reset_policies;

await withPage(async (page) => {
  // 1-2: bare and unknown #/config land on Fleet's first section (dev-types)
  await gotoFresh(page, "#/config");
  await page.waitForSelector("#dev-types");
  check("bare #/config redirects to #/fleet/dev-types",
    page.url().endsWith("#/fleet/dev-types"));
  await gotoFresh(page, "#/config/definitely-not-a-section");
  await page.waitForSelector("#dev-types");
  check("unknown #/config section redirects to #/fleet/dev-types",
    page.url().endsWith("#/fleet/dev-types"));

  // 3: legacy-route redirects — old bookmarks land on the new homes
  await gotoFresh(page, "#/config/pmo");
  await page.waitForSelector("#pmo");
  check("#/config/pmo redirects to the PMO page", page.url().endsWith("#/pmo"));
  await gotoFresh(page, "#/config/repository");
  await page.waitForSelector("#repository");
  check("#/config/repository redirects to Repositories",
    page.url().endsWith("#/repos"));
  await gotoFresh(page, "#/config/traffic");
  await page.waitForSelector("#policies");
  check("#/config/traffic redirects to Settings → Policies",
    page.url().endsWith("#/settings/policies"));
  await gotoFresh(page, "#/config/limits");
  await page.waitForSelector("#policies");
  check("#/config/limits redirects to Settings → Policies",
    page.url().endsWith("#/settings/policies"));
  await gotoFresh(page, "#/config/cron");
  await page.waitForSelector("#scheduled-tasks");
  check("#/config/cron redirects to Scheduled Tasks",
    page.url().endsWith("#/settings/scheduled-tasks"));
  await gotoFresh(page, "#/config/assignments");
  await page.waitForSelector("#mission-types");
  check("#/config/assignments redirects to Mission Types",
    page.url().endsWith("#/fleet/mission-types"));
  await gotoFresh(page, "#/config/skills");
  await page.waitForSelector("#skills");
  check("#/config/skills redirects to Fleet → Skills catalog",
    page.url().endsWith("#/fleet/skills"));
  check("Skills catalog page has no skill-sources section",
    (await page.locator("#skills-sources").count()) === 0);
  await gotoFresh(page, "#/logs");
  await page.waitForSelector('h1:has-text("Consoles")');
  check("#/logs redirects to Consoles", page.url().endsWith("#/consoles"));
  check("Consoles offers OpenObserve and Dagu",
    (await page.locator('a:has-text("Open OpenObserve")').count()) === 1 &&
    (await page.locator('a:has-text("Open Dagu")').count()) === 1);

  // 4: exactly one section per view; the merged view carries BOTH cards
  await gotoFresh(page, "#/fleet/dev-types");
  await page.waitForSelector("#dev-types");
  check("Dev Types view renders only the Dev Types section",
    await page.locator("#dev-types").count() === 1 &&
    await page.locator("#skills").count() === 0);
  await gotoFresh(page, "#/settings/policies");
  await page.waitForSelector("#policies");
  check("Policies view stands alone — Traffic is gone, Steward moved",
    await page.locator("#policies").count() === 1 &&
    await page.locator("#traffic").count() === 0 &&
    await page.locator("#dev-types").count() === 0);
  check("Decomposition depth lives on Counting budgets",
    (await page.locator('select[aria-label="Decomposition depth limit"]').count()) === 1 &&
    await page.locator("#policies-budgets").count() === 1);
  // CAKE-161 six domain cards + CAKE-159 Delivery (attach-merged moved here)
  check("Policies view renders the domain-grouped cards + Delivery",
    await page.locator("#policies").count() === 1 &&
    await page.locator("#policies-attempts").count() === 1 &&
    await page.locator("#policies-budgets").count() === 1 &&
    await page.locator("#policies-recovery").count() === 1 &&
    await page.locator("#policies-mirrors").count() === 1 &&
    await page.locator("#policies-memory").count() === 1 &&
    await page.locator("#policies-delivery").count() === 1);
  check("Fleet bounds carries timeout and container cgroups together",
    (await page.locator('#policies input[aria-label="Dev run timeout (minutes)"]').count()) === 1 &&
    (await page.locator('#policies input[aria-label="Container memory limit (MB)"]').count()) === 1);
  check("attach-merged toggle lives on Settings → Policies → Delivery",
    (await page.locator("text=Also attach merged change set to PMO").count()) >= 1);
  check("the Service auto-restart informational row is gone",
    (await page.locator("text=managed in compose").count()) === 0 &&
    (await page.locator("text=Service auto-restart").count()) === 0);
  check("mirror help no longer says 'no off switch'",
    (await page.locator("text=there is no off switch").count()) === 0);

  // 4b: ADR-0026 spend-discipline rows
  const attemptSelect = page.locator('select[aria-label="Attempt reset policy"]');
  check("Attempt reset row renders with a legal policy selected",
    (await attemptSelect.count()) === 1 &&
    ATTEMPT_RESET_POLICIES.includes(await attemptSelect.inputValue()));
  check("Attempt reset offers every spa-contracts policy",
    (await Promise.all(ATTEMPT_RESET_POLICIES.map((p) =>
      attemptSelect.locator(`option[value="${p}"]`).count())))
      .every((n) => n === 1));
  const brakeToggle = page.locator(
    'button[role="switch"][aria-label="Brake on missing results"]');
  check("Brake-on-missing-results switch renders with a boolean state",
    (await brakeToggle.count()) === 1 &&
    ["true", "false"].includes(await brakeToggle.getAttribute("aria-checked")));

  // 5: sidebar sub-nav highlight is route-driven
  const activeLink = page.locator('aside a[href="#/settings/policies"]');
  check("sidebar sub-nav highlights the routed section",
    ((await activeLink.getAttribute("class")) || "").includes("font-semibold"));

  // 6: ONE draft across PAGES — edit on #/pmo, then on #/settings/policies.
  // Navigate via sidebar links, never gotoFresh (a reload drops the draft);
  // the settings sub-nav is invisible from #/pmo, so hop through Settings.
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
  await page.click('aside a[href="#/settings"]');        // pmo → settings: no guard
  await page.waitForSelector("#policies");
  check("pmo → settings hop raises no nav guard",
    (await page.locator("text=unsaved change").count()) === 0 ||
    (await page.locator('[role="dialog"]').count()) === 0);
  await page.click('aside a[href="#/settings/policies"]');
  await page.waitForSelector(GLOBAL_MAX);
  const gmax = page.locator(GLOBAL_MAX);
  const gmaxBefore = await gmax.inputValue();
  await gmax.fill(String(Number(gmaxBefore) + 1));
  check("edit in Limits joins the same draft (2)", (await dirtyCount()) === "2");

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

  // 8: nav guard on leaving Settings dirty; Esc = Stay
  await page.click('aside a[href="#/runs"]');
  await checked("nav guard appears when leaving Settings dirty", async () => {
    await page.waitForSelector("text=You have 2 unsaved changes",
      { timeout: 8000 });
    return (await page.locator(
      "text=You have 2 unsaved changes").count()) >= 1;
  });
  await page.keyboard.press("Escape");
  await page.waitForTimeout(150);
  check("Esc on the nav guard stays on Settings",
    page.url().includes("#/settings/") &&
    (await page.locator("text=You have 2 unsaved changes").count()) === 0);

  // cleanup: discard the draft, verify clean
  await page.click('button:has-text("Discard changes")');
  await page.waitForTimeout(150);
  check("discard clears the dirty bar",
    (await page.locator("text=Unsaved changes").count()) === 0);
  check("discard restored the edited value",
    (await page.locator(GLOBAL_MAX).inputValue()) === gmaxBefore);

  // 9: the guard also fires leaving the PMO page dirty.
  // Connections sub-entries render only while a connection page is active.
  check("Connections sub-entries stay hidden while Settings is active",
    (await page.locator('aside a[href="#/pmo"]').count()) === 0);
  await page.click('aside a[href="#/repos"]');           // Connections parent
  await page.waitForSelector("#repository");
  await page.click('aside a[href="#/pmo"]');             // sub-entry, now visible
  await page.waitForSelector("#pmo");
  const poll2 = page.locator(POLL);
  await poll2.fill(String(Number(await poll2.inputValue()) + 1));
  await page.waitForSelector('span:has-text("Unsaved changes")');
  await page.click('aside a[href="#/runs"]');
  await checked("nav guard also fires leaving the PMO page dirty", async () => {
    await page.waitForSelector("text=You have 1 unsaved change",
      { timeout: 8000 });
    return (await page.locator(
      "text=You have 1 unsaved change").count()) >= 1;
  });
  await page.click('button:has-text("Discard & leave")');
  await page.waitForTimeout(200);
  check("discard-and-leave lands on Runs with a clean draft",
    page.url().includes("#/runs") &&
    (await page.locator("text=Unsaved changes").count()) === 0);
});

// 9b: ADR-0022 continuation + ADR-0024 mirror controls — all live in Limits
await withPage(async (page) => {
  await gotoFresh(page, "#/settings/policies");
  await page.waitForSelector("#policies");
  const maxAge = page.locator('input[aria-label="Mirror sync max age (seconds)"]');
  check("Policies view renders the mirror sync max-age input",
    (await maxAge.count()) === 1);
  check("Policies view renders the mirror LFS toggle",
    (await page.locator('button[aria-label="Mirror LFS content"], [role="switch"][aria-label="Mirror LFS content"]').count()) >= 1 ||
    (await page.locator('text=Mirror LFS content').count()) >= 1);
  const policy = page.locator('select[aria-label="Continuation policy"]');
  const maxc = page.locator('input[aria-label="Max continuations per run"]');
  check("Policies view renders the continuation policy select",
    (await policy.count()) === 1);
  check("Policies view renders the max continuations input",
    (await maxc.count()) === 1);
  const policyBefore = await policy.inputValue();
  await policy.selectOption(policyBefore === "fresh-only" ? "auto" : "fresh-only");
  const maxcBefore = await maxc.inputValue();
  await maxc.fill(String(Number(maxcBefore) + 48));  // large budgets are legal
  const maxAgeBefore = await maxAge.inputValue();
  await maxAge.fill(String(Number(maxAgeBefore) + 30));
  await page.waitForSelector('span:has-text("Unsaved changes")');
  await page.click('button:has-text("Save changes…")');
  await page.waitForSelector('[role="dialog"]');
  const review = page.locator('[role="dialog"]');
  check("save review labels the continuation policy row",
    (await review.locator("text=Continuation policy").count()) >= 1);
  check("save review labels the max continuations row",
    (await review.locator("text=Max continuations per run").count()) >= 1);
  check("save review labels the mirror sync max-age row",
    (await review.locator("text=Mirror sync max age").count()) >= 1);
  await review.locator('button:has-text("Cancel")').click();
  await page.click('button:has-text("Discard changes")');
  await page.waitForTimeout(150);
  check("continuation edits discard cleanly",
    (await page.locator("text=Unsaved changes").count()) === 0);
});

// 10: mobile — Fleet chip row; Connections chips include skill-sources
await withPage(async (page) => {
  await gotoFresh(page, "#/fleet/skills");
  const chip = page.locator('a[href="#/fleet/skills"]:visible', { hasText: "Skills" }).first();
  check("mobile Fleet chip row shows the active section",
    ((await chip.getAttribute("class")) || "").includes("border-accent"));
  check("mobile Fleet chip row has no PMO, Traffic, or Cron chips",
    (await page.locator('a[href="#/config/pmo"]:visible').count()) === 0 &&
    (await page.locator('a[href="#/pmo"]:visible').count()) === 0 &&
    (await page.locator('a[href="#/config/traffic"]:visible').count()) === 0 &&
    (await page.locator('a[href="#/config/cron"]:visible').count()) === 0);
  await page.locator('a[href="#/fleet/dev-types"]:visible', { hasText: "Dev Types" }).first().click();
  await page.waitForSelector("#dev-types");
  check("mobile Fleet chip switches to the Dev Types view",
    await page.locator("#dev-types").count() === 1 &&
    await page.locator("#skills").count() === 0);

  // ConnectionTabs: skill-sources is a third Connections chip
  await gotoFresh(page, "#/repos");
  await page.waitForSelector("#repository");
  const pmoChip = page.locator('a[href="#/pmo"]:visible').first();
  const skillChip = page.locator('a[href="#/skill-sources"]:visible').first();
  check("mobile Repositories page shows the PMO connection chip",
    (await pmoChip.count()) === 1);
  check("mobile Repositories page shows the Skill sources chip",
    (await skillChip.count()) === 1);
  await pmoChip.click();
  await page.waitForSelector("#pmo");
  check("mobile connection chip switches to the PMO page",
    page.url().endsWith("#/pmo"));
  await page.locator('a[href="#/skill-sources"]:visible').first().click();
  await page.waitForSelector("#skills-sources");
  check("mobile connection chip switches to Skill sources",
    page.url().endsWith("#/skill-sources"));
}, { width: 390, height: 844 });

summary("settings");
