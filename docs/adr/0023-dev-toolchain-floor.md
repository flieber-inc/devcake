# ADR-0023 — The Dev container toolchain floor

- **Status:** accepted (2026-08-02)
- **Context:** In a founder stress test, Devs working on website UI changes
  (Claude Code + Fable, Grok Build + Grok 4.5 — clearly capable models)
  wanted to live-test in a browser and could not get one. The diagnosis is
  a cascade, not a single missing package: `apt` fails (no root — correct,
  and also a verified hard requirement: Claude Code refuses
  `--dangerously-skip-permissions` under root, `07-dev-runtime.md` §7);
  the capable-model workaround `npx playwright install` downloads a browser
  to `~/.cache` but the launch dies on ~40 missing system shared libraries
  that only build-time root can provide; and on the grok image the cascade
  never starts — it shipped with **no Node at all**, so a grok Dev on a JS
  repo could not even `npm run dev` the thing it was asked to test. A
  smaller trap in the same family: `pip install --user` console scripts
  landed in `~/.local/bin`, which was not on PATH in two of three images
  (`08-harness-templates.md` §7 used to warn about it instead of fixing it).
  Founder decisions (2026-08-02): browser in the shared base;
  `build-essential` in; document + spreadsheet processing in.

## Decision

### 1 — Bake capability floors, not tool inventories

The mission space is open-ended, so enumerating tools loses by
construction. The base image instead guarantees three capability classes,
chosen by *who is able to provide the capability*:

- **Class A — root-only, so bake it or it cannot exist:** system shared
  libraries (the browser stack), compilers/linkers (`build-essential` —
  without it any source-only native pip/npm install dies), core system
  utilities.
- **Class B — self-provisioning enablers:** Node/npm/npx in the SHARED
  base (harness-identical floor — mission outcomes must not depend on
  which Dev Type picked the work up), pip/venv, `uv` (root-free Python env
  management), and PATH that actually honors user installs
  (`~/.local/bin`, `~/.npm-global/bin` via a dev-owned `.npmrc` prefix —
  runtime `npm i -g` is now a user-space operation). With class B solid,
  the long tail self-serves: curl + tar already fetches any static binary.
- **Class C — high-frequency conveniences that save turns:** jq, ripgrep,
  unzip/zip, less, procps, file, make (via build-essential), sqlite3 —
  and, per founder decision, the document/spreadsheet floor: `pandoc`,
  `poppler-utils` (pdftotext/pdftoppm), `pandas` + `openpyxl` in the
  system Python. The argument is ADR-0022's turn economics applied to
  image design: every run that hand-bootstraps ripgrep or a CSV parser is
  burning the turns the continuation loop exists to save, and bootstrap
  detours are where narrate-and-stop accidents live.

### 2 — The browser: playwright's pinned headless shell in the shared base

`playwright@1.61.1` (pinned to the same family as the repo's own admin UI
suite — one browser stack for the whole codebase) with
`install --with-deps chromium-headless-shell` at build time, browsers in
the world-readable `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`, and the
`playwright` CLI on PATH at the same pin. Rationale over the
alternatives: deps-only (per-run 170 MB browser downloads — the turn tax
again) and Debian `chromium` (executablePath plumbing friction, no
savings) both lose. The shared-base placement means the ~0.6 GB stack
exists ONCE on disk across all three harness images. A Dev that installs
a mismatched playwright version falls back to downloading its own build —
degraded, never broken; the baked pin is discoverable via
`playwright --version`.

### 3 — Build-time proof, not hope

The base stage ends with a smoke RUN executed AS THE DEV USER: the
headless shell must LAUNCH (`--version` loads every shared library — the
exact failure the unbaked images hit), node/npx/playwright/uv answer,
`import pandas, openpyxl` resolves, and the class-C binaries exist. CI's
image bake therefore asserts the floor on every PR that touches images.

### 4 — Deliberately out, and the levers for the long tail

- **sudo/root** — the sandbox premise plus the hard Claude Code
  requirement. **docker CLI / DinD** — needs socket or privilege; the
  container boundary IS the sandbox. **Databases/services** — repos
  declare their own; sqlite covers the local case. **Cloud/vendor CLIs** —
  vendor segregation; per-Dev-Type territory. **Media tooling**
  (ffmpeg/imagemagick) — heavy and niche; playwright covers screenshots.
  **LibreOffice** — ~700 MB for conversion fidelity nobody has needed.
- Long tail, per Dev Type: `mcp_setup_commands` is functionally a setup
  hook (uid 1000, outbound network, secret-env expansion, 300 s cap) —
  one `pip install --user` / `npm i -g` line per tool, now actually
  invocable thanks to the PATH guarantee. At run time Devs self-serve the
  same way.

## Consequences

- Image weight: base grows to roughly 1.4 GB (browser stack ~0.6 GB,
  build-essential ~0.25 GB, Node ~0.15 GB, the rest small); harness
  images share the base layers, local bake only, no registry push. First
  bake is slow; layer cache absorbs the rest.
- grok Devs gain Node — the silent "cannot even start the dev server"
  failure class disappears.
- The `08` §7 PATH warning inverts into a guarantee; plugin registration
  can drop absolute-path workarounds.
- Debian bookworm packages float within the stable channel (same posture
  as gh/glab today); the pinned-by-ARG pieces are playwright and the
  Python libs. The unpinned-CLI caveat of `08` §1 is unchanged and
  separate.

## Related

- Implement: `images/Dockerfile` (base + all three harness stages).
- Evidence: the base-stage smoke RUN; CI "Bake Dev harnesses".
- Operator: `07-dev-runtime.md` §7a (the floor, normative),
  `08-harness-templates.md` §7 (plugin PATH note), `13-deployment.md` §6.
