// Per-PMO mission-intake toggle (docs/11, PR #50): InstantZone switch on a
// *saved* PMO card, driven by /health via PUT /config/pmos/{name}/intake.
// Always restores the pre-test state so the operator's layout is untouched.
// Skips cleanly when no saved PMO card is present (empty first boot).
import { check, gotoFresh, skip, summary, withPage } from "./harness.mjs";

const POLL = 'input[aria-label="Poll interval (seconds)"]';

async function waitChecked(page, label, want, timeout = 15000) {
  await page.waitForFunction(
    ({ label: l, want: w }) => {
      const el = document.querySelector(
        `#pmo button[role="switch"][aria-label="${l}"]`,
      );
      return el && el.getAttribute("aria-checked") === w && !el.disabled;
    },
    { label, want },
    { timeout },
  );
}

async function saveDraft(page) {
  await page.click('button:has-text("Save changes")');
  const dlg = page.locator('[role="dialog"]').filter({ hasText: /Review \d+ change/ }).first();
  await dlg.waitFor({ timeout: 8000 });
  await dlg.locator("button").filter({ hasText: /^Save \d+ change/ }).click();
  // reload after save clears the dirty bar
  await page.waitForSelector("text=Unsaved changes", {
    state: "detached",
    timeout: 20000,
  }).catch(async () => {
    // flash "Saved" may appear instead
    await page.waitForTimeout(500);
  });
}

await withPage(async (page) => {
  const narrowPuts = [];
  const fullConfigPuts = [];
  page.on("request", (req) => {
    if (req.method() !== "PUT") return;
    const u = req.url();
    if (/\/config\/pmos\/[^/]+\/intake(?:\?|$)/.test(u)) {
      narrowPuts.push(u);
    } else if (/\/api\/v1\/config(?:\?|$)/.test(u) || /\/config(?:\?|$)/.test(u)) {
      if (!u.includes("/config/pmos/") && !u.includes("/profiles")) {
        fullConfigPuts.push(u);
      }
    }
  });

  await gotoFresh(page, "#/pmo");
  await page.waitForSelector("#pmo");

  const switches = page.locator(
    '#pmo button[role="switch"][aria-label^="Mission intake for "]',
  );
  const n = await switches.count();
  if (n === 0) {
    skip("per-PMO intake toggle", "no saved PMO cards (empty first boot)");
    summary("pmo_intake");
    return;
  }

  check("saved PMO card exposes a Mission intake switch", n >= 1);

  check("intake InstantZone is present on a saved card",
    (await page.getByText("applies immediately — does not wait for Save").count()) >= 1);

  const sw = switches.first();
  const ariaLabel = await sw.getAttribute("aria-label");
  const name = (ariaLabel || "").replace(/^Mission intake for /, "");
  check("switch aria-label names the PMO instance", !!name && name !== "this PMO");

  const before = await sw.getAttribute("aria-checked");
  check("switch has a defined aria-checked", before === "true" || before === "false");

  const beforeNarrow = narrowPuts.length;
  const beforeFull = fullConfigPuts.length;
  await sw.click();
  await page.waitForFunction(
    ({ label, prev }) => {
      const el = document.querySelector(
        `#pmo button[role="switch"][aria-label="${label}"]`,
      );
      return el && el.getAttribute("aria-checked") !== prev && !el.disabled;
    },
    { label: ariaLabel, prev: before },
    { timeout: 15000 },
  );

  const mid = await sw.getAttribute("aria-checked");
  check("toggle flips aria-checked after click", mid !== before && (mid === "true" || mid === "false"));
  check("toggle used the narrow /config/pmos/{name}/intake endpoint",
    narrowPuts.length > beforeNarrow,
    `got ${narrowPuts.length - beforeNarrow} narrow PUT(s): ${narrowPuts.slice(beforeNarrow).join(" ")}`);
  check("toggle did not rewrite config via full PUT /config",
    fullConfigPuts.length === beforeFull,
    `unexpected full config PUTs: ${fullConfigPuts.slice(beforeFull).join(" ")}`);

  // ── Save-revert regression: dirty an unrelated field, Save, intake survives ──
  const poll = page.locator(POLL);
  await poll.waitFor({ timeout: 5000 });
  const pollBefore = await poll.inputValue();
  const pollDirty = String(Number(pollBefore) + 1);
  await poll.fill(pollDirty);
  await page.waitForSelector('span:has-text("Unsaved changes")');
  const fullBeforeSave = fullConfigPuts.length;
  await saveDraft(page);
  check("unrelated Save issued a full PUT /config (expected)",
    fullConfigPuts.length > fullBeforeSave);
  // health may refresh after reload — wait for switch to still show mid
  await page.waitForTimeout(800);
  const afterSave = await sw.getAttribute("aria-checked");
  check("intake state survives an unrelated draft Save",
    afterSave === mid,
    `wanted aria-checked=${mid}, got ${afterSave}`);

  // restore poll (real write — leave operator config as found)
  await poll.fill(pollBefore);
  if ((await page.locator('span:has-text("Unsaved changes")').count()) > 0) {
    await saveDraft(page);
  }

  // restore intake
  if ((await sw.getAttribute("aria-checked")) !== before) {
    await sw.click();
    await waitChecked(page, ariaLabel, before);
  }
  check("toggle restores the pre-test intake state",
    (await sw.getAttribute("aria-checked")) === before);
  check("restore path used the narrow endpoint at least twice overall",
    narrowPuts.length >= 2);
});

// Repo picker (2026-08-02 bulk-scale): selected chips render first in
// SELECTION order with the default badge on chip 1; non-first selected
// chips carry a pin "make default" affordance that reorders the draft.
// Everything here ends in Discard — live config untouched.
await withPage(async (page) => {
  await gotoFresh(page, "#/pmo");
  await page.waitForSelector("#pmo");
  const defaultChip = page.locator('#pmo button:has-text("· default")');
  if ((await defaultChip.count()) === 0) {
    skip("repo picker order affordances", "no PMO with ≥1 work repo on this stack");
  } else {
    check("the first selected work repo wears the default badge",
      (await defaultChip.count()) >= 1);
    const pin = page.locator('#pmo button[aria-label^="Make "]').first();
    if ((await pin.count()) === 0) {
      skip("make-default reorders the draft", "PMO has a single work repo");
    } else {
      const newDefault = (await pin.getAttribute("aria-label"))
        .replace(/^Make | the default$/g, "");
      await pin.click();
      await page.waitForSelector('span:has-text("Unsaved changes")');
      check("make-default reorders the draft (DirtyBar appears)",
        (await page.locator(`#pmo button:has-text("${newDefault} · default")`).count()) === 1);
      await page.click('button:has-text("Discard changes")');
      await page.waitForTimeout(200);
      check("discard restores the original default",
        (await page.locator("text=Unsaved changes").count()) === 0);
    }
    // small catalogs keep the zero-friction chip cloud — no search box
    check("catalog search stays hidden under the threshold",
      (await page.locator('#pmo input[aria-label^="Search "]').count()) === 0 ||
      (await page.locator('#pmo button').count()) > 12);
  }
});

summary("pmo_intake");
