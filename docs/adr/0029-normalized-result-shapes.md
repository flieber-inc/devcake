# ADR-0029 — Normalized result shapes (TokenReport v1 + SQL-readiness doctrine)

- **Status:** accepted (2026-08-04)
- **Context:** Founder intent (2026-08-04): standardize the data shapes in
  the system NOW so that a potential future in which **all** results are
  captured in a SQL database is a mechanical adapter swap — while
  explicitly building **no** database in this campaign. Most persisted
  shapes were already SQL-ready: the artifacts envelope is versioned
  (docs/09 §2), `result.json` has per-mission-type legal-outcome schemas
  (docs/03 §6), Run records are `schema_version: 2` with a stable typed
  field set behind `StatePort` — and ADR-0002 already designates "swap
  `state/runs/` to SQLite behind StatePort" as the designed exit.
  ADR-0027 made `error_class` an enumerable dimension. The one genuinely
  non-standard shape was `token_report`: per-harness folklore with
  CONDITIONAL key presence (claude full split + native cost; grok end-event
  split with no cost; grok signals totals-only; codex split with no total),
  provenance encoded by which keys existed, reasoning tokens smuggled
  through a regex-parsed `notes` string, and the signals path lying about
  its own name (`session_json`). Consumers compensated with per-harness
  folklore (evaluation F22).

## Decision

### 1 — TokenReport v1, normalized at the extraction seam

One CLOSED shape from every extractor
(`devcake_dev/harness/tokens.token_report_v1`): every key ALWAYS present
(`None` = unknown, never absent) —

```
schema (1), model, input_tokens, output_tokens, cache_read_tokens,
cache_write_tokens, total_tokens, reasoning_tokens, num_turns, duration_ms,
cost_usd_native, cost_usd_estimated, source, raw
```

- `source` ∈ {`session_json`, `end_event`, `signals`, `cumulative`,
  `mixed`, `unavailable`} — provenance is DATA, not key-presence.
  `cumulative` marks a codex resume chain (the harness's counters are
  cumulative; last-wins); `mixed` a multi-chain merge with disagreeing
  inputs; `signals` now names its actual path.
- `reasoning_tokens` is a first-class scalar (the `notes` regex dies).
- `cost_usd_native` names its provenance; `cost_usd_estimated` ships
  `None` from the image and is filled ONLY by the app-side ADR-0021 stamp
  (plus `rate_card_id`) — the harness layer still never computes a dollar.
- `raw` carries the vendor usage payload untouched (fidelity); merges
  carry `{"invocations": [...]}`. It is the only nested field.
- `total_tokens` stays REPORTED-only: deriving totals from splits is a
  display concern the app labels honestly (`total_tokens_effective`),
  never a measurement the image invents.
- Normalization happens IMAGE-SIDE at the token-extraction seam — the
  exact seam a future `HarnessDialect.parse_run` formalizes (a step
  TOWARD H1, not a conflict). The codex/claude extraction moved out of
  the entrypoint's inline arms into `tokens.py` functions accordingly.

### 2 — App consumers simplify

`costing.effective_cost` callers read `cost_usd_native`/`cost_usd_estimated`
uniformly; the feed renderer reads `reasoning_tokens` directly and prints
`source` on its extraction line; OTel gains `devcake.tokens.reasoning`
while `devcake.cost.usd` keeps its NAME (docs/12 contract — it still means
"billed as reported by the harness"). The Runs API row key stays
`cost_usd` (SPA contract), mapped from the stored v1 key.

### 3 — Skew and legacy records: no shims

App and images deploy lockstep (docs/13); the capture suite pins the new
shape. Legacy Run records on disk keep their old dicts — read paths use
`.get(...)` with a `None` default, so a pre-v1 record renders "—" where v1
keys are absent (native cost on old claude runs, reasoning on old grok
runs). Display-only, wipeable-by-doctrine state (docs/10 §5); no
migration, no dual-shape tolerance window. INV-5 measuring-path rule
unchanged: when a token report is posted, unavailable stays explicit
(`source: "unavailable"`), never silence (see `00-overview.md` for INV-5's
named feed-post exceptions).

### 4 — SQL-readiness doctrine (the sentence, made executable)

**Every persisted record: versioned schema, enumerated classes, no
conditional field presence.** `test_token_report_shape.py` enforces it
where cheap: every harness capture fixture must yield EXACTLY the closed
key set with scalar-typed values; the Run model's fields are pinned as
would-be DDL (scalar columns vs an explicit blob allowlist), so widening
either is a deliberate, reviewed diff. The future database remains: a
`StatePort` adapter + a projection of these frozen shapes. Nothing else
lands now.

## Consequences

- Adding a harness means writing one extractor that fills the closed
  shape — consumers need zero new branches.
- The shape test + the Run-model DDL pin are the contract a future
  SQLite/Postgres adapter codes against.
- Pre-v1 on-disk records display degraded (documented above) — accepted
  under the pre-v1 no-compat-shims rule.
- Image changes require rebake + lockstep deploy (docs/13, R9 ritual).
