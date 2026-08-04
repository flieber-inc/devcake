// Missions suite: pipeline strip + grouped mission list (2026-08-02 board
// re-decision, DESIGN.md §2 — kanban columns retired: their geometry assumed
// even occupancy while the steady state is extreme skew, six empty columns
// around one overflowing Done). Gates: the strip carries all seven stages,
// sections exist only for non-empty stages (Needs human pinned first, Done
// last), rows stay single-line and dense, Done previews then unfolds, the
// page never scrolls horizontally at ANY width, and the sidebar collapse
// toggle works on Missions (the force-collapse exception died with the
// board). Read-only — everything is route-mocked or a plain GET.
import { check, checked, gotoFresh, summary, withPage } from "./harness.mjs";

const LONG_KEY = "PLATFORM-999999";
const LONG_TITLE =
  "A ticket title that is long enough to exercise the single-line truncate " +
  "so a dense row can never wrap into a second line and grow the list";
const DONE_REASON = "terminal — ignored";
const LONG_REPO = "acme-corp/very-long-repo-name-with-hyphens-that-could-wrap";

// Every stage populated + every needs-human variant + 35 done entries so
// bucketize exercises its 30-cap and the section its 10-row preview.
const COLUMN_SEEDS = [
  { col: "backlog",     status: "backlog",     labels: [] },
  { col: "plan",        status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-PLAN"] },
  { col: "execute",     status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-EXECUTE"] },
  { col: "review",      status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-REVIEW"] },
  { col: "merge",       status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-MERGE"] },
  { col: "needs_human_conflict", status: "in_progress", labels: ["DEVCAKE-PLAN", "DEVCAKE-EXECUTE"] },
  { col: "needs_human_skip",     status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-SKIP"] },
  { col: "needs_human_failed",   status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-FAILED"] },
  { col: "needs_human_wanted",   status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-NEEDS-HUMAN"] },
  { col: "done",        status: "done",        labels: ["DEVCAKE"] },
];

function makeRows() {
  const rows = [];
  let idx = 0;
  for (const seed of COLUMN_SEEDS) {
    const n = seed.col === "done" ? 35 : 3;
    for (let i = 0; i < n; i += 1) {
      idx += 1;
      rows.push({
        pmo_id: `ID-${idx}`,
        key: `${LONG_KEY}-${idx}`,
        title: `${LONG_TITLE} (${seed.col} #${i + 1})`,
        status: seed.status,
        labels: seed.labels,
        mission_type: "PLAN",
        priority: i % 2 ? "urgent" : "medium",
        repo: LONG_REPO,
        url: `https://linear.app/example/issue/${idx}`,
        updated_at: new Date(Date.now() - idx * 60_000).toISOString(),
        schedulable: seed.col === "backlog",
        // one deviant done row: the header hoists the MAJORITY reason and
        // only the odd row spells its own out inline
        reason: seed.col === "done" ? (i === 0 ? "terminal — done" : DONE_REASON) : "",
      });
    }
  }
  return rows;
}

function mockMissions(page, rows) {
  return page.route(/\/api\/v1\/missions(?:\?.*)?$/, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ missions: rows, adoption_mode: "auto", teams: {} }),
    }),
  );
}

// ── populated list: structure, density, preview, no horizontal scroll ──────
async function assertList(width, height) {
  await withPage(async (page) => {
    await mockMissions(page, makeRows());
    await gotoFresh(page, "#/missions");
    await page.waitForSelector('[data-testid="mission-list"] [role="button"]', { timeout: 8000 });

    // strip: all 7 stages present, populated ones as jump buttons
    const strip = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="pipeline-strip"]');
      return {
        chips: el ? el.querySelectorAll("span.rounded-full, button").length : 0,
        buttons: el ? el.querySelectorAll("button").length : 0,
        text: el ? el.textContent : "",
      };
    });
    check(`strip carries all 7 stages at ${width}`, strip.chips === 7, `got ${strip.chips}`);
    check(`strip: every populated stage is a jump button at ${width}`,
      strip.buttons === 7, `got ${strip.buttons} (all stages are seeded)`);
    check(`strip shows the capped Done count at ${width}`, /Done\s*30/.test(strip.text));

    // sections: only non-empty stages, Needs human first, Done last, no
    // "empty" placeholders anywhere
    const sections = await page.evaluate(() =>
      [...document.querySelectorAll('[data-testid="mission-list"] section')]
        .map((s) => s.getAttribute("aria-label")));
    check(`sections render only non-empty stages at ${width}`, sections.length === 7,
      `got ${sections.join(",")}`);
    check(`Needs human is pinned first at ${width}`, sections[0] === "Needs human");
    check(`Done is last at ${width}`, sections[sections.length - 1] === "Done");
    check(`no empty placeholders at ${width}`,
      (await page.locator('[data-testid="mission-list"] :text-is("empty")').count()) === 0);

    // density: rows are single-line (≤48px) and the viewport shows a crowd
    const { rowMax, visible, listOver, docOver } = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('[data-testid="mission-list"] [role="button"]')];
      const vh = window.innerHeight;
      return {
        rowMax: Math.max(...rows.map((r) => r.getBoundingClientRect().height)),
        visible: rows.filter((r) => {
          const b = r.getBoundingClientRect();
          return b.top >= 0 && b.bottom <= vh;
        }).length,
        listOver: (() => {
          const el = document.querySelector('[data-testid="mission-list"]');
          return Math.ceil(el.scrollWidth) - el.clientWidth;
        })(),
        docOver: Math.ceil(document.documentElement.scrollWidth) - document.documentElement.clientWidth,
      };
    });
    // 52px = py-2 (16) + the 32px ⋯ trigger + row border and subpixel slack;
    // a wrapped title would add a full ~20px line and blow well past this
    check(`rows stay single-line at ${width} (tallest ${Math.ceil(rowMax)}px ≤ 52)`, rowMax <= 52);
    if (height >= 900) {
      check(`≥10 rows fully visible at ${width}×${height} (got ${visible})`, visible >= 10);
    }
    check(`no horizontal scroll at ${width} (list ${listOver}px, doc ${docOver}px)`,
      listOver <= 1 && docOver <= 1);

    // Done preview: 10 rows + honest unfold, section header hoists the
    // shared reason so rows don't repeat it
    const doneRows = () =>
      page.locator('section[aria-label="Done"] [role="button"]').count();
    check(`Done previews ${10} of 30 at ${width}`, (await doneRows()) === 10);
    check(`Done header hoists the majority reason at ${width}`,
      (await page.locator(`section[aria-label="Done"] header:has-text("${DONE_REASON}")`).count()) === 1);
    check(`rows never repeat the hoisted reason at ${width}`,
      (await page.locator(`section[aria-label="Done"] [role="button"] :text-is("${DONE_REASON}")`).count()) === 0);
    if (width >= 1024) {  // the inline reason column is lg-and-up
      check(`the deviating row spells out its own reason at ${width}`,
        (await page.locator('section[aria-label="Done"] [role="button"] :text-is("terminal — done")').count()) === 1);
    }
    await page.click('section[aria-label="Done"] button:has-text("Show all 30")');
    check(`Show all unfolds the capped 30 at ${width}`, (await doneRows()) === 30);
    await page.click('section[aria-label="Done"] button:has-text("Show fewer")');
    check(`Show fewer folds back at ${width}`, (await doneRows()) === 10);

    // row click opens the drawer (drawer itself unchanged)
    await page.locator('section[aria-label="Done"] [role="button"]').first().click();
    await checked(`row click opens the mission drawer at ${width}`, async () => {
      await page.waitForSelector('button[aria-label="Close drawer"]',
        { timeout: 5000 });
      return (await page.locator(
        'button[aria-label="Close drawer"]').count()) === 1;
    });
    await page.click('button[aria-label="Close drawer"]');
  }, { width, height });
}

