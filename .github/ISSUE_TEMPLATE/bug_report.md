---
name: Bug report
about: Something broken relative to documented behavior
title: ""
labels: []
assignees: []
---

## Summary

<!-- One or two sentences. -->

## Steps to reproduce

1.
2.
3.

## Expected

## Actual

## Environment

<!-- OS, Docker / Compose / Bake versions as relevant. -->

-

## Security and product contract

- Credential- or RCE-class defects, or failures of controls marked **hard** / **gate** in [`docs/14-security.md`](../../docs/14-security.md): follow [`SECURITY.md`](../../SECURITY.md) private channel. **Do not** open a public issue with exploit detail, live tokens, or steps that make unpatched hosts easy to hit.
- Do not ask the project to “fix” accepted design choices in `docs/14-security.md` (prompt trust, dedicated-host posture, reviewer-token second identity, etc.). Those are the product contract, not defects.

## Further reading

[`CONTRIBUTING.md`](../../CONTRIBUTING.md) · [`docs/14-security.md`](../../docs/14-security.md)
