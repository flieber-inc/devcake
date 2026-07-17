// Human labels, formatting and danger warnings for config draft paths —
// drives the Save review dialog. Labels mirror the Field labels on the page.

export const AUTO_MERGE_COPY =
  "DevCake will merge its own pull requests to the default branch without " +
  "human intervention (after its REVIEW step approves). Without a reviewer " +
  "token, merges proceed without a formal approval on the forge. Missions " +
  "already parked at DEVCAKE-MERGE are picked back up: DevCake reopens " +
  "their merge window and merges them as they become ready.";

export const ADOPTION_COPY =
  "DevCake will adopt EVERY non-completed Issue and Project in this team — " +
  "including the entire existing backlog — and start working through them by " +
  "priority, consuming tokens. In opt-in mode it only touches items you " +
  "label DEVCAKE.";

const onOff = (v) => (v ? "on" : "off");
const orEmpty = (v) => (v === null || v === undefined || v === "" ? "(empty)" : String(v));
const lines = (v) => ((v || []).length ? (v || []).join("\n") : "(none)");

const EXACT = {
  "cfg.adoption_mode": {
    group: "PMO", label: "Adoption mode",
    format: (v) => (v === "opt_out" ? "opt-out (whole team)" : "opt-in (label required)"),
    warning: (o, n) => (n === "opt_out" ? ADOPTION_COPY : null),
  },
  "cfg.poll_interval_seconds": { group: "PMO", label: "Poll interval (s)" },
  "cfg.auto_merge": {
    group: "Repository", label: "Auto-merge", format: onOff,
    warning: (o, n) => (n === true ? AUTO_MERGE_COPY : null),
  },
  "cfg.auto_resolve_merge_conflicts": {
    group: "Repository", label: "Auto-resolve merge conflicts", format: onOff,
  },
  "cfg.merge_retry_window_minutes": { group: "Repository", label: "Merge retry window (min)" },
  "cfg.relations_mapper.dev_type": { group: "Traffic control", label: "Mapper Dev Type", format: orEmpty },
  "cfg.relations_mapper.interval_minutes": { group: "Traffic control", label: "Mapper interval (minutes)" },
  "cfg.relations_mapper.enabled": { group: "Traffic control", label: "Mapper periodic service", format: onOff },
  "cfg.concurrency.global_max": { group: "Limits", label: "Global max Devs" },
  "cfg.dev_timeout_minutes": { group: "Limits", label: "Dev run timeout (min)" },
  "cfg.review_loop_warning_every": { group: "Limits", label: "Loop warning every N rejections" },
};

const DEV_TYPE_FIELDS = {
  harness_template: { label: "Harness template" },
  max_concurrency: { label: "Max concurrency" },
  model: { label: "Model", format: orEmpty },
  identifying_prompt: { label: "Identifying prompt", format: orEmpty, multiline: true },
  mcp_setup_commands: { label: "MCP setup commands", format: lines, multiline: true },
  secret_env: { label: "Secret env vars", format: lines, multiline: true },
};

const ASSIGNMENT_FIELDS = {
  dev_type: { label: "Dev type" },
  extra_cli_args: { label: "Extra CLI args", format: orEmpty },
};

// Section display order for grouping rows in the dialog.
export const GROUP_ORDER = [
  "Traffic control", "PMO", "Repository", "Dev Types", "Assignments",
  "Prompts", "Limits", "Other",
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
    const FIELDS = { name: "Repo name", forge: "Forge", url: "Repository URL",
                     default_branch: "Default branch", api_base: "API base" };
    return { group: "Repository", multiline: false, format: orEmpty,
             label: `Repo #${+m[1] + 1} · ${FIELDS[m[2]] || m[2]}` };
  }
  m = path.match(/^cfg\.pmos\.(\d+)\.(name|system|team_key|api_base)$/);
  if (m) {
    const FIELDS = { name: "Instance name", system: "System",
                     team_key: "Team key", api_base: "API base" };
    return { group: "PMO", multiline: false, format: orEmpty,
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
      group: "Assignments", multiline: false, format: orEmpty, ...f,
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
