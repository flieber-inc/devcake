# DevCake Admin SPA — Design Guideline

**Mandatory for every change under `admin/spa/` that affects look, feel, copy, or
interaction.** These are decisions, not suggestions — established during the
2026-07 redesign (branch `feat/admin-ux-redesign`). Follow them; don't re-litigate
them per-PR. If a rule genuinely can't fit a new case, say so explicitly in the PR
instead of silently deviating.

Reference implementations to copy from: `src/pages/RunsPage.jsx` (tables, header
actions), the Config section components `src/components/*Section.jsx` (settings
sections, SettingRow, MoreMenu, InstantZone), `src/pages/OverviewPage.jsx`
(masthead, glyph rows).

---

## 1. Identity — "bakery-warm, terminal-precise"

The product is an oven that bakes software missions. The UI is warm (stone
surfaces, espresso ink, crust-orange accent) but operates with terminal
precision (mono ids, tabular numerals, dense honest tables).

### Color — tokens only, never raw hex in components

All palette decisions live in `src/index.css` `@theme`. Components use Tailwind
classes that resolve to those tokens. **Never introduce a raw hex value or a new
color family in a component.**

| Register | Classes | Meaning |
|---|---|---|
| **Accent** | `accent-*` (brand `#b4550a` at 600) | The ONLY brand accent. Primary buttons, active nav, links, the current StageGlyph layer, InstantZone tint. Don't dilute it with a second accent color. |
| **Neutrals** | `neutral-*` (warm, stone-tinted; 900 = espresso `#2a2018`) | All text/borders/surfaces. The warmth is token-side — keep using `neutral-*` classes. |
| **Surfaces** | `bg-surface` / `bg-surface-raised` (+ `-dark`) | Page ground / cards. Cards via the `Card` component, not hand-rolled divs. |
| **Warning** | `amber-*` (retuned yellower, 600 = `#a1720d`) | Warnings/partial states. Deliberately pushed away from the brand orange — never use `accent-*` for a warning or `amber-*` as decoration. |
| **Danger** | `red-*` (stock) | Destructive actions and errors only. |
| **Success** | `green-*` (stock) | Confirmations only. |

### Type

- **Display face** (`font-display`, Bricolage Grotesque Variable): wordmark, page
  titles, big numerals — **nowhere else**. Body/UI text stays on the system stack.
- Mono (`font-mono`) for machine identifiers: run ids, mission keys, env var
  names, InstantZone eyebrow labels.
- Numerals that line up in columns get `tabular-nums`.

### Signature element

`StageGlyph` (`src/components/StageGlyph.jsx`) — the layer cake. Four stacked
layers baked bottom-up for the ONBOARD→PLAN→EXECUTE→REVIEW pipeline; accent =
current stage. It's the one piece of flourish; don't invent additional decorative
glyphs, illustrations, or emoji. Icons are lucide-react SVGs only.

### Radius / shadow

`rounded-card` + `shadow-card` for cards (via `Card`), `rounded-md` for controls.
Nothing else.

---

## 2. Layout idioms

### Settings model (Cursor/Codex style)

