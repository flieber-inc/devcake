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
