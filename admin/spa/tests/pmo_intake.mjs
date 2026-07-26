// Per-PMO mission-intake toggle (docs/11, PR #50): InstantZone switch on a
// *saved* PMO card, driven by /health via PUT /config/pmos/{name}/intake.
// Always restores the pre-test state so the operator's layout is untouched.
// Skips cleanly when no saved PMO card is present (empty first boot).
import { check, gotoFresh, skip, summary, withPage } from "./harness.mjs";

await withPage(async (page) => {
  const narrowPuts = [];
  const fullConfigPuts = [];
  page.on("request", (req) => {
    if (req.method() !== "PUT") return;
    const u = req.url();
    if (/\/config\/pmos\/[^/]+\/intake(?:\?|$)/.test(u)) {
      narrowPuts.push(u);
    } else if (/\/api\/v1\/config(?:\?|$)/.test(u) || /\/config(?:\?|$)/.test(u)) {
      // bare config PUT (not profiles, not pmos/.../intake)
      if (!u.includes("/config/pmos/") && !u.includes("/profiles")) {
        fullConfigPuts.push(u);
      }
    }
  });

  await gotoFresh(page, "#/config/pmo");
  await page.waitForSelector("#pmo");

  // InstantZone eyebrow only appears once at least one saved card exists
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

  const zone = page.locator("#pmo", {
    has: page.locator("text=applies immediately — does not wait for Save"),
  }).first();
  check("intake InstantZone is present on a saved card",
    (await zone.count()) >= 1
    || (await page.locator("#pmo text=applies immediately").count()) >= 1
    || (await page.getByText("applies immediately — does not wait for Save").count()) >= 1);

  const sw = switches.first();
  const ariaLabel = await sw.getAttribute("aria-label");
  const name = (ariaLabel || "").replace(/^Mission intake for /, "");
  check("switch aria-label names the PMO instance", !!name && name !== "this PMO");

  const before = await sw.getAttribute("aria-checked");
  check("switch has a defined aria-checked", before === "true" || before === "false");

  const beforeNarrow = narrowPuts.length;
  const beforeFull = fullConfigPuts.length;
  await sw.click();

  // wait for optimistic + server round-trip to settle on the flipped value
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

  // restore
  const midNarrow = narrowPuts.length;
  await sw.click();
  await page.waitForFunction(
    ({ label, want }) => {
      const el = document.querySelector(
        `#pmo button[role="switch"][aria-label="${label}"]`,
      );
      return el && el.getAttribute("aria-checked") === want && !el.disabled;
    },
    { label: ariaLabel, want: before },
    { timeout: 15000 },
  );
  check("toggle restores the pre-test intake state",
    (await sw.getAttribute("aria-checked")) === before);
  check("restore also used the narrow endpoint",
    narrowPuts.length > midNarrow);
});

summary("pmo_intake");
