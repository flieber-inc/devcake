# ADR-0033 — Discovery routing: the counterflow lane

- **Status:** proposed (2026-08-11) — founder + assistant sparring session;
  fills the seam ADR-0031 shipped empty (`ELEVATED_MARKERS`) and ADR-0032's
  consequences deferred ("that is discovery routing's lane").
- **Extends:** ADR-0007 (STEWARD gains a second duty, same propose-only
  contract), ADR-0031 (`ELEVATED_MARKERS` gains its first member), ADR-0032
  (the sibling lane — handoff flows *with* the graph at close; discovery
  flows *across* it mid-work), the result contract (one new optional key;
  entrypoint unchanged — unknown keys pass through), docs/03 playbooks.

## Context — the counterflow model

The thesis (docs/19 §2) says every seam a piece of work crosses must
conserve it, and that mismatches do not fail loudly — they *reflect*,
surfacing later as retries, rejections, and rework. A **discovery is the
reflected wave itself**: the plan encoded assumptions, a run collided with
reality, and the contradiction travels backward against the planned flow.
Today that reflected energy dissipates — as rework, as a confused REVIEW,
or entirely, when the container exits. This ADR makes the system conduct
its reflections somewhere useful instead.

The founder's constraint gives the feature its boundary: discoveries fix an
unmatched impedance **without changing the system's components**. Missions
and edges stay; only the *flow* — what enters future context — is
conditioned. Flow conditioning is in scope; topology surgery is not.

Three parties touch a discovery, and each holds context the others never
will. The design principle, load-bearing throughout: **each party bears
exactly the cost that only it can pay cheaply.** The discoverer
de-contextualizes (it alone holds the working context). STEWARD selects
(it alone holds the map). The recipient verifies (it alone holds the
future context where the finding will be used). Any party absorbing
another's role degrades the design — a STEWARD that rewrites content, a
recipient that trusts blindly, a discoverer that writes in session shorthand.

Governing doctrine (shared with ADR-0031/0032): *the PMO carries every
artifact that shapes a run's context; the app contributes only
deterministic, documented selection rules over PMO content; per-run
provenance lives on the Run record.* Discovery routing complies: the
non-deterministic judgment is a **Dev's proposal, memorialized on the
board**; the app's own contribution stays deterministic — validate,
cap, redact, apply — mirroring the existing edge-proposal flow.

## Decision 1 — authored by any Dev, in `result.json`, written for a stranger

The result contract gains an optional `discoveries` list (capped length).
Each entry is structured, not free text:

- `finding` — the fact, stated so a reader with zero session context can
  evaluate it: no coined terminology, no unanchored "the parser bug".
- `evidence` — the receipt for the fact: file paths, the error text, the
  reproducing command, the commit sha at discovery time. A discovery
  without evidence is an opinion; the schema demands the receipt.
- `scope` — what the discoverer believes it applies to, and what it does not.

Authorship follows result.json authorship: **ONBOARD, EXECUTE, and REVIEW**
may contribute discoveries (steward runs are excluded — Decision 7).
**PLAN cannot** — plan mode is read-only and its `result.json` is
entrypoint-synthesized (docs/03: the same reason PLAN cannot emit
`human_needed`), and extracting discoveries from plan prose would violate
the never-parse-prose rule (docs/03 §0; ADR-0032 chose structured fields
over marker-scraping for the same reason). The coverage hole is small
under the counterflow model — reflections happen where work collides with
reality, and PLAN reads what ONBOARD surveyed and EXECUTE is about to
touch — and it has a doctrine-clean relay: the PLAN playbook directs
genuinely off-mission findings into a marked *"Findings beyond this
mission"* section of `PLAN.md` (already a posted, human-visible
deliverable), and the EXECUTE playbook instructs carrying qualifying ones
forward into its own `discoveries` with its own evidence check. The relay
is Dev-to-Dev through an artifact they already share; the app parses
nothing.

The playbook instruction is one sentence and the field is **optional and
exceptional**: handoffs summarize the work; discoveries are surplus
learning. Requiring one per run would manufacture noise. Missing/empty
degrades to pre-ADR behavior, never fails a run. The Dev notices mid-run
but delivers at run end with the rest of the result — INV-4 holds and no
mid-run channel exists.

