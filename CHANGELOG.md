# Changelog

DevCake is pre-v1. Notable public changes, milestone exit criteria, and the
living engineering log live in **[`docs/16-roadmap.md`](docs/16-roadmap.md)** —
that file is the source of truth so history is not maintained twice.

This root `CHANGELOG.md` exists so FOSS and GitHub conventions have a stable
landing page. When maintainers cut numbered releases, release notes can be
added here without copying the full roadmap.

## Unreleased (pre-v1)

See the living log and open candidates in
[`docs/16-roadmap.md`](docs/16-roadmap.md).

Community surface added for public-repo hygiene (no LICENSE change in this
track): [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md).

## v0.4.3 (2026-08-25)

Patch release in the v0.4 "Hummingbird" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.4.3).

- **Fixed — fresh installs on macOS / Docker Desktop** (#313–#319).
  `DOCKER_GID` is derived from the in-container view of the Docker socket;
  the host baker no longer sweeps a keep-set published mid-reconcile,
  verifies its own liveness after launch, and gains `--foreground-baker`;
  `./up.sh --bake` proves dispatch with a hello smoke before reporting
  success; OpenObserve's password policy is validated up front; baker
  launch failures ship diagnostics to OpenObserve and the admin alerts.
  Docker Desktop guidance lives in [`docs/13-deployment.md`](docs/13-deployment.md) §8b.
- **Security — GitHub Security tab at zero** (#320–#329). The CodeQL
  path-injection class is closed by a shared path-confinement helper
  applied across the operator-facing stores and dispatch, with dispatch
  reading credential files through the secrets-store port; CodeQL runs as
  advanced setup from a SHA-pinned in-repo workflow with a model pack for
  the redaction chokepoint; the six remaining alerts are documented false
  positives with a proof table in [`docs/14-security.md`](docs/14-security.md) §12,
  dismissed citing the packet. Dependabot's two alerts closed via the
  transitive `postcss` bump, and automated security fixes are enabled.

## v0.4.2 (2026-08-20)

Patch release in the v0.4 "Hummingbird" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.4.2).

- **Fixed — the baker starts on hosts without pydantic** (#310). The host
  baker runs on the operator's bare system python, and its import chain
  reached pydantic through the harness registry, so fresh macOS and minimal
  Debian installs crashed it at startup. The dependency is cut and a
  regression test now blocks any third-party import from re-entering the
  baker's host-side closure. Affected hosts need no cleanup: pull and re-run
  `./up.sh`.
- **New — nested-engine rig receipts** (#311).
  `scripts/harness_probe/nested_probe.sh` replays the dev-run pipeline's
  exact rootless-podman runtime contract against a locally baked harness
  image and writes a per-rig receipt naming each step's verdict; see
  [`docs/13-deployment.md`](docs/13-deployment.md).

## v0.4.1 (2026-08-20)

Quickstart instructions comment cleanup only — no functional change. Note
this tag predates the v0.4.2 baker fix.

## v0.4 — Hummingbird (2026-08-19)

DevCake's first public release. Full notes on the
[GitHub release](https://github.com/flieber-inc/devcake/releases/tag/v0.4);
history and receipts in [`docs/16-roadmap.md`](docs/16-roadmap.md).
