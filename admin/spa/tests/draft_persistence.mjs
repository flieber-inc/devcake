// Rendered draft behavior at the HTTP seam. Every API request is intercepted;
// these tests never write the operator's configuration.
import { check, checkedEventually, gotoFresh, summary, withPage } from "./harness.mjs";

const POLL = 'input[aria-label="Poll interval (seconds)"]';
const dirty = (page) => page.getByText(/^Unsaved changes/);

async function board(page) {
  const state = {
    cfg: { pmos: [], repos: [], poll_interval_seconds: 30, dismissed_alerts: [] },
    assignments: { ONBOARD: { dev_type: "alpha", extra_cli_args: "" } },
    failing: new Set(), writes: [], unexpected: [],
  };
  await page.route(/\/api\/v1\//, async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace("/api/v1/", "");
    const reply = (data, status = 200) => route.fulfill({
      status, contentType: "application/json", body: JSON.stringify(data),
    });
    if (req.method() === "GET") {
      if (path === "config") return reply(state.cfg);
      if (path === "assignments") return reply(state.assignments);
      if (path === "dev-types") return reply(["alpha", "beta"].map((name) => ({
        name, harness_template: "claude-code", max_concurrency: 1, skills: [], memory_repos: [],
      })));
      if (path === "runs") return reply({ runs: [], total: 0, total_runs: 0, pmo_refs: [] });
      return reply({});
    }
    state.writes.push({ path, body: req.postDataJSON() });
    if (req.method() !== "PUT" || !["config", "assignments"].includes(path)) {
      state.unexpected.push(path);
      return reply({ detail: "unexpected write" }, 405);
    }
    if (state.failing.has(path)) return reply({ detail: `${path} temporarily unavailable` }, 503);
    if (path === "config") Object.assign(state.cfg, req.postDataJSON());
    else state.assignments = req.postDataJSON();
    return reply({ ok: true });
  });
  return state;
}

async function review(page) {
  await page.getByRole("button", { name: "Save changes…", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByText(/^Review \d+ change/).waitFor();
  return dialog;
}

await withPage(async (page) => {
  const state = await board(page);
  state.failing.add("config");
  await gotoFresh(page, "#/pmo");
  await page.locator(POLL).fill("45");
  const dialog = await review(page);
  check("editing and reviewing do not persist config", state.writes.length === 0);
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  check("cancel review preserves the edited value", await page.locator(POLL).inputValue() === "45");
  await (await review(page)).getByRole("button", { name: /^Save 1 change/ }).click();
  await checkedEventually("failed save shows its error and preserves the dirty draft", async () =>
    (await page.getByRole("dialog").innerText()).includes("config temporarily unavailable") &&
    await dirty(page).count() === 1 && await page.locator(POLL).inputValue() === "45");
  check("failed config write did not alter persisted state", state.cfg.poll_interval_seconds === 30);
  state.failing.clear();
  await page.getByRole("button", { name: "Retry failed", exact: true }).click();
  await checkedEventually("successful retry closes the dialog and clears the draft", async () =>
    await page.getByRole("dialog").count() === 0 && await dirty(page).count() === 0);
  check("retry persisted the requested scalar", state.cfg.poll_interval_seconds === 45);
  await page.reload();
  await checkedEventually("saved scalar survives a fresh mount", async () =>
    await page.locator(POLL).inputValue() === "45" && await dirty(page).count() === 0);
  check("single-unit scenario made no unexpected writes", state.unexpected.length === 0);
});

await withPage(async (page) => {
  const state = await board(page);
  state.failing.add("assignments");
  await gotoFresh(page, "#/pmo");
  await page.locator(POLL).fill("45");
  await page.locator('aside a[href="#/fleet"]').click();
  await page.locator('aside a[href="#/fleet/mission-types"]').click();
  const onboard = page.locator("#mission-types tbody tr").filter({
    has: page.locator('td:text-is("ONBOARD")'),
  }).first().locator("select");
  await onboard.selectOption("beta");
  await (await review(page)).getByRole("button", { name: /^Save 2 changes/ }).click();
  await checkedEventually("partial save retains only the failed assignment edit", async () =>
    (await dirty(page).innerText()).includes("(1)") &&
    (await page.getByRole("dialog").innerText()).includes("assignments temporarily unavailable"));
  check("partial save landed config but left assignments unchanged",
    state.cfg.poll_interval_seconds === 45 && state.assignments.ONBOARD.dev_type === "alpha");
  const beforeRetry = state.writes.length;
  state.failing.clear();
  await page.getByRole("button", { name: "Retry failed", exact: true }).click();
  await checkedEventually("retry converges to a clean draft", async () =>
    await dirty(page).count() === 0 && await page.getByRole("dialog").count() === 0);
  check("retry sends only the failed unit",
    JSON.stringify(state.writes.slice(beforeRetry).map((w) => w.path)) === '["assignments"]');
  check("retried assignment is persisted", state.assignments.ONBOARD.dev_type === "beta");
  check("partial-save scenario made no unexpected writes", state.unexpected.length === 0);
});

await withPage(async (page) => {
  const state = await board(page);
  state.failing.add("config");
  await gotoFresh(page, "#/pmo");
  await page.locator(POLL).fill("45");
  await page.locator('aside a[href="#/runs"]').click();
  await page.getByRole("button", { name: "Save & leave…", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: /^Save 1 change/ }).click();
  await checkedEventually("failed Save and leave keeps the user on the draft page", async () =>
    page.url().endsWith("#/pmo") &&
    (await page.getByRole("dialog").innerText()).includes("config temporarily unavailable"));
  state.failing.clear();
  await page.getByRole("button", { name: "Retry failed", exact: true }).click();
  await checkedEventually("successful retry completes the requested navigation", async () =>
    page.url().endsWith("#/runs") && await page.getByRole("dialog").count() === 0);
  check("Save and leave retry persisted config", state.cfg.poll_interval_seconds === 45);
  check("navigation scenario made no unexpected writes", state.unexpected.length === 0);
});

await withPage(async (page) => {
  const state = await board(page);
  state.failing.add("config");
  await gotoFresh(page, "#/pmo");
  await page.locator(POLL).fill("45");
  await page.locator('aside a[href="#/runs"]').click();
  await page.getByRole("button", { name: "Save & leave…", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: /^Save 1 change/ }).click();
  await page.getByRole("button", { name: "Close", exact: true }).click();
  check("closing failed save preserves the draft and cancels navigation",
    page.url().endsWith("#/pmo") && await page.locator(POLL).inputValue() === "45");
  await page.locator('aside a[href="#/runs"]').click();
  await page.getByRole("button", { name: "Discard & leave", exact: true }).click();
  await checkedEventually("a new navigation attempt can discard and leave", async () =>
    page.url().endsWith("#/runs") && await page.getByRole("dialog").count() === 0);
  check("discard after failure leaves server config untouched", state.cfg.poll_interval_seconds === 30);
});

summary("draft_persistence");
