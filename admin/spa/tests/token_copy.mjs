// Token-copy modal (Repos + PMO ⋯ menus): the SERVER decides eligibility —
// picking a source fires a dry_run whose rows drive the target list (no
// client-side family table to drift); Copy is the only non-dry POST.
// Values never appear anywhere — field names only.
import { check, checked, checkedEventually, gotoFresh, summary, withPage } from "./harness.mjs";

const repo = (name, forge, host) => ({
  name, forge,
  url: `https://${host}/example-org/${name}`,
  api_base: null,
  default_branch: "main",
  auto_merge: false,
  auto_resolve_merge_conflicts: true,
  merge_retry_window_minutes: 30,
  merge_settle_minutes: 0,
});

const pmo = (name, system, team) => ({
  name, system, team_key: team, api_base: null,
  repos: [], reference_repos: [], memory_repos: [],
  intake_paused: false, discovery_routing: true,
  plan_approval: false,
  assignments: {}, managed: false,
});

const CFG = {
  pmos: [pmo("ghboard", "github_issues", "example-org/alpha"),
         pmo("linboard", "linear", "DEV"),
         pmo("linboard2", "linear", "OPS")],
  repos: [repo("alpha", "github", "github.com"),
          repo("beta", "github", "github.com"),
          repo("gamma", "gitlab", "gitlab.com")],
  crons: [],
  dismissed_alerts: [],
  poll_interval_seconds: 30,
  adoption_mode: "opt_in",
};

// what the server would answer: eligibility is ITS call, not the SPA's
const DRY_ROWS = {
  alpha: [
    { scope: "repo", name: "beta", eligible: true,
      receives: ["token", "token_ro"], skipped: ["reviewer_token"] },
    { scope: "repo", name: "gamma", eligible: false,
      reason: "different forge (gitlab vs github)" },
    { scope: "pmo", name: "ghboard", eligible: true,
      receives: ["api_key"], skipped: [] },
    { scope: "pmo", name: "linboard", eligible: false,
      reason: "system 'linear' does not take a github token" },
    { scope: "pmo", name: "linboard2", eligible: false,
      reason: "system 'linear' does not take a github token" },
  ],
  linboard: [
    { scope: "pmo", name: "ghboard", eligible: false,
      reason: "a PMO key only fits PMO cards of the same system" },
    { scope: "pmo", name: "linboard2", eligible: true,
      receives: ["api_key"], skipped: [] },
  ],
};

let dryPosts = [];
let copyPosts = [];

async function mockApis(page) {
  dryPosts = [];
  copyPosts = [];
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
      pmo_systems: [
        { id: "linear", display_name: "Linear", supports_priority: true },
        { id: "github_issues", display_name: "GitHub Issues" },
      ],
      forges: [
        { id: "github", display_name: "GitHub" },
        { id: "gitlab", display_name: "GitLab" },
        { id: "gitea", display_name: "Gitea" },
      ],
      secret_shape_prefixes: ["ghp_"],
      managed_labels_expected: 11,
    }],
    [/\/api\/v1\/secrets-check/, {
      conn: {
        "repo:alpha:token": { present: true },
        "repo:alpha:token_ro": { present: true },
        "repo:alpha:reviewer_token": { present: false },
        "repo:beta:token": { present: false },
        "repo:beta:token_ro": { present: false },
        "repo:beta:reviewer_token": { present: true },
        "repo:gamma:token": { present: false },
        "repo:gamma:token_ro": { present: false },
        "repo:gamma:reviewer_token": { present: false },
        "pmo:ghboard:api_key": { present: false },
        "pmo:linboard:api_key": { present: true },
        "pmo:linboard2:api_key": { present: false },
      },
    }],
    [/\/api\/v1\/internal-repos$/, { repos: [] }],
    [/\/api\/v1\/runs(?:\?.*)?$/, {
      total: 0, total_runs: 0, runs: [], totals: null,
      pmo_refs: [], rate_card: {},
    }],
  ]) {
    await page.route(re, (route) =>
      route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify(body),
      }));
  }
  await page.route(/\/api\/v1\/connections\/copy-secrets$/, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = route.request().postDataJSON();
    if (body.dry_run) {
      dryPosts.push(body);
      return route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          ok: true, dry_run: true, source: body.source,
          targets: DRY_ROWS[body.source.name] || [],
        }),
      });
    }
    copyPosts.push(body);
    return route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        ok: true,
        source: { scope: "repo", name: "alpha" },
        results: [
          { scope: "repo", name: "beta",
            copied: ["token", "token_ro"], skipped: ["reviewer_token"] },
          { scope: "pmo", name: "ghboard",
            copied: ["api_key"], skipped: [] },
        ],
      }),
    });
  });
}

