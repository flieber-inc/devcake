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
`PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`, and the `playwright` CLI on
PATH at the same pin. Rationale over the alternatives: deps-only (per-run
170 MB browser downloads — the turn tax again) and Debian `chromium`
(executablePath plumbing friction, no savings) both lose. The shared-base
placement means the ~0.6 GB stack exists ONCE on disk across all three
harness images.

**`/opt/pw-browsers` is dev-owned — corrected by the fix round.** The env
var points EVERY playwright install at that path, so as first shipped
(root-owned, `a+rX`) any browser the base did not bake — another
playwright version's revision, full Chromium, Firefox, WebKit — was
uninstallable: measured `EACCES` on `__dirlock`, and only *after* the
download completed. Ownership by `dev` restores self-healing (a
mismatched pin downloads its own build beside the baked one — degraded,
never broken); it is safe because the container is disposable, so a run
that corrupts the shared stack corrupts only itself. The build smoke
asserts writability. The baked pin stays discoverable via
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

- Image weight: harness images land at ~1.9–2.1 GB, base layers shared,
  local bake only, no registry push — DISK, not RAM (founder note
  2026-08-02); stale per-SHA tags are the only growth, so periodic
  `docker image prune` matters more than before. First bake is slow;
  layer cache absorbs the rest.
- Memory is a CAPABILITY cost, not a standing one: the browser consumes
  RAM only when a run launches it (headless shell idle ≈150 MB, active
  page loads 300–800 MB). The `14` §11 no-hard-container-limits debt is
  unchanged; its reachable ceiling grew — budget
  `concurrency × browser working set` for browser-using fleets.
- `tini` is PID 1 (fix round): the entrypoint reaps nothing, and browser
  process trees orphan children on unclean exits — without a reaper,
  multi-hour runs (longer under ADR-0022 continuations) accumulate
  zombies and stray headless-shells. Dagu cannot pass `--init`.
- PATH precedence (`~/.npm-global/bin` before `/usr/local/bin`) lets a
  run shadow binaries the entrypoint itself later invokes (`grok export`,
  continuation relaunches). No privilege or capability is gained — a Dev
  already authors its own result.json, and `14` treats Dev output as
  untrusted; the vector pre-existed on grok (`~/.grok/bin` is dev-owned).
  Accepted; absolute-path resolution in the entrypoint is the hardening
  candidate if transcript-evidence integrity ever tightens.
- A live browser is a wider prompt-injection intake than curl: rendered
  third-party pages enter the model's context wholesale. Same class as
  the existing open-egress accepts (`14` §5a); named on the `14` §11
  egress-allowlist radar.
- Screenshots of CJK/emoji-heavy pages render tofu (`--with-deps` ships
  Latin fonts only); `fonts-noto-cjk` (~60 MB) is the add if a fleet
  needs it.
- grok Devs gain Node — the silent "cannot even start the dev server"
  failure class disappears.
- The `08` §7 PATH warning inverts into a guarantee; plugin registration
  can drop absolute-path workarounds.
- Debian bookworm packages float within the stable channel (same posture
  as gh/glab today); the pinned-by-ARG pieces are playwright and the
  Python libs. The unpinned-CLI caveat of `08` §1 is unchanged and
  separate.

## Addendum — the container engine joins the floor (2026-08-13, founder go)

The original exclusion — "docker CLI / DinD — needs socket or privilege;
the container boundary IS the sandbox" — is AMENDED, not reversed: Devs
gain **rootless podman inside their own container** (`docker` = podman
compat symlink), and the sandbox premise SURVIVES IN KIND — no
`docker.sock`, no privilege, nested containers live and die inside the Dev
container's own namespaces — at the stated cost in point 4 below: 15
additional syscalls + 2 device nodes for EVERY container the dev-run DAG
launches (hello included), recorded as the docs/14 §6 risk row. docs/14 §5
is unchanged and proud: the socket still never reaches a Dev.

Measured recipe (feasibility matrix + live probes, 2026-08-13):

1. **Engine:** pinned podman-static bundle (5.8.4 — Debian bookworm's 4.3
   fails rootless-in-container; apt cannot serve this floor), sha256-pinned
   per arch, shipping crun/conmon/netavark/pasta/fuse-overlayfs.
   Provenance, recorded: the bundle is `mgoltzsche/podman-static` — an
   UNOFFICIAL upstream, accepted because the artifacts are sha256-pinned
   per arch and no distro ships a rootless-in-container-capable 5.x;
   revisit when one does.
2. **Helpers:** Debian's SETUID `newuidmap`/`newgidmap` EPERM on the
   uid_map write inside a container on the measured host while the
   Fedora-style FILE-CAPS flavor works — the base re-packages them with
   `cap_setuid/cap_setgid=ep`, setuid dropped (mechanism unexplained,
   behavior pinned by the live nested-run probe).
3. **Runtime knobs (dev-run DAG `host:` block):** a CUSTOM seccomp profile
   — Docker's default plus ONE 15-syscall allow rule (userns/mount set +
   sethostname/setdomainname) — inline in the DAG (the Docker API takes
   profile content; only the docker CLI reads files), **never unconfined**
   (structural test enforces); `/dev/fuse` (fuse-overlayfs fallback for
   kernels <5.13; ≥5.13 uses native rootless overlay) and `/dev/net/tun`
   (pasta tap) via the nested Resources block.
4. **Costs, recorded:** the seccomp delta (15 syscalls over default) plus
   /dev/fuse + /dev/net/tun, applied to EVERY container the dev-run DAG
   launches, hello included (docs/14 §6); nested images live under the
   harness $HOME → per-run ephemeral (re-pulled each run — egress, no
   cross-run contamination) — with ONE exception: nested writes onto the
   /workspace BIND outlive the run as foreign-uid files, which is why the
   DAG's exit handler re-chowns the workspace to uid 1000 at run end
   (audit 2026-08-13 B1; deletion stays in workspaces.py); the
   Dev-container cgroup limits bound the nested engine too (pids budget
   must accommodate it).
5. **Host prerequisites** (docs/13): unprivileged user namespaces enabled;
   kernel ≥5.13 recommended (native rootless overlay).

## Related

- Implement: `images/Dockerfile` (base + all three harness stages).
- Evidence: the base-stage smoke RUN; CI "Bake Dev harnesses"; the
  nested-matrix probes (addendum above); `test_repo_structural.py`
  nested-engine pins.
- Operator: `07-dev-runtime.md` §7a (the floor, normative),
  `08-harness-templates.md` §7 (plugin PATH note), `13-deployment.md` §6.