Configuration renders **one section per view**, routed as `#/config/<section>`
(bare `#/config` redirects to the first section). `ConfigPage.jsx` is a thin
dispatcher: it owns the page header, the page-level error line (`pageErr`,
passed as `setPageErr` to sections that report async failures), the mobile
section chip row, and the scroll-to-top on section change — then switches on
the route to exactly ONE section component. Every section is a component in
`src/components/`: `PmoSection`, `DevTypesSection`, `SkillsSection`,
`AssignmentsSection`, `PromptsSection`, `ProfilesSection`, `LimitsSection`,
`TrafficSection`. Most sections pull the shared draft themselves via
`useSharedDraft()` (ConfigDraftContext); `PromptsSection` is the exception —
it takes `cfg`/`setField`/`devTypeNames` as props from the dispatcher. Either
wiring is acceptable, but new sections use `useSharedDraft()`. A section
owns its section-local state and dialogs (its own `ConfirmDialog`, wizards,
etc. — closed dialogs render null, so this stays invisible in the DOM);
anything cross-page (the draft itself, Save/DirtyBar/NavGuard in `DraftChrome`)
stays at App/context level. **Caveat (audit D5 #12):** section-local state
resets on a section *switch* (the section component unmounts) — state that must
survive switching within Config (e.g. session name-tracking spanning cards)
belongs at the dispatcher or context level, not inside a section. Shared
sub-components used by more than one section get their own file (e.g.
`RepoChips.jsx`); a new config section means a new `<Name>Section.jsx`, not
inline JSX in ConfigPage. Sidebar sub-nav active state is route-driven —
there is no scrollspy; don't reintroduce one. On section change, `<main>`
scrolls to top.

### SettingRow — the default for scalar settings

A scalar setting (number, select, toggle, short text) is a
`SettingRow` (`src/components/SettingRow.jsx`): label + one-line description on
the left, control on the right, stacked in a `divide-y` container. Long
explanations go in its `help` popover, not inline. Pass `htmlFor` **only** when
the child is a real labelable input — a bare `<label>` around non-inputs forwards
clicks to the first labelable descendant (this was a shipped bug; see §6).

Do not build a new one-off field layout when SettingRow fits. Complex entities
(PMO instances, Dev Types, repos) stay as cards; grids of per-type values
(Assignments) stay as tables.

### Tables

Lists of records are real `<table>`s styled like the Runs table:
`thead` = `text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400`,
rows = `border-t border-neutral-100 dark:border-neutral-800` with
`hover:bg-stone-50 dark:hover:bg-neutral-900`, cells `align-top` when rows wrap,
ids in `font-mono text-xs`. Wrap in `overflow-x-auto` with a sensible `min-w`.
Never fake a table with flex rows that misalign.

---

## 3. Action hierarchy — one primary, everything else behind ⋯

The rule that cleaned up Skills, Dev Type cards, and the Runs header:

1. **Each header/card gets at most ONE visually primary action** — the thing a
   user actually comes there to do (`Add skill`, `Connect via OAuth…`,
   `Open Dagu`).
2. **Secondary, rare, and destructive actions go into a `MoreMenu`**
   (`src/components/MoreMenu.jsx`): items carry `label` + a one-line `desc`
   stating the real consequence, `external: true` renders ↗, `danger: true`
   renders the label red. Destructive menu items still confirm via
   `ConfirmDialog` — the menu is de-emphasis, not the safety mechanism.
3. **Never create a one-item menu when that item is the element's only action**
   — a lone `Remove` or `Clear data` button stays a visible ghost button.
   Hiding the only action is worse than showing it.
4. Destructive actions are never solid-red primaries sitting beside the real
   primary. `danger`/`danger-ghost` Button kinds exist for confirm dialogs and
   in-flow destructive UI, not for header real estate.

`Button` kinds are fixed: `primary` (accent), `ghost`, `danger`, `danger-ghost`.
Don't add kinds; don't hand-roll button classes (the Runs header's `Open Dagu`
anchor mirrors `primary` because it's an `<a>` — copy that pattern for links).

---

## 4. Progressive disclosure — quiet by default

- Inline helper text: **one short line max** under a label/section title.
- Anything longer lives in the click-popover `Help` (`src/components/Field.jsx`)
  — a "?" that toggles on click with Esc/outside close. No hover-only tooltips
  for essential info.
- Long optional lists (template managers, advanced blocks) collapse behind a
  `<details>` disclosure with a count: `Manage templates (3)…`.
- Section-level background/context goes on the `Section` `help` prop
  (`src/components/Card.jsx`), not a paragraph in the card body.

---

## 5. Dialogs and the two persistence regimes

### Dialogs

Native `window.confirm` / `window.prompt` / `window.alert` are banned. Use
`src/components/Modal.jsx`:

- `ConfirmDialog` — destructive/irreversible confirmations. Body copy states
  exactly what is destroyed AND what survives. No backdrop/Esc dismiss by design.
- `PromptDialog` — single-value input flows (e.g. rename).
- `Modal` / `Overlay` — everything else; Overlay provides focus trap,
  `role="dialog" aria-modal`, `<main>` scroll-lock.

### Draft vs instant

The config surface has exactly two persistence regimes — keep them visually and
semantically distinct:

- **Drafted** (default): edits ride the unified draft (`useConfigDraft`,
  `DraftChrome` DirtyBar/SaveReviewDialog/NavGuardDialog at App level). New
  config fields must join this flow and get labels in `configLabels.js`.
  **Never change draft semantics** — no per-section save buttons, no autosave.
- **Instant**: anything that hits the server the moment it's used gets wrapped
  in `InstantZone` (crust-tinted region with the ⚡ "applies immediately"
  eyebrow) or carries `ImmediateBadge`. No third regime.

---

## 6. Correctness gotchas (each of these was a real bug)

- **Label forwarding**: a `<label>` wrapping non-input content forwards clicks
  to the first labelable descendant — clicking "Adoption mode" once opened the
  adopt-team dialog. Only render `<label>` with an explicit `htmlFor`;
  otherwise use `<span>`. `SettingRow` and `Field` already encode this.
- **Dark mode is manual** (`.dark` class via `theme.js`): every color utility
  needs its `dark:` twin. Verify with computed styles, not by eyeballing a PNG —
  screenshot viewers lie about color profiles.
- **Vite build passes with undefined identifiers** (rollup treats them as
  globals). A green `npm run build` does NOT prove the page renders — load it.
- **Focus**: every interactive element keeps a visible
  `focus-visible:ring-2 focus-visible:ring-accent-500/60` state. Never remove
  focus rings.
- **A11y minimums**: icon-only buttons get `aria-label`; menus get
  `aria-haspopup`/`aria-expanded` + `role="menu"`/`menuitem`; glyphs get
  `role="img"` + `aria-label`; decorative icons get `aria-hidden`.

---

## 7. Copy voice

- Sentence case everywhere; active verbs; the control names its outcome
  (`Clear run history`, not `Clear` — then the toast says what was cleared).
- Destructive copy is **verbatim-honest with the backend**: before writing a
  consequence line, read the endpoint it calls. "Never overwrites your edits"
  shipped only because `/skills/sync` provably doesn't.
- State what survives, not just what dies ("Config and credentials preserved.").
- No exclamation marks, no apology copy, no jargon leaked from internals
  (users manage "built-in skills", not "seed sync").

---

## 8. Definition of done (evidence, per Always Works™)

A UI change is done when you have personally seen:

1. `npm --prefix admin/spa run build` green **from the repo root** (the prefix
   resolves against cwd).
2. The changed screens loaded live — dev loop: vite on :5199 (export
   `ADMIN_USER`/`ADMIN_PASSWORD` from `.env` first) driven by playwright-core +
   the cached Chromium headless shell. Screenshot light + dark + 390 px mobile
   and actually look at them. Note: the app scrolls inside `<main>` (fullPage
   screenshots won't capture it) and hash-only `page.goto` does **not** remount
   the app — reload for fresh-mount behavior.
3. `npm --prefix admin/spa run check:ui` green — the committed behavioral
   suite (`admin/spa/tests/`: settings model, action hierarchy, redesign
   invariants; it boots vite itself and needs the live backend on :8080).
   It never confirms a Save and cancels every destructive dialog, so it is
   safe against live config. New interactions get assertions added here
   (menu opens, dialog reached, draft counts edits), not just screenshots.
   Local-only for now — CI has no live stack to run it against.
4. For prod: `docker buildx bake admin && docker compose up -d admin` + load.

Name anything left unproven (OAuth wizard, real save-PUT, etc.) instead of
claiming it works.
