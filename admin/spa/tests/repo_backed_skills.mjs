// ADR-0039 — repo-backed skill sources in the Skill sources form: the
// Backed-by select swaps the own-remote fields for a reads-through-the-card
// hint and clears a previously typed URL; only CONFIGURED repo cards are
// offered; a dangling backing name renders as a disabled (missing) option
// with the inline draft error; a row carrying BOTH url and backed_by (stale
// rebase / out-of-band state) keeps its URL field visible so the
// mutual-exclusion error never points at an invisible field.
import { checked, gotoFresh, summary, withPage } from "./harness.mjs";

const repo = (name, url) => ({
  name, forge: "github", url, api_base: null, default_branch: "main",
  auto_merge: false, auto_resolve_merge_conflicts: true,
  merge_retry_window_minutes: 30, merge_settle_minutes: 0,
});

const src = (name, extra) => ({
  name, forge: "github", url: "", default_branch: "", subdir: "",
  backed_by: "", ...extra,
});

const CFG = {
  pmos: [],
  repos: [repo("work", "https://github.com/example-org/work"),
          repo("idle", "")],                     // unconfigured — never offered
  skill_sources: [
    src("shelf", { url: "https://github.com/example-org/skills" }),
    src("dangling", { backed_by: "ghost" }),
    src("conflicted", { url: "https://github.com/example-org/more",
                        backed_by: "work" }),
  ],
  crons: [],
  dismissed_alerts: [],
  poll_interval_seconds: 30,
  adoption_mode: "opt_in",
};

async function mockApis(page) {
  await page.route(/\/api\/v1\/config$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify(CFG),
    }));
  for (const [re, body] of [
    [/\/api\/v1\/dev-types$/, []],
    [/\/api\/v1\/assignments$/, {}],
    [/\/api\/v1\/harnesses$/, {}],
    [/\/api\/v1\/health$/, {
      intake_paused: false, pmo_instances: {}, harness_pins: {},
      active_runs: 0, internal_forge: false,
    }],
    [/\/api\/v1\/connections\/registry$/, {
      pmo_systems: [{ id: "linear", display_name: "Linear" }],
      forges: [
        { id: "github", display_name: "GitHub" },
        { id: "gitlab", display_name: "GitLab" },
        { id: "gitea", display_name: "Gitea" },
      ],
      secret_shape_prefixes: ["ghp_"],
      managed_labels_expected: 11,
    }],
    [/\/api\/v1\/secrets-check/, { conn: {} }],
    [/\/api\/v1\/internal-repos$/, { repos: [] }],
    [/\/api\/v1\/skills$/, {
      skills: [],
      store: { enabled: false, ok: false, detail: "", html_url: "" },
    }],
  ]) {
    await page.route(re, (route) =>
      route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify(body),
      }));
  }
}

await withPage(async (page) => {
  await mockApis(page);
  await gotoFresh(page, "#/skill-sources");
  await page.waitForSelector("#skills-sources");

  const backedSel = page.locator(
    'select[aria-label="Skill source 1 backed by"]');
  await checked("own-remote card shows URL field; Backed-by offers only configured repos", async () => {
    const urlField = page.locator('input[aria-label="Skill source 1 URL"]');
    const opts = await backedSel.locator("option").allInnerTexts();
    return (await urlField.count()) === 1
      && opts.includes("Own remote") && opts.includes("work")
      && !opts.some((t) => t.startsWith("idle"));
  });

  await backedSel.selectOption("work");
  await checked("picking a card hides forge/URL/tokens and shows the reads-through hint", async () => {
    const t = await page.locator("#skills-sources").innerText();
    return (await page.locator('input[aria-label="Skill source 1 URL"]').count()) === 0
      && (await page.locator('select[aria-label="Skill source 1 forge"]').count()) === 0
      && /Reads repository card/i.test(t)
      && /no second copy/i.test(t);
  });

  await checked("picking a card cleared the previously typed URL in the draft", async () => {
    await backedSel.selectOption("");
    const url = await page
      .locator('input[aria-label="Skill source 1 URL"]').inputValue();
    return url === "";
  });

  await checked("dangling backed_by renders as a disabled (missing) option + draft error", async () => {
    const sel2 = page.locator('select[aria-label="Skill source 2 backed by"]');
    const opt = sel2.locator('option[value="ghost"]');
    const t = await page.locator("#skills-sources").innerText();
    return (await opt.count()) === 1
      && (await opt.first().isDisabled())
      && (await sel2.inputValue()) === "ghost"
      && /names no repository card/.test(t) && /ghost/.test(t);
  });

  await checked("branch and folder stay editable on a backed card", async () => {
    return (await page.locator('input[aria-label="Skill source 2 branch"]').count()) === 1
      && (await page.locator('input[aria-label="Skill source 2 folder"]').count()) === 1;
  });

  await checked("url + backed_by together keeps the URL field visible with the error", async () => {
    const t = await page.locator("#skills-sources").innerText();
    return (await page.locator('input[aria-label="Skill source 3 URL"]').count()) === 1
      && /mutually exclusive/.test(t);
  });
});

summary("repo_backed_skills");
