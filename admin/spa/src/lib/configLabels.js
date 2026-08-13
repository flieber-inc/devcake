// Human labels, formatting and danger warnings for config draft paths —
// drives the Save review dialog. Labels mirror the Field labels on the page.

export const AUTO_MERGE_COPY =
  "DevCake's app will merge this repository's pull requests to the default " +
  "branch without a human PR click (after its REVIEW step approves). Without " +
  "a reviewer token, merges proceed without a formal approval on the forge. " +
  "Missions on THIS repo already parked at DEVCAKE-MERGE are picked back up: " +
  "DevCake reopens their merge window and merges them as they become ready " +
  "(other repos are unaffected). " +
  "This toggle gates the app only — Devs still hold write forge tokens; " +
  "branch protection is what stops an agent from merging.";

export const ADOPTION_COPY =
  "DevCake will adopt EVERY non-completed Issue and Project in this team — " +
  "including the entire existing backlog — and start working through them by " +
  "priority, consuming tokens. In opt-in mode it only touches items you " +
  "label DEVCAKE.";

const onOff = (v) => (v ? "on" : "off");
// objects/arrays reaching a generic formatter render as JSON — a diff row
// must never show [object Object]
const orEmpty = (v) =>
  v === null || v === undefined || v === "" ? "(empty)"
    : typeof v === "object" ? JSON.stringify(v) : String(v);
const lines = (v) => ((v || []).length ? (v || []).join("\n") : "(none)");
// rate-card rows diff atomically (the Cost Inputs modal PUTs the whole
// list) — render them as readable per-1M lines, never [object Object]
const rateRows = (v) =>
  ((v || []).length
    ? (v || []).map((r) =>
        `${r.model_prefix}: $${r.input_per_mtok} in / ` +
        `$${r.cache_read_per_mtok} cr / $${r.cache_write_per_mtok} cw / ` +
        `$${r.output_per_mtok} out per 1M`).join("\n")
    : "(none)");

const EXACT = {
  "cfg.adoption_mode": {
    group: "PMO", label: "Adoption mode",
    format: (v) => (v === "opt_out" ? "opt-out (whole team)" : "opt-in (label required)"),
    warning: (o, n) => (n === "opt_out" ? ADOPTION_COPY : null),
  },
  "cfg.poll_interval_seconds": { group: "PMO", label: "Poll interval (s)" },
  "cfg.attach_merged_changeset_to_pmo": {
    group: "Repository", label: "Also attach merged change set to PMO", format: onOff,
  },
  "cfg.steward.dev_type": { group: "Limits & Traffic", label: "Steward Dev Type", format: orEmpty },
  "cfg.steward.interval_minutes": { group: "Limits & Traffic", label: "Steward interval (minutes)" },
  "cfg.steward.enabled": { group: "Limits & Traffic", label: "Steward periodic service", format: onOff },
  "cfg.max_decomposition_depth": {
    group: "Limits & Traffic", label: "Decomposition depth",
    format: (v) => (v === 0 ? "unlimited" : String(v)),
  },
  "cfg.budgets.freshness_rereviews": {
    group: "Limits & Traffic", label: "Freshness re-review budget",
    format: (v) => (v === 0 ? "unlimited" : String(v)),
  },
  "cfg.budgets.discoveries_per_run": {
    group: "Limits & Traffic", label: "Discoveries per run",
    format: (v) => (v === 0 ? "unlimited" : String(v)),
  },
  "cfg.concurrency.global_max": { group: "Limits & Traffic", label: "Global max Devs" },
  "cfg.dev_timeout_minutes": { group: "Limits & Traffic", label: "Dev run timeout (min)" },
  "cfg.review_loop_warning_every": { group: "Limits & Traffic", label: "Loop warning every N rejections" },
  "cfg.recover_misplaced_result": {
    group: "Limits & Traffic", label: "Accept misplaced result files", format: onOff,
  },
  "cfg.continuation_policy": { group: "Limits & Traffic", label: "Continuation policy" },
  "cfg.attempt_reset": {
    group: "Limits & Traffic", label: "Attempt reset policy",
    format: (v) => ({ "label-ops": "strict (DEVCAKE-RETRY / labels)",
                      "any-comment": "any comment",
                      unlimited: "unlimited (never give up)" }[v] || String(v)),
  },
  "cfg.brake_on_bad_output": {
    group: "Limits & Traffic", label: "Brake on missing results (exit 11)",
    format: onOff,
  },
  "cfg.repo_mirror.sync_max_age_seconds": {
    group: "Limits & Traffic", label: "Mirror sync max age (s)",
    format: (v) => (v === 0 ? "every dispatch" : String(v)),
  },
  "cfg.repo_mirror.lfs": {
    group: "Limits & Traffic", label: "Mirror LFS content", format: onOff,
  },
  "cfg.max_continuations": {
    group: "Limits & Traffic", label: "Max continuations per run",
    format: (v) => (v === 0 ? "off" : String(v)),
  },
  "cfg.cost_inputs.override_native": {
    group: "Cost", label: "Operator rates override displayed cost", format: onOff,
  },
  "cfg.cost_inputs.rates": {
    group: "Cost", label: "Cost rate card ($/1M tokens)",
    format: rateRows, multiline: true,
  },
};