await withPage(async (page) => {
  await mockApis(page);
  await gotoFresh(page, "#/repos");
  await page.waitForSelector('h1:has-text("Repositories")');

  await page.locator('button[aria-label="More repository actions"]').click();
  await page.locator(
    '[role="menuitem"]:has-text("Copy tokens between connections")').click();
  const modal = page.locator(
    '[role="dialog"]:has-text("Copy tokens between connections")');
  await modal.waitFor({ timeout: 8000 });
  check("no POST of any kind before a source is picked",
    dryPosts.length === 0 && copyPosts.length === 0);

  const select = modal.locator('select[aria-label="Token copy source"]');
  // the slot suffixes and the disabled state arrive with the presence
  // check, after the modal is already open — read until they have
  await checkedEventually("source options show stored slots; empty cards disabled", async () => {
    const opts = await select.locator("option").allInnerTexts();
    const alpha = opts.find((t) => t.startsWith("alpha"));
    const gammaDisabled = await select
      .locator("option", { hasText: "gamma" }).first().isDisabled();
    return /Access token, Read-only token/.test(alpha || "") && gammaDisabled;
  });

  await select.selectOption({ index: 1 });   // alpha
  await page.waitForSelector('[role="dialog"] input[type="checkbox"]',
    { timeout: 8000 });
  await checked("picking a source fires ONE dry_run naming every other card", async () => {
    if (dryPosts.length !== 1 || !dryPosts[0].dry_run) return false;
    const names = dryPosts[0].targets.map((t) => `${t.scope}:${t.name}`).sort();
    return names.join(",") ===
      "pmo:ghboard,pmo:linboard,pmo:linboard2,repo:beta,repo:gamma";
  });
  await checked("target list renders the SERVER's eligible rows only", async () => {
    const labels = await modal.locator("label span.font-mono").allInnerTexts();
    return labels.includes("beta") && labels.includes("ghboard")
      && !labels.includes("gamma") && !labels.includes("linboard");
  });
  await checked("rows describe what actually moves", async () => {
    const t = await modal.innerText();
    return /gets Access token, Read-only token/.test(t)
      && /gets the Access token as its API key/i.test(t);
  });

  await modal.locator('button:has-text("Select all")').click();
  await modal.locator('button:has-text("Copy to 2 cards")').click();
  await page.waitForSelector('[role="dialog"]:has-text("Tokens copied")',
    { timeout: 8000 });
  await checked("one real POST with the chosen targets, values nowhere", async () => {
    if (copyPosts.length !== 1) return false;
    const b = copyPosts[0];
    const names = (b.targets || []).map((t) => `${t.scope}:${t.name}`).sort();
    return !b.dry_run && b.source?.name === "alpha"
      && names.join(",") === "pmo:ghboard,repo:beta"
      && !JSON.stringify(b).includes("ghp_");
  });
  await checked("results list field names per scope-labeled card", async () => {
    const t = await page.locator(
      '[role="dialog"]:has-text("Tokens copied")').innerText();
    return /repo beta/.test(t) && /Access token, Read-only token/.test(t)
      && /Reviewer token not stored on the source/.test(t)
      && /board ghboard/.test(t) && /API key/.test(t);
  });
  await page.locator('[role="dialog"] button:has-text("Done")').click();
});

await withPage(async (page) => {
  await mockApis(page);
  await gotoFresh(page, "#/pmo");
  await page.waitForSelector("#pmo");

  await page.locator('button[aria-label="More PMO actions"]').click();
  await page.locator(
    '[role="menuitem"]:has-text("Copy tokens between connections")').click();
  const modal = page.locator(
    '[role="dialog"]:has-text("Copy tokens between connections")');
  await modal.waitFor({ timeout: 8000 });

  const select = modal.locator('select[aria-label="Token copy source"]');
  await checked("PMO mode sources are PMO cards only", async () => {
    const opts = await select.locator("option").allInnerTexts();
    return opts.some((t) => t.startsWith("linboard"))
      && !opts.some((t) => t.startsWith("alpha"));
  });
  await select.selectOption({ index: 2 });   // linboard (ghboard, linboard, …)
  await page.waitForSelector('[role="dialog"] input[type="checkbox"]',
    { timeout: 8000 });
  await checked("linear source offers only the server-eligible linear board", async () => {
    const labels = await modal.locator("label span.font-mono").allInnerTexts();
    return labels.includes("linboard2")
      && !labels.includes("ghboard") && !labels.includes("alpha");
  });
  check("PMO page fired only the dry_run, never a copy", copyPosts.length === 0);
});

summary("token_copy");