## Decision 2 — harvest at finalize: `DISCOVERY_<seq>.md` on the source feed

At finalize the app renders the entries into `DISCOVERY_<seq>.md` (seq =
the Mission Step, matching transcript naming) and posts it to the **source
mission's feed** with a `` `devcake:discovery:v1` `` marker line, alongside
the step's other deliverables. This memorializes the finding where humans
watch, gives the activity mirror the record for free, and — because the
board is the single source of truth — makes the discovery itself
recoverable state (Decision 3, Decision 8).

## Decision 3 — trigger: event-driven, single-flight per family

Harvest enqueues; a STEWARD discovery run launches as soon as the app
holds a discovery — with **at most one discovery run in flight per family
graph**. Arrivals during a run queue; the next run drains the whole queue.
A quiet board gets near-instant routing; a busy board gets natural
batching at one chokepoint, which is what prevents parallel finalizes from
re-creating the fan-in problem as concurrent STEWARDs. This is the
**discovery flavor only** — the relations cadence (ADR-0007's interval
service) is unchanged. Recovery is
board-derived, not queue-trusted: a `devcake:discovery:v1` post on a
source feed with no corresponding routed receipts in the family is
pending work the sweep re-detects. The in-memory queue is advisory,
per doctrine.

## Decision 4 — the STEWARD discovery run: curated context, verbatim transport

The context package is curated, not accumulated (the system's own doctrine
applies to its own machinery):

- the **family graph map**: the decomposition tree + `blocked_by`
  connected component, every node with status — siblings and cousins
  included, not just the source's branch;
- for **finished/canceled** missions: description + handoff note
  (ADR-0032's conserved summary is built for exactly this) + outcome line,
  labeled prior context;
- for **backlog and in-flight** missions: descriptions;
- the **full new `DISCOVERY_<seq>.md`** entries and their source mission's
  description — the one place full fidelity earns its tokens;
- the **family's work repositories, read-only** (founder ruling): the
  union of repos the family's missions route to — not the instance's
  whole configured set — mount-capped like blocker mounts (8, ADR-0017)
  and presented as consult-as-needed reference, never dumped into the
  prompt. Purpose: anchoring relevance — evidence anchors are paths and
  shas, and a steward that can grep the tree judges scope against ground
  truth. Precedent: `dispatch_steward` already refuses to run without a
  repository. One selection carve-out: the steward **may decline to route
  a finding whose cited evidence it cannot locate**, recording that in
  its run summary — anchor-existence is mechanical, and an unlocatable
  anchor is more likely hallucination than load-bearing fact. This is
  selection, not truth adjudication; verbatim transport stands.

Not the family's full feeds: that is accumulated context, the
anti-pattern. If evaluation shows the model starved, widening is a
deliberate follow-up, not a default. RO posture is by construction: a
STEWARD is a Dev — no PMO client, no forge write; it acts only through
its proposal.

The prompt's core is the **laminarity test**: *route a finding only where
it reduces uncertainty for that mission's stated work; volume that does
not reduce uncertainty is contamination.*

**Verbatim transport is a hard rule.** STEWARD lacks the context to
rewrite safely, and a paraphrase adds a second lossy hop — the telephone
game is how a true finding becomes a misleading one. It selects recipients
and may add one "routed because…" line in its own, visibly separate voice.
The finding text travels unedited; redaction and defang are the only
transformations (app-side, Decision 5). The cost asymmetry is deliberate:
a routing mistake costs the recipient a shrug; a content mutation can
silently poison a run.

## Decision 5 — propose-only output; the app validates and applies

The run returns proposals: `{target mission, findings routed, routed-because
line}`. App-side finalize validates deterministically — targets inside the
family, budgets not exhausted (Decision 7), `redact()` + backtick defang
(a discovery must never smuggle a live marker — the ADR-0032 §Decision 3
treatment verbatim) — and applies. STEWARD never writes the PMO (INV-4);
`finalize_steward`'s edge flow is the exact precedent.

## Decision 6 — delivery: marked feed comment; block-rendered; boundary-only

One comment per recipient per run, marked
`` `devcake:discovery-in:v1` `` with per-finding provenance (source key ·
step seq · date · commit anchor). Storage is the **feed**, not the
description: an unstarted mission's description is operator-owned spec
text (ADR-0032 appends only to *finished* missions); a comment is
deletable — the human override stays one click; and the marker joins
`ELEVATED_MARKERS`, so a recipient already at REVIEW's context-closing
finalize counts the routed finding as material and re-reviews (ADR-0031's
seam, doing what it was built for).

Presentation is the founder's block: the MISSION.md renderer collects
`discovery-in` comments and renders them as a dedicated closing block —
*"Related missions reported the following discoveries (leads, not truths —
verify against each source before relying): 1. [KEY · step n · date · at
`sha`] …"* — rather than leaving them inline in feed chronology. The
advisory register is part of the contract: routed text is model-authored
material that crossed a mission boundary; it is marked as such and never
laundered into spec-register text (quoting-quarantine, extended).

