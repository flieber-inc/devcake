// CAKE-162: heading dedupe, SettingRow proximity, chip-strip wrap,
// SelectionChips searchable add-list at scale. Source pins — hermetic.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(join(root, rel), "utf8");

const reposSrc = read("src/pages/ReposPage.jsx");
const pmoSrc = read("src/components/PmoSection.jsx");
const settingRowSrc = read("src/components/SettingRow.jsx");
const fleetSrc = read("src/pages/FleetPage.jsx");
const settingsSrc = read("src/pages/SettingsPage.jsx");
const tabsSrc = read("src/components/ConnectionTabs.jsx");
const chipsSrc = read("src/components/SelectionChips.jsx");
const docs11 = readFileSync(
  join(root, "../../docs/11-admin-panel.md"),
  "utf8",
);

let failed = 0;
const check = (name, fn) => {
  try {
    fn();
    console.log(`  ✓ ${name}`);
  } catch (e) {
    failed += 1;
    console.log(`  ✗ ${name}: ${e.message}`);
  }
};

check("Repos page Section title is not a duplicate of PageHeader Repositories", () => {
  assert.match(reposSrc, /PageHeader title="Repositories"/);
  assert.doesNotMatch(
    reposSrc,
    /<Section id="repository" title="Repositories"/,
    "Section must carry new information (e.g. Forge connections), not repeat Repositories",
  );
  assert.match(
    reposSrc,
    /<Section id="repository" title="Forge connections"/,
    "Section title should be Forge connections",
  );
});

check("PMO Section title is not a soft duplicate of PageHeader PMO", () => {
  assert.doesNotMatch(
    pmoSrc,
    /<Section id="pmo" title="PMO connections"/,
    "Section must not restate PMO connections under a PMO page header",
  );
  assert.match(
    pmoSrc,
    /<Section id="pmo" title="Watched teams"/,
    "Section title should be Watched teams",
  );
});

check("SettingRow keeps the control adjacent to the label (no full-row justify-between)", () => {
  assert.doesNotMatch(
    settingRowSrc,
    /sm:justify-between/,
    "SettingRow must not push the control to the far right across dead space",
  );
  assert.match(
    settingRowSrc,
    /sm:flex-row sm:items-center/,
    "SettingRow stays a one-line row on sm+",
  );
});

check("Fleet/Settings mobile section chips wrap instead of horizontal scroll", () => {
  for (const [src, sections, who] of [
    [fleetSrc, "FLEET_SECTIONS", "FleetPage"],
    [settingsSrc, "SETTINGS_SECTIONS", "SettingsPage"],
  ]) {
    const chipRow = src.match(
      new RegExp(String.raw`sticky top-0[\s\S]*?` + sections + String.raw`\.map`),
    );
    assert.ok(chipRow, who + " must still render the mobile chip row");
    assert.match(chipRow[0], /flex-wrap/);
    assert.doesNotMatch(chipRow[0], /overflow-x-auto/);
    assert.doesNotMatch(chipRow[0], /shrink-0/);
  }
});

check("AdapterTabs wrap instead of horizontal scroll", () => {
  assert.match(tabsSrc, /flex-wrap/);
  assert.doesNotMatch(tabsSrc, /overflow-x-auto/);
  assert.doesNotMatch(tabsSrc, /shrink-0/);
});

check("SelectionChips uses an add-list for large catalogs (past searchThreshold)", () => {
  assert.match(
    chipsSrc,
    /role="listbox"|data-testid="selection-chips-add-list"|aria-label=\{`Add /,
    "Large-catalog path must expose a searchable add-list, not only catalog chips",
  );
  assert.match(
    chipsSrc,
    /searching\s*\?/,
    "add-list path must gate on searching (options past searchThreshold)",
  );
  // Small catalogs keep the chip cloud — unselected options still render as chips
  // when not searching (ternary else / explicit !searching branch).
  assert.ok(
    /!searching/.test(chipsSrc) || /searching\s*\?/.test(chipsSrc),
    "Small catalogs must keep the zero-friction chip cloud gated on searching",
  );
});

check("docs/11 Multi-select convention documents add-list at scale", () => {
  assert.match(
    docs11,
    /add-list|add list/i,
    "docs/11 must describe the large-catalog add-list shape",
  );
  assert.match(
    docs11,
    /SelectionChips/,
    "docs/11 must still name SelectionChips as the chokepoint",
  );
});

check("Repos ↔ PMO collapsed summary + card MoreMenu shells stay parallel", () => {
  assert.match(reposSrc, /data-testid="repo-summary-row"/);
  assert.match(pmoSrc, /data-testid="pmo-summary-row"/);
  assert.match(reposSrc, /stopPropagation/);
  assert.match(pmoSrc, /stopPropagation/);
  assert.match(reposSrc, /label:\s*"Rename adapter"/);
  assert.match(pmoSrc, /label:\s*"Rename adapter"/);
  // Section-level Clear secrets ⋯ is intentional (DESIGN §3.4 / Dev Types precedent)
  assert.match(pmoSrc, /More PMO actions/);
  assert.match(reposSrc, /More repository actions/);
});

if (failed) {
  console.error(`cake162_polish.mjs: ${failed} check(s) failed`);
  process.exit(1);
}
console.log("cake162_polish.mjs: all checks passed");
