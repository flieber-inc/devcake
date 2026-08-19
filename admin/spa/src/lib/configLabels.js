// Human labels, formatting and danger warnings for config draft paths —
// drives the Save review dialog. Labels mirror the Field labels on the page.

import { ATTEMPT_RESET_POLICIES } from "./configEnums.js";

export const AUTO_MERGE_COPY =
  "DevCake's app will merge this repository's pull requests to the default " +
  "branch without a human PR click (after its REVIEW step approves). Without " +
  "a reviewer token, merges proceed without a formal approval on the forge. " +
  "Missions on THIS repo already parked at DEVCAKE-MERGE are picked back up: " +
  "DevCake reopens their merge window and merges them as they become ready " +
  "(other repos are unaffected). " +
  "This toggle gates the app only — Devs still hold write forge tokens; " +
  "forge branch protection (operator-owned; not a DevCake hard gate) is what " +
  "stops an agent from merging.";

export const MEMORY_AUTO_MERGE_COPY =
  "Recommended: keep this off so a person merges every note. " +
  "With this on, a note the Curator wrote becomes official once " +
  "another Dev (a Reviewer) approves it. That is two models in a " +
  "row. It is not a person. It is not the reviewer token. A wrong " +
  "note can guide every later run until you revert it. Everything " +
  "stays in git: every merge names its run and can be reverted.";

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
  "cfg.steward.dev_type": { group: "Scheduled Tasks", label: "Relations Steward · Dev Type", format: orEmpty },
  "cfg.steward.interval_minutes": { group: "Scheduled Tasks", label: "Relations Steward · Interval (minutes)" },
  "cfg.steward.enabled": { group: "Scheduled Tasks", label: "Relations Steward · Periodic service", format: onOff },
  "cfg.steward.playbook_template": {
    group: "Scheduled Tasks", label: "Relations Steward · Instructions",
    format: orEmpty, multiline: true,
  },
  "cfg.context_sourcing_strict": {
    group: "Limits", label: "Context sourcing strict", format: onOff,
  },
  "cfg.memory_auto_merge": {
    group: "Limits", label: "Memory auto-merge", format: onOff,
    warning: (o, n) => (n === true ? MEMORY_AUTO_MERGE_COPY : null),
  },
  "cfg.budgets.claims_queue_max": {
    group: "Limits", label: "Claims queue max",
    format: (v) => (v === 0 ? "unlimited" : String(v)),
  },
  "cfg.max_decomposition_depth": {
    group: "Limits", label: "Decomposition depth",
    format: (v) => (v === 0 ? "unlimited" : String(v)),
  },
  "cfg.budgets.freshness_rereviews": {
    group: "Limits", label: "Freshness re-review budget",
    format: (v) => (v === 0 ? "unlimited" : String(v)),
  },
  "cfg.budgets.discoveries_per_run": {
    group: "Limits", label: "Discoveries per run",
    format: (v) => (v === 0 ? "unlimited" : String(v)),
  },
  "cfg.container_limits.memory_mb": {
    group: "Limits", label: "Container memory (MB)",
    format: (v) => (v === 0 ? "unlimited" : String(v)),
  },
  "cfg.container_limits.cpus": {
    group: "Limits", label: "Container CPUs",
    format: (v) => (v === 0 ? "unlimited" : String(v)),
  },
  "cfg.container_limits.pids": {
    group: "Limits", label: "Container PIDs",
    format: (v) => (v === 0 ? "unlimited" : String(v)),
  },
  "cfg.concurrency.global_max": { group: "Limits", label: "Global max Devs" },
  "cfg.dev_timeout_minutes": { group: "Limits", label: "Dev run timeout (min)" },
  "cfg.review_loop_warning_every": { group: "Limits", label: "Loop warning every N rejections" },
  "cfg.recover_misplaced_result": {
    group: "Limits", label: "Accept misplaced result files", format: onOff,
  },
  "cfg.continuation_policy": { group: "Limits", label: "Continuation policy" },
  "cfg.attempt_reset": {
    group: "Limits", label: "Attempt reset policy",
    // Operator-facing labels only — vocabulary lives in configEnums.js /
    // spa-contracts (ATTEMPT_RESET_POLICIES).
    format: (v) => {
      const labels = {
        "label-ops": "strict (DEVCAKE-RETRY / labels)",
        "any-comment": "any comment",
        unlimited: "unlimited (never give up)",
      };
      if (ATTEMPT_RESET_POLICIES.includes(v) && labels[v]) return labels[v];
      return String(v);
    },
  },
  "cfg.brake_on_bad_output": {
    group: "Limits", label: "Brake on missing results (exit 11)",
    format: onOff,
  },
  "cfg.repo_mirror.sync_max_age_seconds": {
    group: "Limits", label: "Mirror sync max age (s)",
    format: (v) => (v === 0 ? "every dispatch" : String(v)),
  },
  "cfg.repo_mirror.lfs": {
    group: "Limits", label: "Mirror LFS content", format: onOff,
  },
  "cfg.max_continuations": {
    group: "Limits", label: "Max continuations per run",
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
  backend_base_url: { label: "Backend base URL", format: orEmpty },
  cli_version: { label: "CLI version", format: orEmpty },
  identifying_prompt: { label: "Identifying prompt", format: orEmpty, multiline: true },
  dev_entrypoint: { label: "Entrypoint script", format: orEmpty, multiline: true },
  override_harness_adapter: { label: "Override harness adapter", format: onOff },
  skills: { label: "Skills (available)", format: lines, multiline: true },
  skills_required: { label: "Skills (required)", format: lines, multiline: true },
  secret_env: { label: "Secret env vars", format: lines, multiline: true },
  memory_repos: { label: "Memory (domain-bound)", format: lines, multiline: true },
};

