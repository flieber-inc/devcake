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
surfaces, espresso ink, frosting-violet accent) but operates with terminal
precision (mono ids, tabular numerals, dense honest tables).

**Dark is the temperature exception (CAKE-158):** light stays bakery-warm;
dark re-grounds in a cool, faintly violet-tinted near-black so the cool
violet brand reads electric instead of muddy. That cool ramp is remapped
under `html.dark` in `src/index.css` — do **not** cool the shared `@theme`
light neutrals/surfaces. Dark is not a second brand accent; it is the same
violet on a cooler ground.

### Color — tokens only, never raw hex in components

All palette decisions live in `src/index.css` `@theme` (plus `html.dark`
remaps for the cool-dark exception). Components use Tailwind classes that
resolve to those tokens. **Never introduce a raw hex value or a new color
family in a component.** Hex contracts are pinned by
`tests/design_tokens.mjs` (off-palette class ban **and** light/dark hex pins).

| Register | Classes | Meaning |
|---|---|---|
| **Accent** | `accent-*` (brand `#6042cc` at 600; 400/500/600 are the brand mark's frosting-gradient stops) | The ONLY brand accent. Primary buttons, active nav, links, the current StageGlyph layer, InstantZone tint. Don't dilute it with a second accent color. Dark lifts mid/deep stops under `html.dark` so chips/text stay electric; 400/500/600 stay identity. |
| **Neutrals** | `neutral-*` (warm stone on light — 900 = espresso `#2a2018`; cool violet-grey under `html.dark`) | All text/borders/surfaces. Keep using `neutral-*` classes; temperature is token-side. |
| **Surfaces** | `bg-surface` / `bg-surface-raised` (+ `-dark`) | Page ground / cards. Light = warm stone; dark = cool near-black `#0f0e14` / raised `#17161f`. Cards via the `Card` component, not hand-rolled divs. |
| **Warning** | `amber-*` (true amber, 600 = `#a1720d`; dark-tuned under `html.dark`) | Warnings/partial states — a register of its own: never use `accent-*` for a warning or `amber-*` as decoration. |
| **Danger** | `red-*` (stock) | Destructive actions and errors only. |
| **Success** | `green-*` (stock) | Status dots and small success affordances only — not large chrome next to the brand mark. Confirmations/toasts elsewhere stay green. |

### Type

- **Display face** (`font-display`, Bricolage Grotesque Variable): page
  titles, big numerals — **nowhere else**. Body/UI text stays on the system stack.
  (The wordmark is no longer set in type: the brand mark and wordmark are SVG
  artwork — canonical files in `docs/img/brand/`, bundled copies in
  `src/assets/` — rendered per theme: black-violet on light, **white-violet on
  dark** (no cream on dark ground). The UI's `accent-*` ramp is anchored on the
  artwork's violet gradient; cream cake layers remain light/marketing
  artwork-only — don't promote them into UI classes. UI accents stay `accent-*`.)
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

Nav regroup (CAKE-159): **Connections** (credentials + Test connection),
**Fleet** (who does the work), **Settings** (how the system behaves). Fleet
and Settings each render **one section per view**, routed as
`#/fleet/<section>` and `#/settings/<section>` (bare `#/fleet` /
`#/settings` land on the first section; legacy `#/config/*` redirects).
`FleetPage.jsx` / `SettingsPage.jsx` are thin dispatchers: each owns the
page header (with a **per-section save-regime subtitle** — Profiles is
immediate; Limits/Scheduled Tasks are draft; Dev Types/Prompts state their
mixed regimes honestly), the page-level error line where needed, the mobile
section chip row, and scroll-to-top on section change — then switches on the
route to exactly ONE section view.

Fleet sections (from `nav.js`): `dev-types`, `mission-types`, `prompts`,
`skills` (catalog only). Settings sections: `limits`, `scheduled-tasks`,
`profiles`. Connections pages: `#/repos`, `#/pmo`, `#/skill-sources`
(mobile chips via `ConnectionTabs`). Every section is a component in
`src/components/`: `DevTypesSection`, `SkillsSection`, `SkillSourcesSection`,
`AssignmentsSection`, `PromptsSection`, `ProfilesSection`, `LimitsSection`,
`ScheduledTasksSection` (DevCake tasks — Relations Steward + Memory Curator —
over Custom tasks). Most sections pull the shared draft via
`useSharedDraft()` (ConfigDraftContext); `PromptsSection` is the exception —
it takes `cfg`/`setField`/`devTypeNames` as props from the dispatcher. Either
wiring is acceptable, but new sections use `useSharedDraft()`. A section
owns its section-local state and dialogs (its own `ConfirmDialog`, wizards,
etc. — closed dialogs render null, so this stays invisible in the DOM);
anything cross-page (the draft itself, Save/DirtyBar/NavGuard in `DraftChrome`)
stays at App/context level. **Caveat (audit D5 #12):** section-local state
resets on a section *switch* (the section component unmounts) — state that must
survive switching within Fleet/Settings (e.g. session name-tracking spanning
cards) belongs at the dispatcher or context level, not inside a section.
Shared sub-components used by more than one section get their own file (e.g.
`RepoChips.jsx`); a new section means a new `<Name>Section.jsx`, not inline
JSX in the dispatcher. Sidebar sub-nav active state is route-driven — there
is no scrollspy; don't reintroduce one. On section change, `<main>` scrolls
to top.

### SettingRow — the default for scalar settings

A scalar setting (number, select, toggle, short text) is a
`SettingRow` (`src/components/SettingRow.jsx`): label + one-line description on
the left, control adjacent on the right (one-line row on `sm+` — do **not**
`justify-between` across the full pane; that left an eye-tracking gulf under
the page's `max-w-6xl` shell), stacked in a `divide-y` container. Long
explanations go in its `help` popover, not inline. Pass `htmlFor` **only** when
the child is a real labelable input — a bare `<label>` around non-inputs forwards
clicks to the first labelable descendant (this was a shipped bug; see §6).

Do not build a new one-off field layout when SettingRow fits. Complex entities
(PMO instances, repos) stay as cards; grids of per-type values (Assignments)
stay as tables.

### Roster table (Dev Types)

Dev Types render as a **roster table** (re-decided 2026-08-02, founder-directed
— the 2026-07-27 tile grid didn't survive bulk scale: a 12-Dev-Type stress
test made oversized cards the problem; a table is as elegant at 3 rows as at
20). The tile grid's invariants carry over unchanged: each row leads with the
small monogram avatar (no emoji/illustrations) wearing the
credential-readiness dot (green = ready, amber = none — readiness is NEVER
hidden), name in mono, then harness / model / max-concurrency
(`tabular-nums`) / skills-count columns and a **status** cell that is always
informative: credential readiness in words (green "ready" / amber
"no credentials" — the avatar dot's meaning, spelled out) plus an amber
"· unsaved changes" badge appended while the row has draft edits (founder
follow-up 2026-08-02: the earlier reserved-but-invisible badge slot looked
like an empty, broken column).
A dashed full-width "New Dev Type" row closes the table and is the ONLY
create affordance — no twin button in the section header (the header keeps
the immediacy badge and the ⋯; Clear secrets stays in the ⋯ even as its lone
item, because §3.4 bars destructive header buttons). The whole row opens the
**editor modal**; Rename/Delete stay behind the row's ⋯. The modal edits the
shared draft (footer states "applied by the page-level Save" — no per-modal
save button, ever); OAuth / uploads / secret values stay in their
`InstantZone`s inside it. The editor's first view is harness, model and
skills only (Buzz-style); credentials, concurrency and per-type plumbing
(MCP setup, secret env) live behind a single `Advanced…` disclosure (labeled
just "Advanced"). Credential readiness is never hidden by the fold — the
header subtitle carries it (green ready / amber missing). Roster rows batch
their readiness probes by harness template (one fetch per template, not per
row). Use this idiom only for identity-like rosters; settings stay
SettingRows.

### Mission list (Missions page)

Missions render as a **pipeline strip + grouped list** (re-decided 2026-08-02,
founder-directed — the kanban board didn't survive contact with the product's
own success: working stages drain in minutes-to-hours *by design* while Done
accumulates, so the steady state was six columns rendering "empty" (~86% of
the board's width) around one overflowing Done whose ~160px-tall cards showed
4 of 30 missions with titles clipped to ~4 words). The strip — a sticky row
of seven stage chips with live counts, populated ones jump buttons, Needs
human amber when non-zero — is the ONLY place empty stages appear; it carries
the whole "shape of the pipeline" signal the columns used to spend the full
viewport on. Sections exist per non-empty stage (Needs human pinned first —
it answers "what needs me" — Done last) and each mission is ONE dense row:
glyph slot (reserved even when empty so keys align), mono key, full-width
truncating title, needs-human badge, deviating reason, repo, priority DOT
(tooltip — labelled chips repeat "medium" fleet-wide and carry no scan
value), age, ↗, ⋯. A reason all rows of a section share is hoisted into its
header. Done counts are true totals (strip chip and section badge); the
section previews its 10 newest and "Show all N" unfolds the full Done set
(`N` = true total — never a second silent ceiling). Invariants carried
over from the board: row click opens the drawer, context actions stay behind
the ⋯, the PMO is the source of truth. The list owes rendering at ANY
viewport width with no horizontal scroll — retiring the board also deleted
the sidebar's Missions force-collapse exception, and nothing may reintroduce
per-page sidebar behavior.

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
- Long optional lists and advanced blocks collapse behind a `<details>`
  disclosure with a count when they stay on the page. Template create/edit
  lives in the page-level **Manage templates** modal (CAKE-166) — not a
  per-row `<details>` list.
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
  in `InstantZone` (accent-tinted region with the ⚡ "applies immediately"
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
- **Optimistic overrides key on the (instance, pmo_id) ref, never the bare
  id** (`lib/reqSeq.js`'s sibling concern): gitea_issues pmo_ids are per-repo
  issue numbers, so #3 collides across boards — a bare-id key painted a Park
  on one board onto the colliding id on another (2026-08-12 audit). Every
  action/comment also sends `instance` so the server resolves the right board
  (`mission_actions._find_row`). And an override self-heals after
  `PROJECTION_MAX_POLLS` even if the server advanced to a state the projection
  never predicted — an exact-label-match test alone would stick forever.
- **Stale responses are dropped, not committed** (`lib/reqSeq.js`): loaders
  that re-fire on changing deps (RunsPage's eight filters) or from many call
  sites with no in-flight guard (ConfigDraftContext.reload) tag each request
  with a monotonic token; a resolution that is not the latest is dropped. A
  config rebase resolving out of order otherwise rolled the server baseline
  backward, and since config does not poll the phantom dirt persisted.

### Accepted single-operator scope (ledgered, NOT built — founder ruling
2026-08-12)

The control plane is single-operator by construction and stays that way for
now: `PUT /config` is last-write-wins with an edits-win rebase, and there is
no `If-Match`/version stamp on config or secret writes. With two admins, a
second operator's change is overwritten with no conflict surface. This is a
real liability the day there are two admins and the deliberate fix is
end-to-end config versioning (backend version stamp → SPA `If-Match` → 409 +
conflict UI) — out of scope for the audit-remediation campaign, which
addressed only the single-operator correctness bugs above. Do not add
optimistic multi-writer behavior without that versioning.

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
   It cancels every destructive dialog, and every suite except one never
   confirms a Save — the honest exception is `pmo_intake.mjs`, which
   REALLY saves (a poll-interval tweak plus intake flips, restoring both
   to as-found) to prove the save-revert regression; run it against
   stacks whose config you own. New interactions get
   real predicates added here (`checked(name, fn)` — never `check(name,
   true)` after a bare wait), not just screenshots; a predicate over
   text the page decorates after a later fetch (a stored-token suffix,
   a presence-driven disabled state) uses `checkedEventually(name, fn)`,
   which re-reads until it holds, so a slow runner cannot turn a
   one-shot read into a flake. CI runs the full
   suite (`npm run check:ui`) against the compose-backed admin on
   `:8080` after hello dispatch + forge/PMO contract batteries (founder
   REVIEW_LEDGER item 9 — CI-minutes cost accepted). Local operators
   still use the vite default (`:5199`) or
   `UI_BASE=http://127.0.0.1:8080` against a stack whose config they
   own. The pure-node helper suites (markdown/format) also ride the
   early hermetic `test:helpers` CI step and need nothing live.
4. For prod: `docker buildx bake admin && docker compose up -d admin` + load.

Name anything left unproven (OAuth wizard, real save-PUT, etc.) instead of
claiming it works.