Recipients: **the whole family graph, including in-flight Missions, from
the feature's first release** (founder ruling — this ADR ships in a
pre-v1 product release; nothing here marks or gates product v1). Running
**Dev containers are never interrupted** — not a version choice but flow
doctrine: intervention lands at step boundaries, never inside one. A discovery waits on the feed like a
colleague's comment and is seen at the next boundary; the mechanism
inherits the guarantee rather than needing an exception. Discoveries do
not cross between unconnected families: a board-global finding is the
operator's to promote by hand; automatic broadcast is how tips become
ambient noise.

## Decision 7 — budgets and termination (the counterflow must terminate)

An unterminated line rings. Three dampers, all mandatory:

1. **No chain reactions.** A routed discovery never triggers discovery
   evaluation on its recipient, and steward runs cannot author
   discoveries. One reflection, one routing, done.
2. **Caps, marker-counted from the board.** Per-source-mission routing
   budget and per-recipient accumulation cap, both counted from live feed
   markers (the FRESHNESS/CONFLICT counting precedent) — PMO-derivable,
   restart-proof, wipe-proof. No local ledger. A human deleting a comment
   deliberately resets that slot: humans own the feed. Initial constants
   (evaluation-tuned, not operator knobs — knobs are debt too):
   `DISCOVERIES_PER_RUN_MAX = 3`, `DISCOVERY_ROUTES_PER_SOURCE_MAX = 3`,
   `DISCOVERY_IN_PER_RECIPIENT_MAX = 5`.
3. **Advisory framing** (Decision 6) is the amplitude damper: a wrong
   finding propagates as a lead to verify, not a fact to build on.

Dedup is provenance-based: a `(source key, seq)` already present in a
recipient's feed markers is never re-delivered — re-runs and repeat
STEWARD passes are idempotent against the board.

## Decision 8 — two ledgers, one direction

The PMO is the **knowledge ledger**: discovery content, evidence, routing
receipts — all of it, only there. OpenObserve is the **operations
ledger**: the machine's exhaust. Discovery events emit count-level span
attributes (family, stage, counts — never content) so dashboards can
chart reflection coefficients per seam — which decompositions leak, which
specs underdetermine their work (the docs/19 §4 instrument, materialized).
The invariant: **wipe OpenObserve and the mission loop must not change
behavior in any way.** Nothing in the production loop reads OO; the
ground truth for every metric is derivable from the board alone. The only
sanctioned Dev↔logs contact remains the opt-in vendor-segregated
logs-MCP plugin; anything more is its own ADR.

## Decision 9 — scope guard: flow, never topology

Discovery routing conditions context; it never touches the plumbing. When
a finding implies the plan itself is wrong — a mission mooted, a
decomposition mis-cut — that is topology surgery and escalates to the
human: STEWARD may *flag* it in its routed-because line, but its strongest
permitted act remains what ADR-0007 already grants — proposing an edge.
Cancel and rescope stay above the membrane (docs/19 §6). A discovery
orders information crossing a flow boundary; it never originates intent.

## Decision 10 — staffing: the steward class is EXECUTE-grade (founder ruling)

