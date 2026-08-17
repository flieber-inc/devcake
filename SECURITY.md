# Security

## Product security contract

DevCake's **normative security posture** is
[`docs/14-security.md`](docs/14-security.md). Read that file before filing a
report or writing claims about isolation, multi-tenancy, or supply-chain
controls.

In short: DevCake is a **single-operator agent runtime for a dedicated host**.
Anyone who can write tickets on a configured PMO instance or land content in a
configured repository can influence coding agents that hold forge and model
credentials. Multi-tenant least-privilege SaaS is an **explicit non-goal**.
The security-relevant second identity on the forge is the **reviewer token**
(app-only) — not staffing a different Dev Type for the REVIEW pipeline stage.

Do not file issues that ask the project to “fix” accepted design choices
documented there (prompt injection as trusted input, capable agents with open
egress, Dagu with host `docker.sock`, basic auth on a dedicated host). Those
are the product contract (§0 / §10 of `docs/14-security.md`), not defects.

## What to report privately

Report **implementation defects** that break documented hard controls, for
example:

- A control marked **hard** or **gate** in `docs/14-security.md` that does not
  hold in a current release
- Failures of redaction at its documented choke points (`security.redact` /
  related egress paths)
- **Credential or secret material committed in the tree** (tokens, private
  keys, real `.env` contents)
- Paths that map to no accepted stance in `docs/14` and no residual-risk row —
  true **unknown unknowns** that expand blast radius beyond the contract

### Private channel

Prefer a **non-public** path so unpatched credential- or RCE-class issues are
not broadcast:

1. **GitHub Security Advisories (private vulnerability reporting)** on this
   repository, when the maintainers have enabled it for the public remote —
   use the repository’s **Security → Advisories / Report a vulnerability** UI.
2. If private reporting is not yet enabled on the remote you cloned, contact a
   **maintainer privately** (for example a direct message on a channel they
   publish, or a private GitHub discussion with a maintainer). **Do not** open
   a public issue that includes exploit details, live tokens, or steps that
   make unpatched hosts easy to hit.

There is **no** dedicated public security mailbox published in-tree. Do not
invent one; do not paste secrets into public issues, PRs, or the PMO feed.

When the defect is already public knowledge (for example a dependency CVE with
a published advisory and no secret material), an ordinary issue or PR is fine.

## Dependency inventory and SBOM (honest status)

What exists today:

| Check | Where | Scope |
|---|---|---|
| `pip-audit` on the app-test image environment | `.github/workflows/ci.yml` | Python packages installed in `devcake/app-test` |
| `npm audit --omit=dev` | `.github/workflows/ci.yml` | Admin SPA production npm dependencies |
| Bake `sbom: true` | `.github/workflows/docker-publish.yml` (manual publish to GHCR) | SBOM attached to **published** images only |

What does **not** exist:

- No committed tree-wide SBOM artifact
- No continuous CycloneDX/Syft (or equivalent) pipeline on every PR
- CI bake intentionally uses `sbom: false` (see `.github/workflows/ci.yml`)

Roadmap still lists a fuller SBOM under public-release hygiene
([`docs/16-roadmap.md`](docs/16-roadmap.md)). Do not treat the checks above as
a complete software bill of materials program.

## Operator secrets (not vulnerability reports)

Stack bootstrap passwords live in `.env` (gitignored; start from
`.env.example`). Operator PMO/forge/model secrets are entered through the
admin UI and stored under `/data` — never commit them. See
[`docs/14-security.md`](docs/14-security.md) §4 and
[`docs/13-deployment.md`](docs/13-deployment.md).

If you rotate a credential because it may have been exposed historically in
git history, rotation is an **operator** action outside this repository;
history rewrite is not performed by routine contributions.
