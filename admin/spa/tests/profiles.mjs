// Profiles suite (ADR-0013, D4): exercises the ProfilesSection lifecycle end
// to end — save-current-as, list, apply PREVIEW (never the destructive
// world-swap), rename, delete. It writes only a clearly-named throwaway
// profile (`zz-uitest-profile`) and deletes it again, so the operator's real
// profiles and live settings are untouched. The apply flow is opened for its
// preview diff and then CANCELLED — this suite never clicks "Apply profile".
import { BASE, check, gotoFresh, summary, withPage } from "./harness.mjs";

const NAME = "zz-uitest-profile";
const NAME2 = "zz-uitest-renamed";

const rowFor = (page, name) =>
  page.locator("#profiles table tbody tr", { hasText: name }).first();

async function deleteIfPresent(page, name) {
  const row = rowFor(page, name);
  if (!(await row.count())) return;
  await row.getByRole("button", { name: `More actions for profile ${name}` }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const dlg = page.locator('[role="dialog"]', { hasText: "Delete profile" }).first();
  await dlg.waitFor();
  await dlg.locator('button:has-text("Delete profile")').click();
  await row.waitFor({ state: "detached", timeout: 8000 });
}

await withPage(async (page) => {
  // 1 — the section renders on its route
  await gotoFresh(page, "#/config/profiles");
  await page.waitForSelector("#profiles");
  check("Profiles view renders the #profiles section",
    (await page.locator("#profiles").count()) === 1
    && (await page.locator("#pmo").count()) === 0);

  // pre-clean any leftover throwaway rows from a prior aborted run
  await deleteIfPresent(page, NAME);
  await deleteIfPresent(page, NAME2);

  // 2 — Save current as profile… writes a snapshot (no live-config change)
  await page.click('button:has-text("Save current as profile")');
  const saveDlg = page.locator('[role="dialog"]', { hasText: "Save current settings as a profile" }).first();
  await saveDlg.waitFor();
  await saveDlg.locator("input").fill(NAME);
  await saveDlg.locator('button:has-text("Save")').click();
  await rowFor(page, NAME).waitFor({ timeout: 8000 });
  check("Save current as profile creates the snapshot row", true);

  // 3 — it appears in the Apply select
  const applyOpt = page.locator('select[aria-label="Profile to apply"] option',
    { hasText: NAME });
  check("the new profile is offered in the Apply select",
    (await applyOpt.count()) >= 1);

  // 4 — Apply opens a PREVIEW diff, then Cancel (never apply — world-swap)
  await page.selectOption('select[aria-label="Profile to apply"]', NAME);
  await page.click('button:has-text("Apply profile")');
  const applyDlg = page.locator('[role="dialog"]', { hasText: "Apply profile" }).first();
  await applyDlg.waitFor();
  // the preview diff loads async (GET /profiles/{name}) after the dialog opens
  const preview = applyDlg.locator('[data-testid="apply-preview"]');
  await preview.waitFor({ timeout: 8000 }).catch(() => {});
  check("Apply shows the change-preview before anything applies",
    (await preview.count()) >= 1);
  await applyDlg.locator('button:has-text("Cancel")').click();
  await applyDlg.waitFor({ state: "detached", timeout: 8000 });
  check("Apply preview cancels cleanly — no world-swap", true);

  // 5 — Rename via the row action
  const row = rowFor(page, NAME);
  await row.getByRole("button", { name: `More actions for profile ${NAME}` }).click();
  await page.getByRole("menuitem", { name: "Rename" }).click();
  const renameDlg = page.locator('[role="dialog"]', { hasText: "Rename profile" }).first();
  await renameDlg.waitFor();
  const nameInput = renameDlg.locator("input");
  await nameInput.fill("");
  await nameInput.fill(NAME2);
  await renameDlg.locator('button:has-text("Rename")').click();
  await rowFor(page, NAME2).waitFor({ timeout: 8000 });
  check("Rename moves the profile to the new name",
    (await rowFor(page, NAME2).count()) === 1
    && (await rowFor(page, NAME).count()) === 0);

  // 6 — Delete (cleanup) via the confirm dialog — real settings untouched
  await deleteIfPresent(page, NAME2);
  check("Delete removes the snapshot row (cleanup)",
    (await rowFor(page, NAME2).count()) === 0);
});

summary("profiles");
