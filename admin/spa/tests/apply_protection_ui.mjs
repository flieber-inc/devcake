// CAKE-182: route-mocked UI — per-repo + bulk apply are opt-in ConfirmDialog
// triggers (never fire on load / connect / create). No live backend.
import { check, checked, gotoFresh, summary, withPage } from "./harness.mjs";

const REPO = {
  name: "work",
  forge: "github",
  url: "https://github.com/example-org/work",
  api_base: null,
  default_branch: "main",
  auto_merge: false,
  auto_resolve_merge_conflicts: true,
  merge_retry_window_minutes: 30,
  merge_settle_minutes: 0,
};

const PMO = {
  name: "linear1",
  system: "linear",
  team_key: "DEV",
  api_base: null,
  repos: ["work"],
  reference_repos: [],
  memory_repos: [],
  intake_paused: false,
  discovery_routing: true,
  assignments: {},
  managed: false,
};

let liveCfg = {
  pmos: [PMO],
  repos: [REPO],
  crons: [],
  dismissed_alerts: [],
  poll_interval_seconds: 30,
  adoption_mode: "opt_in",
};

let applyPosts = [];
let bulkPosts = 0;

async function mockApis(page) {
  applyPosts = [];
  bulkPosts = 0;
  await page.route(/\/api\/v1\/config$/, async (route) => {
    if (route.request().method() === "PUT") {
      const patch = route.request().postDataJSON() || {};
      liveCfg = { ...liveCfg, ...patch };
      return route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify(liveCfg),
      });
    }
    return route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify(liveCfg),
    });
  });
  await page.route(/\/api\/v1\/dev-types$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify([]),
    }));
  await page.route(/\/api\/v1\/assignments$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({}),
    }));
  await page.route(/\/api\/v1\/harnesses$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({}),
    }));
  await page.route(/\/api\/v1\/health$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        intake_paused: false,
        pmo_instances: { linear1: { intake_paused: false } },
        internal_forge: false,
        harness_pins: {},
        active_runs: 0,
        forge_protection: { work: { protected: false, requires_reviews: null } },
      }),
    }));
  await page.route(/\/api\/v1\/connections\/registry$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        pmo_systems: [
          { id: "linear", display_name: "Linear", supports_priority: true },
        ],
        forges: [
          { id: "github", display_name: "GitHub" },
          { id: "gitea", display_name: "Gitea" },
        ],
        secret_shape_prefixes: ["ghp_"],
        managed_labels_expected: 11,
      }),
    }));
  await page.route(/\/api\/v1\/secrets-check/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        conn: {
          "repo:work:token": { present: true },
          "repo:work:token_ro": { present: false },
          "repo:work:reviewer_token": { present: false },
        },
      }),
    }));
  await page.route(/\/api\/v1\/internal-repos$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ repos: [] }),
    }));
  await page.route(/\/api\/v1\/runs(?:\?.*)?$/, (route) =>
    route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({
        total: 0, total_runs: 0, runs: [], totals: null,
        pmo_refs: [], rate_card: {},
      }),
    }));
  await page.route(/\/api\/v1\/connections\/forge\/work\/apply-protection$/,
    async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      applyPosts.push("work");
      return route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          ok: true, repo: "work", outcome: "applied",
          shape: {
            require_pull_request: true,
            allow_force_push: false,
            allow_deletions: false,
            required_status_checks: [],
            required_approving_review_count: 0,
            require_status_checks_unscoped: false,
          },
        }),
      });
    });
  await page.route(/\/api\/v1\/connections\/forge\/apply-protection$/,
    async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      bulkPosts += 1;
      return route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          results: [{
            ok: true, repo: "work", outcome: "already_as_strict",
            shape: { require_status_checks_unscoped: true },
          }],
        }),
      });
    });
}

await withPage(async (page) => {
  liveCfg = {
    pmos: [PMO],
    repos: [REPO],
    crons: [],
    dismissed_alerts: [],
    poll_interval_seconds: 30,
    adoption_mode: "opt_in",
  };
  await mockApis(page);
  await gotoFresh(page, "#/repos");
  await page.waitForSelector('h1:has-text("Repositories")');

  // No apply POST on page load / connect.
  await page.waitForTimeout(400);
  check("no apply POST on Repositories load",
    applyPosts.length === 0 && bulkPosts === 0);

  // Per-repo ⋯ → Apply → Cancel must not POST.
  const collapse = page.locator('button[aria-label^="Collapse repository"]');
  if (await collapse.count()) await collapse.first().click();
  await page.waitForSelector('[data-testid="repo-summary-row"]');
  await page.locator('button[aria-label="More actions for work"]').first().click();
  await page.locator('[role="menuitem"]:has-text("Apply branch protection")').click();
  const confirm = page.locator('[role="dialog"]:has-text("Apply branch protection")');
  await confirm.waitFor({ timeout: 8000 });
  const body = await confirm.innerText();
  check("confirm names already-strict / 403 consequence",
    /already-as-strict|already as strict/i.test(body)
    && /403|admin permission/i.test(body));
  await confirm.locator('button:has-text("Cancel")').click();
  await confirm.waitFor({ state: "detached", timeout: 8000 }).catch(() => {});
  check("cancel does not POST apply", applyPosts.length === 0);

  // Confirm → applied result surfaces near the card.
  await page.locator('button[aria-label="More actions for work"]').first().click();
  await page.locator('[role="menuitem"]:has-text("Apply branch protection")').click();
  await page.waitForSelector('[role="dialog"]:has-text("Apply branch protection")');
  await page.locator('[role="dialog"] button:has-text("Apply protection now")').click();
  await checked("single apply POSTs once and shows applied", async () => {
    await page.waitForTimeout(300);
    const result = page.locator('[data-testid="apply-protection-result-work"]');
    if ((await result.count()) < 1) return false;
    const text = await result.innerText();
    return applyPosts.length === 1 && /applied/i.test(text);
  });

  // Bulk path via section ⋯.
  await page.locator('button[aria-label="More repository actions"]').click();
  await page.locator(
    '[role="menuitem"]:has-text("Apply protection to unprotected repos")').click();
  await page.waitForSelector(
    '[role="dialog"]:has-text("Apply protection to unprotected")');
  await page.locator(
    '[role="dialog"] button:has-text("Apply to unprotected repos")').click();
  await checked("bulk apply shows results modal with already_as_strict", async () => {
    await page.waitForSelector('[role="dialog"]:has-text("Apply-protection results")',
      { timeout: 8000 });
    const text = await page.locator('[role="dialog"]').innerText();
    return bulkPosts === 1 && /already as strict|already_as_strict/i.test(text);
  });
  await page.locator('[role="dialog"] button:has-text("Close")').click();
});

summary();