const ASSIGNMENT_FIELDS = {
  dev_type: { label: "Dev type" },
  extra_cli_args: { label: "Extra CLI args", format: orEmpty },
};

// Section display order for grouping rows in the dialog.
export const GROUP_ORDER = [
  "PMO", "Repository", "Skills", "Dev Types", "Mission Types",
  "Prompts", "Limits", "Scheduled Tasks", "Cost", "Other",
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
      merge_settle_minutes: { label: "Post-approve settle (min)" },
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
  m = path.match(/^cfg\.pmos\.(\d+)\.memory_repos$/);
  if (m) {
    return { group: "PMO", label: `Memory notebooks (instance #${+m[1] + 1})`,
             multiline: false,
             format: (v) => ((v || []).length ? (v || []).join(", ") : "(none)") };
  }
  m = path.match(/^cfg\.skill_sources$/);
  if (m) {
    return { group: "Skills", label: "Skill sources",
             multiline: false, format: instanceNames };
  }
  m = path.match(/^cfg\.skill_sources\.(\d+)\.([^.]+)$/);
  if (m) {
    const FIELDS = {
      name: { label: "Name" },
      forge: { label: "Forge" },
      url: { label: "Repository URL" },
      default_branch: { label: "Branch" },
      subdir: { label: "Skills folder" },
    };
    const f = FIELDS[m[2]] || { label: m[2] };
    return { group: "Skills", multiline: false, format: orEmpty, ...f,
             label: `Skill source #${+m[1] + 1} · ${f.label}` };
  }
  m = path.match(/^cfg\.crons$/);
  if (m) {
    return { group: "Scheduled Tasks", label: "Scheduled tasks",
             multiline: false,
             format: (v) => ((v || []).length
               ? (v || []).map((r) => r.id).join(", ") : "(none)") };
  }
  m = path.match(/^cfg\.crons\.(\d+)\.([^.]+)$/);
  if (m) {
    const FIELDS = {
      id: { label: "Task id" },
      name: { label: "Name" },
      enabled: { label: "Periodic service", format: onOff },
      interval_minutes: { label: "Interval (minutes)" },
      pmo: { label: "Target board", format: orEmpty },
      entry_stage: { label: "Entry stage" },
      description_template: { label: "Ticket text", format: orEmpty,
                              multiline: true },
      reserved: { label: "Reserved", format: onOff },
    };
    const f = FIELDS[m[2]] || { label: m[2] };
    return { group: "Scheduled Tasks", multiline: false, format: orEmpty,
             ...f, label: `Task #${+m[1] + 1} · ${f.label}` };
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
