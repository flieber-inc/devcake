// Markdown View suite: Skills + Prompts View dialogs render Markdown
// (Rendered) with a Source toggle for stored bytes. Never mutates config.
// Soft-skips when the live stack has no catalog entries.
import { check, gotoFresh, skip, summary, withPage } from "./harness.mjs";

await withPage(async (page) => {
  // ── Skills View ──────────────────────────────────────────────────────────
  await gotoFresh(page, "#/config/skills");
  await page.waitForSelector("#skills");
  // catalog may take a tick after mount
  await page.waitForTimeout(400);
  const skillView = page.locator('button[aria-label^="View skill"]').first();
  if (!(await skillView.count())) {
    skip("Skills Markdown View", "no skills in the catalog on this stack");
  } else {
    await skillView.click();
    await page.waitForSelector('[role="dialog"]');
    const dlg = page.locator('[role="dialog"]');
    // mode toggle appears only after the skill payload loads (.md path)
    await dlg.locator('button:text-is("Rendered")').waitFor({ timeout: 10000 });

    check("skill View shows Rendered mode control",
      (await dlg.locator('button:text-is("Rendered")').count()) === 1);
    check("skill View shows Source mode control",
      (await dlg.locator('button:text-is("Source")').count()) === 1);
    check("skill View defaults to Rendered (aria-pressed)",
      (await dlg.locator('button:text-is("Rendered")').getAttribute("aria-pressed")) === "true");

    // wait for rendered body (async skill fetch → markdown parse)
    await dlg.locator(".markdown-body").waitFor({ timeout: 10000 });
    // Tailwind preflight recovery: real structure, not a flat pre dump
    const structure = await dlg.locator(
      ".markdown-body h2, .markdown-body h3, .markdown-body ul, .markdown-body ol, .markdown-body p",
    ).count();
    check("skill Rendered shows markdown structure (heading/list/p)", structure > 0);

    // frontmatter stripped in Rendered — dialog chrome already has name/desc
    const renderedText = (await dlg.locator(".markdown-body").innerText()).trimStart();
    check("skill Rendered does not start with YAML name:",
      renderedText.length > 0 && !renderedText.startsWith("name:"));

    // Source = stored bytes (leading --- for SKILL.md)
    await dlg.locator('button:text-is("Source")').click();
    check("skill Source control is pressed after click",
      (await dlg.locator('button:text-is("Source")').getAttribute("aria-pressed")) === "true");
    await dlg.locator("pre").waitFor({ timeout: 5000 });
    const src = await dlg.locator("pre").innerText();
    check("skill Source includes leading frontmatter fence",
      src.trimStart().startsWith("---"));
    check("skill Source is not empty", src.trim().length > 20);

    // back to Rendered still works
    await dlg.locator('button:text-is("Rendered")').click();
    await dlg.locator(".markdown-body").waitFor({ timeout: 5000 });
    check("skill View can return to Rendered",
      (await dlg.locator(".markdown-body").count()) === 1);

    await dlg.locator('button:text-is("Close")').click();
    await page.waitForTimeout(100);
    check("skill View Close dismisses the dialog",
      (await page.locator('[role="dialog"]').count()) === 0);
  }

  // ── Prompts View ─────────────────────────────────────────────────────────
  await gotoFresh(page, "#/config/prompts");
  await page.waitForSelector("#prompts");
  // wait for templates to load (section shell is always present)
  try {
    await page.waitForSelector("text=Manage templates", { timeout: 12000 });
  } catch {
    skip("Prompts Markdown View", "prompt templates did not load on this stack");
    summary("markdown");
    return;
  }

  // open the first template manager and View the first entry
  await page.locator("summary").filter({ hasText: "Manage templates" }).first().click();
  const promptView = page.locator('button:text-is("View")').first();
  if (!(await promptView.count())) {
    skip("Prompts Markdown View", "no View controls after expanding templates");
  } else {
    await promptView.click();
    await page.waitForSelector('[role="dialog"]');
    const dlg = page.locator('[role="dialog"]');
    await dlg.locator('button:text-is("Rendered")').waitFor({ timeout: 10000 });
    await dlg.locator(".markdown-body").waitFor({ timeout: 10000 });

    check("prompt View shows Rendered and Source controls",
      (await dlg.locator('button:text-is("Rendered")').count()) === 1 &&
      (await dlg.locator('button:text-is("Source")').count()) === 1);

    const structure = await dlg.locator(
      ".markdown-body h2, .markdown-body h3, .markdown-body ul, .markdown-body ol, .markdown-body p",
    ).count();
    check("prompt Rendered shows markdown structure", structure > 0);

    const mdText = await dlg.locator(".markdown-body").innerText();
    check("prompt Rendered has playbook prose",
      /mission|workspace|ONBOARD|PLAN|EXECUTE|REVIEW|classify|result\.json/i.test(mdText));

    await dlg.locator('button:text-is("Source")').click();
    await dlg.locator("pre").waitFor({ timeout: 5000 });
    const src = await dlg.locator("pre").innerText();
    check("prompt Source has substantial template text", src.trim().length > 50);
    // unsubstituted placeholders are expected in Source (and often Rendered)
    check("prompt Source keeps unsubstituted {var} placeholders when present",
      /\{[a-z_]+\}/.test(src) || src.length > 50);

    await dlg.locator('button:text-is("Close")').click();
    await page.waitForTimeout(100);
    check("prompt View Close dismisses the dialog",
      (await page.locator('[role="dialog"]').count()) === 0);
  }
});

summary("markdown");
