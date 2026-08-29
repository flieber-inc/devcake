# ADR-0037 — Per-PMO prompt-template overrides (dual-crew playbooks)

- **Status:** accepted (2026-08-29)
- **Context:** Named Mission Type playbook template sets already exist
  (`Development`, `Customer Success`, operator copies under
  `/data/config/prompt_templates/`), but `AppConfig.active_prompt_templates`
  was deployment-global — every PMO instance dispatched the same ONBOARD /
  PLAN / EXECUTE / REVIEW playbook. A Customer Success board and a Development
  board on one deployment therefore shared prompts, which produces bad outcomes
  for at least one of the two. ADR-0019 solved the parallel problem for
  staffing; this ADR does the same for playbook selection (CAKE-150).

## Decision

### 1 — Overrides live on the instance, resolution lives in one function

`PMOInstance.active_prompt_templates: dict[str, str]` (default `{}`) holds
per-instance Mission Type → template name overrides.
`active_prompt_template_for(config, instance, mission_type)` (`config.py`) is
the ONLY resolver: the instance's name when the key is present, else
`AppConfig.active_prompt_templates.get(mission_type)` (which may be `None` —
`resolve_playbook` already falls back to the built-in Development preset).
Dispatch (`dispatch.py` `_pb`) and health (`template_warnings`) go through it;
no second lookup of the raw maps.

**Presence = override:** an absent key inherits the global active *live* — a
later global edit applies to inheriting instances immediately. An empty-string
value is refused at validation ("remove the key to inherit"); unknown
mission-type keys are refused loudly (a typo would otherwise be silently
inert). Template-name existence vs disk is checked at PUT/bundle time (same
split as the global map — no disk I/O in pydantic validators).

### 2 — Placement on `PMOInstance`, not a parallel map

Overrides ride the `pmos` list: deleting an instance deletes its overrides
(config lists replace wholesale — docs/10 §3), profiles/export/import carry
them for free (ADR-0013), and no instance-name-keyed sibling structure can
drift against the instance list. Operator-tunable on managed rows
(`reconcile_managed_pmos`), like `assignments`.

### 3 — Reference hygiene matches the global map

- Template **DELETE** 409s while the name is the global active **or** any
  PMO override for that Mission Type (the error names the holding instances).
- Bundle/profile **apply** validates `pmos[*].active_prompt_templates` the
  same way as the global actives (known Mission Type; name exists as builtin
  or stored operator template).
- Health walks every effective selection (global ∪ each instance override)
  without duplicate spam when override == global.

### 4 — Admin UI

The Prompts section keeps the global per-Mission-Type active selects and adds
one override block per configured PMO card: a select whose inherit option
names the effective global template; choosing a template writes the override
key; choosing inherit deletes it. Draft semantics unchanged
(`useConfigDraft` / `setField`); template body CRUD stays Immediate. Draft
load seeds `active_prompt_templates: {}` on every PMO card so diffs stay
per-key.

### 5 — Out of scope

- Per-PMO Dev Type identifying-prompt overrides (`active_devtype_prompts`
  stays global / per-Dev-Type).
- New builtin workflow presets or rewriting Development / Customer Success
  prose.
- Changing `resolve_playbook` fallback semantics or on-disk layout.
- Priority-conditional or rule-language selection.

## Consequences

- Dual-crew playbooks work today: the CS instance can run Customer Success
  ONBOARD while eng keeps Development — one deployment, one control plane,
  different prompts (completing the ADR-0009 / ADR-0019 operator story for
  playbooks).
- Existing deployments keep working unchanged: unset overrides inherit the
  global `active_prompt_templates` map (empty = Development via
  `resolve_playbook`).
- GET `/prompt-templates` `active` payload remains the **global** actives for
  the Prompts section; per-PMO overrides ride `pmos` in GET/PUT `/config`.

## Related

- ADR-0019 (placement / presence / inherit idiom)
- ADR-0013 (bundle serializes whole config)
- ADR-0030 (`reconcile_managed_pmos` operator-tunable list)
- docs/02 §9, docs/10 §3, docs/11 Prompts
