"""Harness package — per-CLI container behavior (Zone B).

Module map (entrypoint façade imports these; do not re-fork spines):

* ``dialect`` / ``dialects`` — fail-closed ``HarnessDialect`` registry
  (docs/16 H1); unknown id raises, never Claude fall-through.
* ``launch`` — ``composed_launch`` (dialect argv ± operator script).
* ``aim`` — backend adaptor (docs/08 §8), not fault classification.
* ``argv`` / ``render`` / ``tokens`` — CLI argv, live log lines, TokenReport v1.
* ``continuation`` — ADR-0022 policy, nudges, session chains, token merge.
"""