The steward duty class carries a normative capability bar: **at least the
level the EXECUTE role demands** — for both duties. Discovery routing is
family-wide relevance judgment; edge proposal always was critical (a wrong
`blocked_by` edge silently reorders a family's execution). The seeded
steward Dev Type re-pins from `claude-haiku-4-5` to **Claude Opus** on the
`claude-code` harness (the base-harness stance); operators staff
differently via the existing steward Dev Type selection and ADR-0019
overrides (e.g. Grok 4.5 on Grok Build — capable and competitively fast).
The seed affects fresh boots only: existing deployments keep their
configured staffing and upgrade via the admin UI. Docs/00 glossary and
docs/08 seeded-table rows update with the implementation.

## Decision 11 — per-PMO routing toggle; harvest is unconditional (founder ruling)

Each PMO instance gains a **`discovery_routing` toggle** gating the
steward discovery runs and cross-mission delivery for that instance's
families (families never span instances — DevCake creates no
cross-instance edges, so the toggle's unit matches the routing unit).
Default **on**: caps and family scoping bound the blast radius, and a
default-off feature never generates evaluation data. The **harvest half
is unconditional** — like HANDOFF, `DISCOVERY_<seq>.md` on the source
feed is pure memorialization: receipts cost nothing, stand alone as the
knowledge-base record, and keep the board complete for a later toggle-on.
Precedent: per-PMO intake toggles; ADR-0017's zip opt-in. This is
operator sovereignty over cross-mission feed noise, not a tuning knob —
the Decision 7 constants remain constants.

## Consequences

- Cross-subtree missions — which ADR-0032 left with nothing, by design —
  now receive mid-work findings at the cost of one bounded STEWARD run
  per discovery burst, with all content on the board and all budgets
  derivable from it.
- Pre-ADR runs and operator prompt overrides that omit `discoveries`
  degrade silently to today's behavior.
- PLAN-stage findings route only via the PLAN.md → EXECUTE relay
  (Decision 1) — one deliberate lossy hop, accepted because the
  alternative is prose-parsing. If evaluation shows PLAN discoveries
  systematically lost, the escalation is a structured side-channel for
  plan mode's synthesized result — its own change, not this ADR's.
- The discovery's quality ceiling is the discoverer's write-time
  discipline; the structured contract (finding/evidence/scope) is the
  enforcement surface, and thin or context-bound findings are a playbook
  bug to surface in evaluation, not a routing bug.
- Latency under load is cadenced by single-flight batching — accepted:
  batching calm is preferred over instant fan-out racing.
- New marker surface: two marker classes, both defanged on any append
  path, both counted (never trusted) per marker doctrine.

## Phasing and graduation

- **PR-1 (harvest):** result key + finalize rendering + source-feed post +
  queue + board-derived pending detection. Red→green at the finalize seam.
- **PR-2 (routing):** STEWARD discovery flavor (dispatch/prompt/finalize
  + family work-repo mounts), `ELEVATED_MARKERS` join, MISSION.md block,
  budgets, the per-PMO `discovery_routing` toggle, and the steward seed
  re-pin to Opus (config seed + docs/00/08 rows). Red→green at the
  steward-finalize and renderer seams.
- **Graduation:** a live multi-mission family smoke — one mission's
  discovery reaching a sibling's MISSION.md and an in-flight recipient's
  freshness re-review — before the feature may join field-evidence
  claims (docs/16 living-log criteria, 2026-08-11 entry).
- The freshness/handoff evaluation window on the refreshed host tunes the
  constants with real incident data.

## Related

- Implement: `domain/orchestrator/markers.py` (two marker classes,
  `ELEVATED_MARKERS` membership, counting helpers), finalize paths
  (harvest chokepoint), `steward.py` / `steward_service.py` (discovery
  flavor beside the relations cadence), `activity_payload.py` (MISSION.md
  block), `prompts/` (contract + playbook line + laminarity test),
  `config.py` (per-PMO `discovery_routing` toggle + steward seed re-pin),
  entrypoint unchanged.
- Doctrine: INV-1/INV-4, ADR-0007 (propose-only), ADR-0012 (edge
  permanence), ADR-0014 (mirror discipline), ADR-0019 (assignability),
  ADR-0031 (elevated markers, marker counting), ADR-0032 (handoff
  sibling lane, defang treatment), docs/19 §2 (impedance), §4
  (instrument), §6 (membrane).