await assertList(1440, 900);
await assertList(1280, 900);
await assertList(390, 844);   // the list layout owes ANY width — no fit math

// ── empty board: message only, no strip ────────────────────────────────────
await withPage(async (page) => {
  await mockMissions(page, []);
  await gotoFresh(page, "#/missions");
  await page.waitForSelector(':text("No missions yet")', { timeout: 8000 });
  check("empty board shows the waiting message",
    (await page.locator(':text("No missions yet")').count()) >= 1);
  check("empty board renders no strip",
    (await page.locator('[data-testid="pipeline-strip"]').count()) === 0);
});

// ── the sidebar collapse toggle works on Missions at any width now ─────────
await withPage(async (page) => {
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('h1:has-text("Missions")');
  const state = await page.evaluate(() => {
    const btn = [...document.querySelectorAll("aside button")]
      .find((b) => /sidebar/i.test(b.getAttribute("aria-label") || ""));
    return { present: !!btn, disabled: btn?.disabled };
  });
  check("sidebar toggle enabled on Missions at 1280 (force-collapse deleted)",
    state.present && state.disabled === false, `disabled=${state.disabled}`);
}, { width: 1280, height: 900 });

// ── stage visibility (2026-08 reviewer round): Hide collapses a section to
// its strip pill (dimmed, count kept — never invisible-invisible); the pill
// shows it again; the choice persists in localStorage ────────────────────────
await withPage(async (page) => {
  await mockMissions(page, makeRows());
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('[data-testid="mission-list"] section');

  await page.click('button[aria-label="Hide the Backlog section"]');
  await page.waitForTimeout(100);
  check("Hide removes the stage section from the list",
    (await page.locator('section[aria-label="Backlog"]').count()) === 0);
  const pill = page.locator('button[aria-label*="hidden, click to show"]');
  check("hidden stage keeps a dimmed strip pill with its count",
    (await pill.count()) === 1 && /Backlog\s*\d+/.test(await pill.innerText()));

  // persists across a reload (localStorage, per-operator view state)
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('[data-testid="mission-list"] section');
  check("hidden stage stays hidden after a reload",
    (await page.locator('section[aria-label="Backlog"]').count()) === 0);

  await page.click('button[aria-label*="hidden, click to show"]');
  await page.waitForTimeout(100);
  check("clicking the dimmed pill shows the section again",
    (await page.locator('section[aria-label="Backlog"]').count()) === 1);
  // cleanup: leave no persisted preference behind for later suites
  await page.evaluate(() => localStorage.removeItem("devcake-missions-hidden"));
});

summary("missions");
