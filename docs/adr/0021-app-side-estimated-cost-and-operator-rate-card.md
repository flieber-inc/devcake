# ADR-0021 — App-side estimated cost and the operator rate card

- **Status:** accepted (2026-08-02); **partial identifier supersession** —
  `finalize_mapper()` renamed to `finalize_steward` (MAPPER→STEWARD; same
  costing stamp behavior) — note in Decision 2
- **Context:** Grok runs extract full token splits but `cost_usd` is always
  null (grok-build 0.2.112 emits no cost field; `08-harness-templates.md`
  §5), so fleet spend was understated everywhere native cost is summed —
  the review-loop warning, the OO cost panels, any operator export. The
  2026-08-01 feedback asks for a clearly-labeled **estimate** derived from
  published rates, without breaking the "native cost is never guessed"
  invariant that keeps `devcake.cost.usd` claude-only-and-honest. Founder
  decisions (2026-08-02): harness-reported data stays authoritative by
  default; a per-model rate card with built-in defaults; an operator
  checkbox may flip *displayed* cost to the rate-card computation.

## Decision

### 1 — Estimation lives app-side, never in the harness

`images/` stays estimate-free (guarded by `test_entrypoint_tokens.py`).
A pure module `domain/costing.py` prices an already-extracted token_report:

- `estimate_cost_usd(report, rates)` → `float | None`. **None** unless
  `input_tokens`, `cache_read_tokens`, `output_tokens` are all present AND
  a rate row matches the model (longest `model_prefix` wins). Totals-only
  fallback shapes (`signals.json`) and unmapped models are never priced.
  Null `cache_write_tokens` prices as 0 (grok has no write counter) but
  stays null in the report — display renders "—", never `$0`.
- Reasoning tokens are a **subset** of `output_tokens` — never added on top.

### 2 — Option A persistence: separate, labeled, revocable

`finalize()` and `finalize_mapper()` stamp `cost_usd_estimated` +
`rate_card_id` into `run.token_report` (via `costing.stamp_estimate`)
before OTel/feed/persist read the dict. *(Runtime: the mapper finalize
path is `finalize_steward` — MAPPER→STEWARD rename; costing stamp
behavior unchanged.)* `cost_usd` is never written by estimation, so every
existing native-cost aggregation stays pure. The stamp lands even when
native cost exists — the override display mode (§4) needs both numbers.

### 3 — The rate card is operator config

`AppConfig.cost_inputs: {rates: [ModelRate], override_native: bool}`
(`ModelRate` = `model_prefix` + four per-1M USD rates, `ge=0`, unique
prefixes). Defaults ship the xAI grok-4.5 standard list rates
($2.00 / $0.30 / $6.00 per 1M input / cache-read / output). The derived
`rate_card_id` — `builtin-v1` when the card equals the defaults (bump the
suffix when defaults change), else `operator:<sha256[:8]>` — names the
vintage on every stamped estimate. Additive config: no schema bump,
carried automatically by ADR-0013 bundles/profiles.

The long-context (≥200k prompt) ×2 ceiling is deliberately **not** in v1:
session aggregates cannot tell which turns crossed the threshold, so the
standard rate is the honest single number; the ceiling can join later as a
second labeled metric if wanted.

### 4 — Vintage semantics: finalize-time stamp vs read-time display

The feed line, OTel attribute, and persisted stamp use the rates current
**at finalize** (the `rate_card_id` names them). Read-side surfaces (the
Runs API/tab, PR chain part 3) recompute from the *current* card so a
Cost-Inputs edit takes effect immediately; the two may differ after a rate
edit, and that divergence is intended — the stamp is the historical
record, the display is today's best estimate. `effective_cost(native,
estimate, cost_inputs)` defines what displays show: native-first with
estimate filling gaps, flipped when `override_native` — which only bites
for models a rate row maps (overriding Claude's native cost requires
adding a `claude-` row).

### 5 — Known assumption

Grok's `usage.input_tokens` is treated as non-cached input (the
`total = input + cache_read + output` identity holds on live FLI2 runs).
If xAI's accounting shifts, the estimate shifts with it — everything is
labeled "estimated" and operator-tunable, never invoiced truth: server-side
tool fees, priority/batch tiers, and storage are all out of scope.

## Consequences

- Grok spend becomes visible (labeled) in the review-loop cumulative
  warning, the feed token report, OTel (`devcake.cost.usd_estimated`), and
  the Runs tab — while `devcake.cost.usd` keeps meaning "billed as
  reported by the harness".
- Docs that said "never estimated" now scope that claim to the harness
  layer and native `cost_usd` (`02` §10, `03` §8, `08` §5, `12` §3-§4).
- Rollout: part 1 = estimator + config + persistence (this ADR), part 2 =
  feed/OTel/OO surfaces, part 3 = Runs API + admin UI (Cost Inputs modal).
