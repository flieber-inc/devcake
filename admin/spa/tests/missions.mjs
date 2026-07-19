// Missions suite: the board must fit the main pane at the notebook floor
// (≥1440) and at the harness default (1280) without triggering horizontal
// scroll — both on an empty board AND with a stress payload that exercises
// every column with long mono keys, long titles, and every label variant a
// MissionCard renders. The empty-board pass is trivially satisfied by empty
// dashed placeholders; the populated pass is what real operators see.
// Read-only — everything is route-mocked or a plain GET.
import { check, gotoFresh, summary, withPage } from "./harness.mjs";

// Worst-case rows: each column gets 5 cards with fields that push MissionCard
// widest — a long key (font-mono, no truncate in the header row), a long
// title, priority pill, repo string, url link, reason text. `done` gets 35
// entries so `bucketize` exercises its 30-cap slice too.
const LONG_KEY = "PLATFORM-999999";
const LONG_TITLE =
  "A ticket title that exercises the two-line line-clamp so the card grows " +
  "tall enough to reveal any horizontal bleed from the meta row below";
const LONG_REASON =
  "A blocking condition that spans two lines when clamped so the reason row " +
  "is exercised alongside the label chip and updated timestamp";
const LONG_REPO = "acme-corp/very-long-repo-name-with-hyphens-that-could-wrap";

const COLUMN_SEEDS = [
  { col: "backlog",     status: "backlog",    labels: [] },
  { col: "plan",        status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-PLAN"] },
  { col: "execute",     status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-EXECUTE"] },
  { col: "review",      status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-REVIEW"] },
  { col: "merge",       status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-MERGE"] },
  { col: "needs_human_conflict", status: "in_progress", labels: ["DEVCAKE-PLAN", "DEVCAKE-EXECUTE"] },
  { col: "needs_human_skip",     status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-SKIP"] },
  { col: "needs_human_failed",   status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-FAILED"] },
  { col: "needs_human_wanted",   status: "in_progress", labels: ["DEVCAKE", "DEVCAKE-NEEDS-HUMAN"] },
  { col: "done",        status: "done",       labels: ["DEVCAKE"] },
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
        mission_type: "main-dev",
        priority: "URGENT",
        repo: LONG_REPO,
        url: `https://linear.app/example/issue/${idx}`,
        updated_at: new Date(Date.now() - idx * 60_000).toISOString(),
        schedulable: seed.col === "backlog",
        reason: seed.col === "backlog" ? LONG_REASON : "",
      });
    }
  }
  return rows;
}

async function assertBoardFits(width, height, { populated } = {}) {
  const rows = populated ? makeRows() : [];
  const label = populated ? "populated" : "empty";
  await withPage(async (page) => {
    if (populated) {
      // Surgical mock: exact /api/v1/missions only (leave /health, actions,
      // etc. alone). Matches both first fetch and the 10s poll.
      await page.route(/\/api\/v1\/missions(?:\?.*)?$/, (route) =>
        route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ missions: rows, adoption_mode: "auto", teams: {} }),
        }),
      );
    }
    await gotoFresh(page, "#/missions");
    await page.waitForSelector('[data-testid="board-scroller"] section', { timeout: 8000 });
    if (populated) {
      // Wait for at least one real card (MissionCard uses role="button" with
      // a mono key inside). Bare-stack empty renders "empty" placeholders,
      // no role=button — this proves the mock landed.
      await page.waitForSelector('[data-testid="board-scroller"] [role="button"]', { timeout: 8000 });
    }
    const { boardScroll, boardClient, worstCard } = await page.evaluate(() => {
      const el = document.querySelector('[data-testid="board-scroller"]');
      const cards = [...el.querySelectorAll('[role="button"]')];
      let worst = { overflow: 0, key: null };
      for (const c of cards) {
        const over = Math.ceil(c.scrollWidth) - c.clientWidth;
        if (over > worst.overflow) {
          const keyEl = c.querySelector(".font-mono");
          worst = { overflow: over, key: keyEl ? keyEl.textContent : "?" };
        }
      }
      return { boardScroll: el.scrollWidth, boardClient: el.clientWidth, worstCard: worst };
    });
    const boardOver = Math.ceil(boardScroll) - boardClient;
    check(
      `${label} board fits at ${width}×${height} (scrollWidth ${boardScroll} ≤ clientWidth ${boardClient})`,
      boardOver <= 1,
      boardOver > 1 ? `overflows by ${boardOver}px` : "",
    );
    if (populated) {
      check(
        `${label} board — no card overflows its column at ${width}×${height}`,
        worstCard.overflow <= 1,
        worstCard.overflow > 1
          ? `worst card (${worstCard.key}) overflows by ${worstCard.overflow}px`
          : "",
      );
    }
  }, { width, height });
}

// Notebook floor per plan/joyful-herding-knuth: no horizontal scroll at ≥1440.
await assertBoardFits(1440, 900);
await assertBoardFits(1280, 900);
// Populated payload: what an operator actually sees.
await assertBoardFits(1440, 900, { populated: true });
await assertBoardFits(1280, 900, { populated: true });

// The Missions force-collapse under 1440 makes the sidebar toggle a no-op —
// disable it honestly instead of shipping a control that lies. At 1440 the
// force is off and the toggle is enabled.
await withPage(async (page) => {
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('[data-testid="board-scroller"]');
  const state = await page.evaluate(() => {
    const btn = [...document.querySelectorAll("aside button")]
      .find((b) => /sidebar|Missions below 1440/i.test(b.getAttribute("aria-label") || ""));
    return { present: !!btn, disabled: btn?.disabled, aria: btn?.getAttribute("aria-label") };
  });
  check("sidebar toggle disabled at 1280 on Missions", state.present && state.disabled === true,
    `aria=${state.aria} disabled=${state.disabled}`);
}, { width: 1280, height: 900 });

await withPage(async (page) => {
  await gotoFresh(page, "#/missions");
  await page.waitForSelector('[data-testid="board-scroller"]');
  const state = await page.evaluate(() => {
    const btn = [...document.querySelectorAll("aside button")]
      .find((b) => /sidebar|Missions below 1440/i.test(b.getAttribute("aria-label") || ""));
    return { present: !!btn, disabled: btn?.disabled };
  });
  check("sidebar toggle enabled at 1440 on Missions", state.present && state.disabled === false,
    `disabled=${state.disabled}`);
}, { width: 1440, height: 900 });

summary("missions");