const DEV_TYPE_FIELDS = {
  harness_template: { label: "Harness template" },
  max_concurrency: { label: "Max concurrency" },
  model: { label: "Model", format: orEmpty },
  identifying_prompt: { label: "Identifying prompt", format: orEmpty, multiline: true },
  mcp_setup_commands: { label: "MCP setup commands", format: lines, multiline: true },
  skills: { label: "Skills (available)", format: lines, multiline: true },
  skills_required: { label: "Skills (required)", format: lines, multiline: true },
  secret_env: { label: "Secret env vars", format: lines, multiline: true },
};

const ASSIGNMENT_FIELDS = {
  dev_type: { label: "Dev type" },
  extra_cli_args: { label: "Extra CLI args", format: orEmpty },
};

// Section display order for grouping rows in the dialog.
export const GROUP_ORDER = [
  "PMO", "Repository", "Dev Types", "Mission Types",
  "Prompts", "Limits & Traffic", "Cost", "Other",
];

// the instance LISTS diff atomically when a card is added/removed — show the
// resulting name sets, not [object Object]
const instanceNames = (v) =>
  ((v || []).length ? (v || []).map((r) => r.name).join(", ") : "(none)");

export function metaFor(path) {
  if (EXACT[path]) return { multiline: false, format: orEmpty, ...EXACT[path] };
  if (path === "cfg.repos")
    return { group: "Repository", label: "Repository cards",
             multiline: false, format: instanceNames };
  if (path === "cfg.pmos")
    return { group: "PMO", label: "PMO instances",
             multiline: false, format: instanceNames };
  let m = path.match(/^cfg\.repos\.(\d+)\.([^.]+)$/);
  if (m) {
    // meta objects so per-repo merge doctrine can carry format/warning
    // (ADR-0020); plain string values remain label-only identity fields
    const FIELDS = {
      name: { label: "Repo name" },
      forge: { label: "Forge" },
      url: { label: "Repository URL" },
      default_branch: { label: "Default branch" },
      api_base: { label: "API base" },
      auto_merge: {
        label: "Auto-merge", format: onOff,
        warning: (o, n) => (n === true ? AUTO_MERGE_COPY : null),
      },
      auto_resolve_merge_conflicts: {
        label: "Auto-resolve merge conflicts", format: onOff,
      },
      merge_retry_window_minutes: { label: "Merge retry window (min)" },
    };
    const f = FIELDS[m[2]] || { label: m[2] };
    return {
      group: "Repository", multiline: false, format: orEmpty, ...f,
      label: `Repo #${+m[1] + 1} · ${f.label}`,
    };
  }
  m = path.match(/^cfg\.pmos\.(\d+)\.(name|system|team_key|api_base|intake_paused|discovery_routing|managed)$/);
  if (m) {
    const FIELDS = { name: "Instance name", system: "System",
                     team_key: "Team key", api_base: "API base",
                     intake_paused: "Intake paused",
                     discovery_routing: "Discovery routing",
                     managed: "Managed (default board)" };
    const bool = m[2] === "intake_paused" || m[2] === "managed"
      || m[2] === "discovery_routing";
    return { group: "PMO", multiline: false, format: bool ? onOff : orEmpty,
             label: `PMO #${+m[1] + 1} · ${FIELDS[m[2]]}` };
  }
  m = path.match(/^cfg\.active_prompt_templates\.([^.]+)$/);
  if (m) {
    return { group: "Prompts", label: `${m[1]} active template`,
             multiline: false, format: orEmpty };
  }
  m = path.match(/^cfg\.pmos\.(\d+)\.repos$/);
  if (m) {
    return { group: "PMO", label: `Repositories (instance #${+m[1] + 1})`,
             multiline: false,
             format: (v) => ((v || []).length ? (v || []).join(" › ") : "(none — internal forge)") };
  }
  m = path.match(/^cfg\.pmos\.(\d+)\.reference_repos$/);
  if (m) {
    return { group: "PMO", label: `Reference repos (instance #${+m[1] + 1})`,
             multiline: false,
             format: (v) => ((v || []).length ? (v || []).join(", ") : "(none)") };
  }
  // ADR-0019 instance override rows: field-level edits recurse to leaves;
  // adding/removing a whole row diffs atomically at the row path
  m = path.match(/^cfg\.pmos\.(\d+)\.assignments\.([^.]+)\.(.+)$/);
  if (m && ASSIGNMENT_FIELDS[m[3]]) {
    const f = ASSIGNMENT_FIELDS[m[3]];
    return { group: "Mission Types", multiline: false, format: orEmpty, ...f,
             label: `${m[2]} override (PMO #${+m[1] + 1}) · ${f.label}` };
  }
  m = path.match(/^cfg\.pmos\.(\d+)\.assignments\.([^.]+)$/);
  if (m) {
    return { group: "Mission Types", multiline: false,
             label: `${m[2]} override (PMO #${+m[1] + 1})`,
             format: (v) => (v == null ? "(inherit global)"
                             : `${v.dev_type || "(unassigned)"}` +
                               (v.extra_cli_args ? ` · ${v.extra_cli_args}` : "")) };
  }
  m = path.match(/^cfg\.pmos\.(\d+)\.assignments$/);
  if (m) {
    return { group: "Mission Types", multiline: false,
             label: `Stage overrides (PMO #${+m[1] + 1})`,
             format: (v) => {
               const keys = Object.keys(v || {});
               return keys.length ? keys.join(", ") : "(none)";
             } };
  }
  m = path.match(/^devTypes\.([^.]+)\.(.+)$/);
  if (m && DEV_TYPE_FIELDS[m[2]]) {
    const f = DEV_TYPE_FIELDS[m[2]];
    return {
      group: "Dev Types", multiline: false, format: orEmpty, ...f,
      label: `${m[1]} · ${f.label}`,
    };
  }
  m = path.match(/^assignments\.([^.]+)\.(.+)$/);
  if (m && ASSIGNMENT_FIELDS[m[2]]) {
    const f = ASSIGNMENT_FIELDS[m[2]];
    return {
      group: "Mission Types", multiline: false, format: orEmpty, ...f,
      label: `${m[1]} · ${f.label}`,
    };
  }
  return { group: "Other", label: path, format: orEmpty, multiline: false };
}

// Decorate raw diff rows with label/group/formatted values/warnings.
export function describeDiff(diff) {
  return diff.map((d) => {
    const meta = metaFor(d.path);
    return {
      ...d,
      group: meta.group,
      label: meta.label,
      oldText: meta.format(d.old),
      newText: meta.format(d.new),
      multiline: !!meta.multiline,
      warning: meta.warning ? meta.warning(d.old, d.new) : null,
    };
  });
}
