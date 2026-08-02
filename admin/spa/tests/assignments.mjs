// ADR-0019 per-PMO assignment overrides: the global table plus one override
// block per configured PMO instance; a row set there replaces the global
// assignment wholesale, an inherit row follows the global table. Never
// confirms a Save — the flow restores the original selection so the dirty
// bar clears and live config is untouched.
import { check, gotoFresh, skip, summary, withPage } from "./harness.mjs";

const MTS = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];

await withPage(async (page) => {
  await gotoFresh(page, "#/config/mission-types");
  await page.waitForSelector("#mission-types");

  for (const mt of MTS) {
    check(`global table shows a ${mt} row`,
      (await page.locator(`#mission-types td:text-is("${mt}")`).count()) >= 1);
  }

  const blocks = page.locator('#mission-types h4:has-text("Overrides —")');
  const nBlocks = await blocks.count();
  if (nBlocks === 0) {
    skip("override blocks", "no PMO instance configured (empty first boot)");
  } else {
    // every configured instance gets a block; every row offers inherit
    const rowSelects = page.locator("#mission-types table").nth(1).locator("select");
    check("first override block renders one select per mission type",
      (await rowSelects.count()) === MTS.length);
    const inheritOpt = rowSelects.first().locator('option[value=""]');
    check("override select offers an inherit option naming the global type",
      ((await inheritOpt.textContent()) || "").includes("inherit —"));

    // flip the first row: overridden rows grow an args input, inherit rows
    // show the inherits-global note; restoring the selection clears the
    // dirty bar (the diff is symmetric — key removal restores the map)
    const sel = rowSelects.first();
    const original = await sel.inputValue();
    const optionValues = await sel.locator("option")
      .evaluateAll((os) => os.map((o) => o.value));
    const target = optionValues.find((v) => v !== "" && v !== original);
    if (target === undefined) {
      skip("override flip", "only one dev type configured — nothing to flip to");
    } else {
      await sel.selectOption(target);
      await page.waitForSelector('span:has-text("Unsaved changes")');
      check("setting an override dirties the draft", true);
      check("an overridden row exposes its own CLI-args input",
        (await page.locator("#mission-types table").nth(1)
          .locator('input[placeholder="e.g. --max-turns 15"]').count()) >= 1);

      // the save review names the override row — then Cancel, never Save
      await page.click('button:has-text("Save changes…")');
      const dlg = page.locator('[role="dialog"]');
      await dlg.waitFor();
      check("save review labels the row as an instance override",
        (await dlg.locator('text=override (PMO').count()) >= 1);
      await dlg.locator('button:has-text("Cancel")').click();

      await sel.selectOption(original);
      await page.waitForSelector('span:has-text("Unsaved changes")',
        { state: "detached", timeout: 5000 });
      check("restoring the selection clears the dirty bar (symmetric diff)", true);
    }
  }
});

summary("assignments");
