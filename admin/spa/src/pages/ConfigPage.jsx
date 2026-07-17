import React, { useEffect, useState } from "react";
import { Play, Plus, Trash2, KeyRound, Upload } from "lucide-react";
import { get, send } from "../api.js";
import PageHeader from "../components/PageHeader.jsx";
import { Section } from "../components/Card.jsx";
import { Field, Help, ListTextarea, SecretField, Input, Select, Textarea } from "../components/Field.jsx";
import InstantZone from "../components/InstantZone.jsx";
import SettingRow from "../components/SettingRow.jsx";
import Button from "../components/Button.jsx";
import Toggle from "../components/Toggle.jsx";
import { ConfirmDialog, Modal, PromptDialog } from "../components/Modal.jsx";
import ImmediateBadge from "../components/ImmediateBadge.jsx";
import MoreMenu from "../components/MoreMenu.jsx";
import PromptsSection from "../components/PromptsSection.jsx";
import SelectionChips from "../components/SelectionChips.jsx";
import { ADOPTION_COPY } from "../lib/configLabels.js";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { CONFIG_SECTIONS } from "../lib/nav.js";
import { getRegistry, loadRegistry } from "../lib/registry.js";
import { nextFreeName, useNewNames } from "../lib/instanceNames.js";

const MISSION_TYPES = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];
// harness identities (image, credential requirements, OAuth availability) come
// from GET /harnesses — the registry is authoritative, nothing is hardcoded here

// ── OAuth wizard (docs/16 M6): device-code flow driven from the UI ──────────
// Immediate by nature: the credential is stored server-side the moment the
// device flow completes — it cannot wait for the page's Save.

function OAuthWizard({ devType, onClose }) {
  const [status, setStatus] = useState({ state: "starting" });
  const [runId, setRunId] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let timer;
    send("POST", `/oauth/dev-types/${devType}/start`)
      .then(({ run_id }) => {
        setRunId(run_id);
        timer = setInterval(
          () => get(`/oauth/status/${run_id}`).then(setStatus).catch(() =>
            setStatus({ state: "failed", error: "session lost (app restarted?) — close and retry" })),
          3000
        );
      })
      .catch((e) => setError(String(e)));
    return () => clearInterval(timer);
  }, [devType]);
  return (
    <Modal onClose={onClose}>
        <h4 className="mb-3 text-base font-semibold tracking-tight">Connect {devType} (OAuth)</h4>
        {error && <p className="text-sm text-red-600">{error}</p>}
        {!error && status.state === "starting" && (
          <p className="text-sm text-neutral-500">Starting a login container…</p>
        )}
        {status.state === "awaiting_user" && (
          <div className="space-y-3 text-sm">
            <p>Open this URL and confirm the code:</p>
            <a
              className="block break-all rounded-md bg-neutral-100 p-2 font-mono text-xs underline dark:bg-neutral-800"
              href={status.url}
              target="_blank"
              rel="noopener"
            >
              {status.url}
            </a>
            <p className="text-center text-2xl font-bold tracking-widest">{status.code}</p>
            <p className="text-xs text-neutral-500 dark:text-neutral-400">
              Waiting for approval… this dialog completes automatically.
            </p>
          </div>
        )}
        {status.state === "completed" && (
          <p className="text-sm text-green-700 dark:text-green-400">
            ✅ Credential stored. This Dev Type is ready.
          </p>
        )}
        {status.state === "failed" && (
          <p className="text-sm text-red-600">Login failed: {status.error}</p>
        )}
        <div className="mt-5 flex justify-end">
          <Button kind="ghost" onClick={onClose}>
            {status.state === "completed" ? "Done" : "Close"}
          </Button>
        </div>
    </Modal>
  );
}

// ── Dev Type editor card ─────────────────────────────────────────────────────

// Per-required-file upload: the stored filename is FORCED to the registry's
// secret_file so the checklist and dispatch's _credential_spec always line up,
// whatever the local file is called. Stores immediately (server-side write).
function UploadButton({ devType, secretFile, onDone }) {
  const [msg, setMsg] = useState("");
  const inputId = `up-${devType}-${secretFile}`;
  const upload = async (file) => {
    if (!file) return;
    try {
      await send("POST", `/dev-types/${devType}/credentials`, {
        filename: secretFile,
        content: await file.text(),
      });
      setMsg("✓ stored");
      onDone && onDone();
    } catch (e) {
      setMsg(`✗ ${String(e.message || e)}`);
    }
    setTimeout(() => setMsg(""), 4000);
  };
  return (
    <span className="inline-flex items-center gap-1.5">
      <input id={inputId} type="file" className="hidden"
        onChange={(e) => { upload(e.target.files[0]); e.target.value = ""; }} />
      <label htmlFor={inputId}
        title="Stores the credential immediately — does not wait for Save"
        className="inline-flex cursor-pointer items-center gap-1 rounded border border-neutral-300 px-1.5 py-0.5 text-[11px] hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800">
        <Upload size={10} aria-hidden /> upload…
      </label>
      {msg && <span className="text-[11px] text-green-700 dark:text-green-400">{msg}</span>}
    </span>
  );
}

// Fully controlled: the card renders and edits the shared draft. Save is the
// page-level Save; only Delete / OAuth / upload act immediately.
function DevTypeCard({ name, draftDt, serverDt, harnesses, setField, onDelete, onOAuth, onRename, onCredChange, skillsCatalog }) {
  const d = draftDt;
  const set = (k, v) => setField(`devTypes.${name}.${k}`, v);
  const h = harnesses[d.harness_template] || {};   // registry info for the DRAFTED harness
  const pending = d.harness_template !== serverDt.harness_template;   // unsaved switch
  const [envSet, setEnvSet] = useState({});
  useEffect(() => {
    setEnvSet({});
    const names = (h.credential_env || []).join(",");
    if (names)
      get(`/secrets-check?harness=${encodeURIComponent(names)}`)
        .then((r) => setEnvSet(Object.fromEntries(
          Object.entries(r.harness || {}).map(([k, v]) => [k, v.present]))))
        .catch(() => {});
  }, [d.harness_template]);
  const filePresent = (sf) => (serverDt.secrets_present || []).includes(sf);
  const ready = Object.values(envSet).some(Boolean) ||
                (h.credential_files || []).some((cf) => filePresent(cf.secret_file));
  return (
    <div className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-mono text-sm font-semibold">{name}</span>
        <div className="flex items-center gap-2">
          {/* keyed on the DRAFTED harness so switching to an OAuth-capable
              harness shows the button immediately; disabled until the switch
              is saved (the device flow runs against the SAVED harness) */}
          {h.oauth_available && (
            <Button kind="ghost" icon={KeyRound} disabled={pending}
              title={pending
                ? "Save the harness change first — OAuth runs the saved harness's login flow"
                : undefined}
              onClick={() => !pending && onOAuth(name)}>
              Connect via OAuth…{pending ? " (save first)" : ""}
            </Button>
          )}
          <MoreMenu label={`More actions for ${name}`} items={[
            { label: "Rename",
              desc: "Config, credentials and prompt templates follow the new name.",
              onClick: () => onRename(name) },
            { label: "Delete Dev Type", danger: true,
              desc: "Removes its config; stored credentials stay on disk.",
              onClick: () => onDelete(name) },
          ]} />
        </div>
      </div>
      {/* one line: harness · model · a much smaller max-concurrency box */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_1fr_6rem]">
        <Field label="Harness template"
          help="Which coding agent this Dev runs: claude-code (Claude Code), grok-build (Grok Build) or codex (Codex). Authoritative — the Docker image and credential requirements below follow it automatically on Save.">
          <Select
            value={d.harness_template}
            onChange={(e) => set("harness_template", e.target.value)}
          >
            {Object.keys(harnesses).map((t) => <option key={t}>{t}</option>)}
          </Select>
        </Field>
        <Field label="Model" hint="Empty = harness default"
          help="Pins the model the harness runs (claude --model / codex -m / grok --model), e.g. claude-fable-5 for Claude Code. Leave empty to let the harness pick its own default.">
          <Input value={d.model || ""}
            placeholder={h.default_model ? `harness default: ${h.default_model}` : "e.g. claude-fable-5"}
            onChange={(e) => set("model", e.target.value)} />
        </Field>
        <Field label="Max conc."
          help="How many Devs of this type may run at once. The global ceiling under Limits still applies on top.">
          <Input
            type="number" min="1" value={d.max_concurrency}
            onChange={(e) => set("max_concurrency", Number(e.target.value))}
            onBlur={(e) => set("max_concurrency", Math.max(1, Number(e.target.value) || 1))}
          />
        </Field>
      </div>
      <p className="text-xs text-neutral-500 dark:text-neutral-400">
        Identifying prompt is managed in the{" "}
        <a className="underline" href="#/config/prompts">Prompts section</a>.
      </p>
      <Field
        label="MCP setup commands (one per line)"
        hint="⚠ Runs arbitrary code in the Dev container before the agent starts."
        help="A failing or hung command fails the run (exit 14, 300 s cap per command) with the command and stderr in the run error."
      >
        <ListTextarea
          rows={2}
          value={d.mcp_setup_commands || []}
          onChange={(v) => set("mcp_setup_commands", v)}
        />
      </Field>
      <Field
        label="Secret env vars (one per line)"
        hint="Names only (UPPER_SNAKE_CASE) — paste each value below."
        help="Values live in the secret store, never in config, and are delivered to this Dev Type's runs so MCP setup commands can reference them (e.g. $DD_API_KEY). A name referenced by a setup command must have a stored value or the mission won't dispatch."
      >
        <ListTextarea
          rows={2}
          value={d.secret_env || []}
          onChange={(v) => set("secret_env", v)}
        />
      </Field>
      {(d.secret_env || []).length > 0 && (
        <InstantZone note="secret values store immediately">
          {[...new Set(d.secret_env || [])].map((v) => (
            <SecretField key={v} label={v}
              help={`Delivered to ${name} runs as $${v}. Stored securely — never echoed, never in .env.`}
              refKey={v} checkKind="harness" paste />
          ))}
        </InstantZone>
      )}
      <SelectionChips label="Skills"
        help={`Skill-store skills installed to ~/${h.skills_dir || ".claude/skills"} inside the Dev container before the agent starts. The catalog lives in the Skills section below.`}
        options={(skillsCatalog?.skills || []).map((s) => ({
          name: s.name, title: s.description || undefined }))}
        selected={d.skills || []}
        disabled={!h.skills_dir}
        disabledNote={`The ${d.harness_template} harness does not support skill-store skills${
          !h.skills_dir && (d.skills || []).length
            ? ` — ${d.skills.length} selected skill(s) will be skipped`
            : ""}.`}
        emptyNote="no skills in the catalog yet — see the Skills section below"
        staleNote="not in the skill store — skipped at dispatch; click to remove"
        onChange={(next) => {
          // write back in CATALOG order (unknown names last): uncheck-then-
          // recheck must not surface a reorder-only dirty diff (diffLeaves
          // compares arrays order-sensitively); skill order has no meaning
          const cat = (skillsCatalog?.skills || []).map((c) => c.name);
          set("skills", [...cat.filter((n) => next.includes(n)),
                         ...next.filter((n) => !cat.includes(n))]);
        }} />
      <InstantZone className="text-xs" note="credentials store immediately">
        <div className="flex items-center justify-between">
          <span>
            Image <span className="font-mono">{h.docker_image || "?"}</span>
            <Help text="Derived from the harness template — not editable. Changing the harness changes the image and the credential requirements below." />
          </span>
          <span className={ready
            ? "font-medium text-green-700 dark:text-green-400"
            : "font-medium text-amber-600 dark:text-amber-400"}>
            {ready ? "✓ credentials ready" : "✗ no credentials configured"}
          </span>
        </div>
        {pending && (
          <p className="text-amber-600 dark:text-amber-400">
            ⚠ Unsaved harness change: on Save this Dev Type runs {h.docker_image} and
            needs the credentials listed below. Files under /data/secrets/{name}/
            for the old harness are kept but unused.
          </p>
        )}
        {!h.oauth_available && (
          <p className="text-neutral-500 dark:text-neutral-400">
            {d.harness_template} has no device-code OAuth flow — it
            authenticates via a pasted key/token below
            {d.harness_template === "claude-code"
              ? " (run `claude setup-token` locally and paste the CLAUDE_CODE_OAUTH_TOKEN, or use an ANTHROPIC_API_KEY)"
              : ""}.
          </p>
        )}
        <div className="space-y-2">
          {(h.credential_env || []).map((v) => (
            <SecretField key={v} label={v}
              help={`API key for the ${d.harness_template} harness. Stored securely — never echoed, never in .env.`}
              refKey={v} checkKind="harness" paste />
          ))}
        </div>
        <ul className="space-y-1">
          {(h.credential_files || []).map((cf) => (
            <li key={cf.secret_file} className="flex items-center gap-2">
              <span>
                {filePresent(cf.secret_file) ? "✓" : "✗"} file{" "}
                <span className="font-mono">{cf.secret_file}</span>
                <span className="text-neutral-500 dark:text-neutral-400"> → {cf.path_hint}</span>
              </span>
              <UploadButton devType={name} secretFile={cf.secret_file} onDone={onCredChange} />
            </li>
          ))}
        </ul>
        <p className="text-neutral-500 dark:text-neutral-400">
          Any one ✓ is enough — env keys pass through at dispatch; files are
          delivered securely to each run (stored 0600 under /data/secrets/{name}/).
        </p>
      </InstantZone>
    </div>
  );
}

// "New Dev Type" dialog: name + harness picked up front — the old flow
// created "new-dev-###" instantly and made the operator rename it after.
function NewDevTypeDialog({ harnesses, onClose, onCreated }) {
  const [name, setName] = useState("");
  const [harness, setHarness] = useState(Object.keys(harnesses)[0] || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const create = async () => {
    setBusy(true); setErr("");
    try {
      await send("POST", "/dev-types", {
        name: name.trim(), harness_template: harness,
        identifying_prompt: "", max_concurrency: 1,
      });
      onCreated(); onClose();
    } catch (e) { setErr(String(e.message || e).replace(/^\d+ /, "")); }
    finally { setBusy(false); }
  };
  return (
    <Modal onClose={busy ? undefined : onClose}>
      <h4 className="mb-3 text-base font-semibold tracking-tight">New Dev Type</h4>
      <div className="space-y-3">
        <Field label="Name" hint="lowercase letters/digits/dashes — e.g. senior-dev">
          <Input value={name} onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && name.trim() && !busy && create()} />
        </Field>
        <Field label="Harness template"
          help="Which coding agent this Dev runs. The Docker image and credential requirements follow it — both can be changed later.">
          <Select value={harness} onChange={(e) => setHarness(e.target.value)}>
            {Object.keys(harnesses).map((t) => <option key={t}>{t}</option>)}
          </Select>
        </Field>
        {err && <p className="text-sm text-red-600 dark:text-red-400">✗ {err}</p>}
        <div className="flex justify-end gap-2">
          <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button disabled={busy || !name.trim() || !harness} onClick={create}>
            {busy ? "Creating…" : "Create Dev Type"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// ── skill authoring (docs/11 Skills section) ─────────────────────────────────

// browser-safe base64 for uploaded files (chunked — a spread over a large
// Uint8Array overflows the call stack)
async function fileToB64(file) {
  const buf = new Uint8Array(await file.arrayBuffer());
  let s = "";
  for (let i = 0; i < buf.length; i += 0x8000)
    s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  return btoa(s);
}

// "Add skill" dialog: Write (name + trigger + markdown; the app generates
// the frontmatter — the operator never sees YAML) or Import (upload a
// SKILL.md + optional supporting files). 409 flips into an explicit
// overwrite confirmation instead of silently replacing.
function AddSkillDialog({ onClose, onSaved }) {
  const [mode, setMode] = useState("write");
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [body, setBody] = useState("");
  const [files, setFiles] = useState([]);      // [{path, content_b64}]
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [askOverwrite, setAskOverwrite] = useState(false);

  const canSubmit = mode === "write"
    ? name.trim() && desc.trim() && body.trim()
    : files.length > 0;

  // any content edit invalidates a pending 409/overwrite decision — the
  // collision was for the OLD name/payload, so re-check from scratch
  const edited = (setter) => (v) => { setter(v); setAskOverwrite(false); setErr(""); };

  const submit = async (overwrite) => {
    setBusy(true); setErr("");
    try {
      if (mode === "write")
        await send("POST", "/skills",
          { name: name.trim(), description: desc.trim(), body, overwrite });
      else
        await send("POST", "/skills/import", { files, overwrite });
      onSaved(); onClose();
    } catch (e) {
      setAskOverwrite(e.status === 409);
      setErr(String(e.message || e).replace(/^\d+ /, ""));
    } finally { setBusy(false); }
  };

  return (
    <Modal className="max-w-2xl" onClose={busy ? undefined : onClose}>
      <div className="mb-4 flex items-center justify-between">
        <h4 className="text-base font-semibold tracking-tight">Add skill</h4>
        <div className="flex gap-1 rounded-md bg-stone-100 p-0.5 text-xs dark:bg-neutral-800">
          {[["write", "Write"], ["import", "Import files"]].map(([m, l]) => (
            <button key={m} type="button"
              onClick={() => { setMode(m); setErr(""); setAskOverwrite(false); }}
              className={`rounded px-2.5 py-1 font-medium transition ${
                mode === m ? "bg-white shadow-sm dark:bg-neutral-700"
                           : "text-neutral-500 hover:text-neutral-700 dark:hover:text-neutral-300"}`}>
              {l}
            </button>
          ))}
        </div>
      </div>
      {mode === "write" ? (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field label="Name" hint="lowercase, - or _ (e.g. release-notes)">
              <Input value={name} onChange={(e) => edited(setName)(e.target.value)}
                placeholder="my-skill" />
            </Field>
            <Field label="When should the agent use it?"
              hint="this description is the trigger — be specific">
              <Input value={desc} onChange={(e) => edited(setDesc)(e.target.value)}
                placeholder="Writes release notes: use when a release is being prepared." />
            </Field>
          </div>
          <Field label="Instructions (markdown)">
            <Textarea rows={10} value={body}
              onChange={(e) => edited(setBody)(e.target.value)}
              placeholder={"# Release notes\n\nStep-by-step guidance the agent should follow…"} />
          </Field>
        </div>
      ) : (
        <div className="space-y-3">
          <Field label="Skill folder"
            hint="pick the skill's folder (containing SKILL.md) — nested files keep their layout; the name comes from the frontmatter">
            {/* webkitdirectory: a plain multi-file picker only exposes
                basenames, which silently flattens refs/x.md → x.md and
                collides same-named files. A folder pick carries
                webkitRelativePath, so the layout under the skill dir survives. */}
            <input type="file" webkitdirectory="" directory=""
              className="block w-full text-sm text-neutral-500 file:mr-3 file:rounded-md file:border-0 file:bg-stone-100 file:px-3 file:py-1.5 file:text-sm file:font-medium dark:file:bg-neutral-800"
              onChange={async (e) => {
                setErr(""); setAskOverwrite(false);
                const fs = [];
                for (const f of e.target.files) {
                  // "<folder>/<rel>" → drop the chosen folder's own name
                  const rel = (f.webkitRelativePath || f.name)
                    .split("/").slice(1).join("/") || f.name;
                  // skip OS/VCS cruft a folder pick sweeps in (.DS_Store, .git/…)
                  if (rel.split("/").some((seg) => seg.startsWith("."))) continue;
                  fs.push({ path: rel, content_b64: await fileToB64(f) });
                }
                setFiles(fs);
              }} />
          </Field>
          {files.length > 0 && (
            <p className="text-xs text-neutral-500 dark:text-neutral-400">
              {files.length} file(s): {files.map((f) => f.path).join(", ")}
            </p>
          )}
        </div>
      )}
      {err && (
        <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
          {err}
        </p>
      )}
      <div className="mt-5 flex justify-end gap-2">
        <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
        {askOverwrite ? (
          <Button kind="danger" disabled={busy || !canSubmit} onClick={() => submit(true)}>
            {busy ? "Working…" : "Overwrite existing skill"}
          </Button>
        ) : (
          <Button disabled={busy || !canSubmit} onClick={() => submit(false)}>
            {busy ? "Working…" : "Save to store"}
          </Button>
        )}
      </div>
    </Modal>
  );
}

// ── the page ─────────────────────────────────────────────────────────────────

// Repo-flavored SelectionChips (PMO repo set + reference repos, v0.1.2):
// selection order is the list order; entries selected in the SIBLING list
// render disabled (the two sets are disjoint by config validation).
function RepoChips({ label, help, all, selected, excluded, excludedNote,
                     unavailable = [], unavailableNote = "",
                     firstBadge = "", onChange }) {
  return (
    <SelectionChips label={label} help={help}
      options={all.map((r) => ({
        name: r.name,
        disabled: excluded.includes(r.name) || unavailable.includes(r.name),
        disabledNote: unavailable.includes(r.name)
          ? unavailableNote
          : `already selected as a ${excludedNote}`,
      }))}
      selected={selected} onChange={onChange} firstBadge={firstBadge}
      emptyNote="no repositories configured — add them on the Repositories page"
      staleNote="this repo card no longer exists — click to remove the stale entry" />
  );
}

export default function ConfigPage({ section }) {
  // the draft, reload, harnesses and health snapshot come from the shared
  // provider (v0.1.1 B4) — the Repositories page edits the SAME draft, and
  // DraftChrome (App-level) owns Save/DirtyBar/NavGuard
  const { dr, reload, harnesses, healthInfo, loadErr } = useSharedDraft();
  const [registry, setRegistry] = useState(getRegistry());
  useEffect(() => { loadRegistry().then(setRegistry); }, []);
  const [confirm, setConfirm] = useState(null); // flip-time danger + delete confirms
  // repos WITHOUT a stored Access (write) token cannot join a PMO's WORK
  // set (founder request 2026-07-15) — EXECUTE would fail at push; they
  // remain selectable as reference repos
  const [repoHasToken, setRepoHasToken] = useState({});
  const repoNamesKey = (dr.draft?.cfg.repos || []).map((r) => r.name).join(",");
  useEffect(() => {
    const names = repoNamesKey ? repoNamesKey.split(",").filter(Boolean) : [];
    if (!names.length) { setRepoHasToken({}); return; }
    const q = names.map((n) => `repo:${n}:token`).join(",");
    get(`/secrets-check?conn=${encodeURIComponent(q)}`)
      .then((r) => setRepoHasToken(Object.fromEntries(
        names.map((n) => [n, !!r.conn[`repo:${n}:token`]?.present]))))
      .catch(() => setRepoHasToken({}));
  }, [repoNamesKey]);
  const [oauthFor, setOauthFor] = useState(null);
  // skill store catalog (v1): store-listed when Gitea is up, bundled
  // fallback otherwise — `store` says which (and where to edit)
  const [skillsCatalog, setSkillsCatalog] = useState({ skills: [], store: null });
  const [skillsErr, setSkillsErr] = useState(false);
  const [skillsMsg, setSkillsMsg] = useState("");
  const loadSkills = () =>
    get("/skills")
      .then((r) => { setSkillsCatalog(r); setSkillsErr(false); })
      .catch(() => setSkillsErr(true));
  useEffect(() => { loadSkills(); }, []);
  const [addSkill, setAddSkill] = useState(false);
  const [addDev, setAddDev] = useState(false);
  const [renameFor, setRenameFor] = useState(null);
  const [renameBusy, setRenameBusy] = useState(false);
  const [renameErr, setRenameErr] = useState("");
  const [testResult, setTestResult] = useState({});
  const [mapperMsg, setMapperMsg] = useState("");
  const [pageErr, setPageErr] = useState("");
  // PMO cards added/renamed this session stay name-editable even when their
  // name collides with a still-saved one (delete-then-re-add / mid-typing trap)
  const newPmoNames = useNewNames(dr.server?.cfg.pmos, dr.draft?.cfg.pmos);

  const loaded = dr.loaded;

  // settings-style navigation: one section per view — switching sections
  // starts at the top of the pane
  useEffect(() => {
    document.querySelector("main")?.scrollTo({ top: 0 });
  }, [section]);

  if (!loaded) return <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading…{loadErr}</p>;

  const cfg = dr.draft.cfg;
  const setField = dr.setField;
  // stored secrets are keyed by instance name — renaming a saved instance
  // would orphan them, so the name locks once saved (remove + re-add to
  // rename; removal also deletes the instance's stored secrets). A card
  // counts as saved only when the server holds its name AND it wasn't
  // (re)added this session, so a new card can never be born frozen.
  const savedPmoNames = new Set((dr.server.cfg.pmos || []).map((p) => p.name));
  // only the LAST card carrying a name counts as the session-added one — a
  // new card duplicating a saved name never unlocks the saved card itself
  const pmoNameLocked = (name, idx) =>
    savedPmoNames.has(name) &&
    !(newPmoNames.has(name) && idx === cfg.pmos.map((p) => p.name).lastIndexOf(name));

  // flip-time danger confirms (founder decision): the scary dialog interrupts
  // at flip time, but confirming only writes the DRAFT — nothing persists
  // until the page-level Save (whose review re-highlights these rows).
  const guardedFlip = (path, value, title, body) =>
    setConfirm({
      title, body, confirmLabel: "I understand — proceed",
      action: () => { setField(path, value); setConfirm(null); },
    });

  const test = async (kind) =>
    setTestResult({ ...testResult, [kind]: await send("POST", `/connections/${kind}/test`) });
  // per-instance tests (schema v3 / M10): keyed pmo:{name} / forge:{name}
  const testPmo = async (name) =>
    setTestResult({ ...testResult,
                    [`pmo:${name}`]: await send("POST", `/connections/pmo/${name}/test`) });
  const runMapper = async () => {
    setMapperMsg("Starting…");
    try {
      const r = await send("POST", "/relations-mapper/run");
      setMapperMsg(`✓ dispatched ${r.run_id} — watch it on the Runs page`);
    } catch (e) {
      setMapperMsg(`✗ ${String(e.message || e)}`);
    }
  };

  const rm = cfg.relations_mapper || { enabled: false, interval_minutes: 60, dev_type: null };
  const serverRm = dr.server.cfg.relations_mapper || {};
  const mapperDirty = dr.diff.some((x) => x.path.startsWith("cfg.relations_mapper"));

  // harness-mismatch advisory for an assignment row (replaces the old
  // blocking dialog and its cancelAction special case)
  const argsAdvisory = (mt) => {
    const a = dr.draft.assignments[mt] || {};
    const sa = dr.server.assignments[mt] || {};
    if (!a.extra_cli_args || a.dev_type === sa.dev_type) return null;
    const oldH = (dr.server.devTypes[sa.dev_type] || dr.draft.devTypes[sa.dev_type])?.harness_template;
    const newH = dr.draft.devTypes[a.dev_type]?.harness_template;
    if (!oldH || !newH || oldH === newH) return null;
    return { oldH, newH, newDt: a.dev_type };
  };


  return (
    <div className="space-y-5">
      <PageHeader title="Configuration"
        subtitle="One section at a time — drafted edits apply on Save, wherever you made them" />
      {pageErr && <p className="text-sm text-red-600 dark:text-red-400">✗ {pageErr}</p>}

      {/* mobile section switcher (sidebar sub-nav is expanded-drawer-only) */}
      <div className="sticky top-0 z-20 -mx-4 flex gap-1.5 overflow-x-auto bg-surface/90 px-4 py-2 backdrop-blur dark:bg-surface-dark/90 lg:hidden">
        {CONFIG_SECTIONS.map((s) => (
          <a key={s.id} href={`#/config/${s.id}`}
            className={`shrink-0 rounded-full border px-3 py-1 text-xs font-medium ${
              section === s.id
                ? "border-accent-300 bg-accent-50 font-semibold text-accent-800 dark:border-accent-800 dark:bg-accent-950/60 dark:text-accent-200"
                : "border-neutral-200 bg-surface-raised text-neutral-600 dark:border-neutral-800 dark:bg-surface-raised-dark dark:text-neutral-300"
            }`}>
            {s.label}
          </a>
        ))}
      </div>

      {section === "pmo" && (
      <Section id="pmo" title="PMO connections"
        description="The PMO teams DevCake watches, and how missions are adopted."
        help={`One instance per team. Supported: ${registry.pmo_systems.map((s) => s.display_name).join(", ")}. Instance names prefix branches and run ids (LINEAR-DEV-17).`}>
        {cfg.pmos.map((inst, idx) => {
          const tr = testResult[`pmo:${inst.name}`];
          return (
            <div key={idx} className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono text-sm font-semibold">{inst.name || "(unnamed)"}</span>
                {cfg.pmos.length > 1 && (
                  <Button kind="danger-ghost" onClick={() => {
                    const doRemove = () => {
                      newPmoNames.untrack(inst.name);
                      setField("cfg.pmos", cfg.pmos.filter((_, i) => i !== idx));
                    };
                    if (savedPmoNames.has(inst.name)) {
                      // saving the removal permanently deletes the stored
                      // secret — worth an explicit confirm (audit A21)
                      setConfirm({
                        title: `Remove PMO instance "${inst.name}"?`,
                        body: "Removing it and saving permanently deletes its stored API key; in-flight runs of this instance fail cleanly. Nothing changes until you Save.",
                        confirmLabel: "Remove from draft",
                        action: () => { doRemove(); setConfirm(null); },
                      });
                    } else doRemove();
                  }}>
                    Remove
                  </Button>
                )}
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Field label="Instance name"
                  help="Operator-chosen identity (lowercase letters/digits, ≤12, no hyphens). Uppercased, it prefixes this instance's branches and run ids. Locked once saved — stored secrets and in-flight missions key on it; remove and re-add to rename.">
                  <Input value={inst.name} disabled={pmoNameLocked(inst.name, idx)}
                  onChange={(e) => {
                    newPmoNames.rename(inst.name, e.target.value);
                    setField(`cfg.pmos.${idx}.name`, e.target.value);
                  }} />
                  {dr.errors[`cfg.pmos.${idx}.name`] && (
                    <span className="mt-1 block text-xs text-red-600 dark:text-red-400">
                      ✗ {dr.errors[`cfg.pmos.${idx}.name`]}
                    </span>
                  )}</Field>
                <Field label="Team key"
                  help="The team's short key — the prefix of its issue IDs (PRJ for PRJ-123). This instance watches only this team. Empty = instance stays idle.">
                  <Input value={inst.team_key}
                  onChange={(e) => setField(`cfg.pmos.${idx}.team_key`, e.target.value)} /></Field>
                <SecretField label="API key"
                  help="This instance's PMO API key. Stored securely on the app volume — never echoed back, never in .env."
                  refKey={`pmo:${inst.name}:api_key`} paste
                  locked={!pmoNameLocked(inst.name, idx)} />
                <RepoChips label="Repositories"
                  help="The ORDERED set of repos this instance's missions may target — only repos with a stored Access token qualify (work needs push). Click to toggle; the first selected is the default for missions without a `devcake-repo:` marker; markers must name a listed repo. Empty = every mission gets its own internal-forge repo."
                  all={cfg.repos} selected={inst.repos || []}
                  excluded={inst.reference_repos || []}
                  excludedNote="reference repo"
                  unavailable={cfg.repos.map((r) => r.name).filter((n) => !repoHasToken[n])}
                  unavailableNote="no Access token stored — usable only as a reference repo"
                  firstBadge=" · default"
                  onChange={(next) => setField(`cfg.pmos.${idx}.repos`, next)} />
                <RepoChips label="Reference repos"
                  help="Read-only consultation material (docs sources, style guides) cloned into EVERY stage's workspace alongside the mission's repository. Never a work target — markers naming one gate. Multiple supported."
                  all={cfg.repos} selected={inst.reference_repos || []}
                  excluded={inst.repos || []}
                  excludedNote="work repo"
                  onChange={(next) => setField(`cfg.pmos.${idx}.reference_repos`, next)} />
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <Button kind="ghost" onClick={() => testPmo(inst.name)}>Test connection</Button>
                <ImmediateBadge text="tests saved values" />
                {tr && (
                  <span className={`text-sm ${tr.ok ? "text-green-700 dark:text-green-400" : "text-red-600"}`}>
                    {tr.ok
                      ? `✓ team ${tr.team}: ${tr.labels}/${tr.labels_expected ?? 10} labels, ${tr.missions_visible} items visible`
                      : `✗ ${tr.error}`}
                  </span>
                )}
              </div>
            </div>
          );
        })}
        <Button kind="ghost" onClick={() => {
          const name = nextFreeName("linear", cfg.pmos, dr.server.cfg.pmos);
          newPmoNames.track(name);
          setField("cfg.pmos", [...cfg.pmos,
            { name, system: "linear",
              team_key: "", api_base: null, repos: [],
              reference_repos: [] }]);
        }}>
          + Add PMO instance
        </Button>
        <div className="divide-y divide-neutral-100 border-t border-neutral-100 dark:divide-neutral-800 dark:border-neutral-800">
          <SettingRow label="Poll interval"
            desc="Seconds between polls of each PMO for new or changed missions."
            help="Lower = faster pickup, more API calls.">
            <Input type="number" className="w-24" value={cfg.poll_interval_seconds}
              aria-label="Poll interval (seconds)"
              onChange={(e) => setField("cfg.poll_interval_seconds", Number(e.target.value))} />
          </SettingRow>
          <SettingRow label="Adoption mode"
            desc={cfg.adoption_mode === "opt_in"
              ? "opt-in — only items you label DEVCAKE are adopted."
              : "opt-out — every non-completed item in the team, backlog included."}
            help="opt-in: DevCake only adopts items you label DEVCAKE. opt-out: it adopts every non-completed issue and project in the team, including the backlog.">
            <span className={`text-xs ${cfg.adoption_mode === "opt_in" ? "font-semibold" : "text-neutral-500 dark:text-neutral-400"}`}>
              opt-in
            </span>
            <Toggle on={cfg.adoption_mode === "opt_out"} label="Adoption mode"
              onClick={() =>
                cfg.adoption_mode === "opt_in"
                  ? guardedFlip("cfg.adoption_mode", "opt_out", "Adopt the ENTIRE team?",
                      ADOPTION_COPY + "\n\n(Drafted now; applies when you Save.)")
                  : setField("cfg.adoption_mode", "opt_in")} />
            <span className={`text-xs ${cfg.adoption_mode === "opt_out" ? "font-semibold" : "text-neutral-500 dark:text-neutral-400"}`}>
              opt-out
            </span>
          </SettingRow>
        </div>
      </Section>
      )}

      {section === "dev-types" && (
      <Section id="dev-types" title="Dev Types"
        description="Agent configurations — harness, model, concurrency and credentials."
        actions={
          <>
            <ImmediateBadge text="create/delete apply immediately" />
            <Button kind="ghost" icon={Plus} onClick={() => setAddDev(true)}>
              New Dev Type
            </Button>
          </>
        }>
        <div className="grid gap-4 grid-cols-1">
          {dr.order.filter((n) => dr.draft.devTypes[n] && dr.server.devTypes[n]).map((name) => (
            <DevTypeCard key={name} name={name}
              draftDt={dr.draft.devTypes[name]}
              serverDt={dr.server.devTypes[name]}
              harnesses={harnesses}
              setField={setField}
              onCredChange={reload}
              onDelete={(nm) => {
                const hasEdits = dr.diff.some((x) => x.path.startsWith(`devTypes.${nm}.`));
                setConfirm({
                  title: `Delete dev type ${nm}?`,
                  body: "Its config file is removed; credentials under /data/secrets stay until cleaned manually."
                    + (hasEdits ? "\n\nThis card has unsaved edits — deleting discards them." : ""),
                  confirmLabel: "Delete",
                  action: async () => {
                    try { await send("DELETE", `/dev-types/${nm}`); await reload(); }
                    catch (e) { setPageErr(`dev-type delete failed: ${String(e.message || e)}`); }
                    setConfirm(null);
                  },
                });
              }}
              onOAuth={setOauthFor}
              onRename={(nm) => { setRenameErr(""); setRenameFor(nm); }}
              skillsCatalog={skillsCatalog} />
          ))}
        </div>
      </Section>
      )}

      {section === "skills" && (
      <Section id="skills" title="Skills"
        description="Reusable expertise installed into the agent session."
        help="Skill-store skills Devs can use. Select them per Dev Type in the Dev Types section."
        actions={skillsCatalog.store?.enabled && (
          <>
            <Button icon={Plus} onClick={() => setAddSkill(true)}>
              Add skill
            </Button>
            <MoreMenu label="More skill-store actions" items={[
              ...(skillsCatalog.store?.html_url ? [{
                label: "Open the store in Gitea",
                desc: "Skills live in a Git repo — edit them there directly.",
                external: true,
                onClick: () => window.open(skillsCatalog.store.html_url, "_blank", "noopener"),
              }] : []),
              {
                label: "Restore built-in skills",
                desc: "Re-adds any missing bundled skills. Never overwrites your edits.",
                onClick: async () => {
                  try {
                    await send("POST", "/skills/sync");
                    await loadSkills();
                    setSkillsMsg("✓ built-in skills restored");
                    setTimeout(() => setSkillsMsg(""), 4000);
                  } catch (e) {
                    setPageErr(`restoring built-ins failed: ${String(e.message || e)}`);
                  }
                },
              },
            ]} />
          </>
        )}>
        {skillsCatalog.store && !skillsCatalog.store.enabled && (
          <p className="mb-3 text-sm text-neutral-500 dark:text-neutral-400">
            Served from the bundled copies — set GITEA_ADMIN_PASSWORD (bundled
            Gitea) to get the editable skill-store repo.
          </p>
        )}
        {skillsMsg && (
          <p className="mb-3 text-sm text-green-700 dark:text-green-400">{skillsMsg}</p>
        )}
        {skillsCatalog.store?.enabled && !skillsCatalog.store.ok && (
          <p className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-200">
            Skill store unreachable ({skillsCatalog.store.detail}) — serving the
            bundled copies. Runs keep working; store edits are unavailable.
          </p>
        )}
        {skillsErr ? (
          <p className="text-sm text-red-700 dark:text-red-300">
            Couldn&apos;t load the skill catalog.{" "}
            <button type="button" onClick={loadSkills}
              className="font-medium underline underline-offset-2">
              Retry
            </button>
          </p>
        ) : skillsCatalog.skills.length === 0 ? (
          <p className="text-sm text-neutral-500 dark:text-neutral-400">No skills found.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[36rem] text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
                <tr>
                  <th className="py-1.5 pr-4 font-semibold">Skill</th>
                  <th className="pr-4 font-semibold">Description</th>
                  <th className="pr-2 font-semibold">Source</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {skillsCatalog.skills.map((s) => (
                  <tr key={s.name} className="border-t border-neutral-100 align-top dark:border-neutral-800">
                    <td className="whitespace-nowrap py-2.5 pr-4 font-mono text-xs font-semibold">
                      {s.name}
                    </td>
                    <td className="py-2.5 pr-4 text-neutral-500 dark:text-neutral-400">
                      {s.description || "(no description)"}
                    </td>
                    <td className="py-2.5 pr-2">
                      <span className={"inline-block whitespace-nowrap rounded px-1.5 py-0.5 text-xs "
                        + (s.source === "store"
                          ? "bg-stone-100 text-stone-700 dark:bg-neutral-800 dark:text-neutral-300"
                          : "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300")}>
                        {s.source === "store" ? "store" : "bundled"}
                      </span>
                    </td>
                    <td className="py-2.5 text-right">
                      {/* built-ins re-seed at boot — only operator skills delete */}
                      {skillsCatalog.store?.enabled && !s.builtin && (
                        <button type="button"
                          title={`Delete skill ${s.name}`}
                          aria-label={`Delete skill ${s.name}`}
                          className="rounded text-neutral-500 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-neutral-400 dark:hover:text-red-400"
                          onClick={() => setConfirm({
                            title: `Delete skill ${s.name}?`,
                            body: "Removed from the skill store. Dev Types that "
                              + "selected it keep the name (⚠) but the skill is "
                              + "skipped at dispatch until re-added.",
                            confirmLabel: "Delete",
                            action: async () => {
                              try {
                                await send("DELETE", `/skills/${encodeURIComponent(s.name)}`);
                                await loadSkills();
                              } catch (e) {
                                setPageErr(`skill delete failed: ${String(e.message || e)}`);
                              }
                              setConfirm(null);
                            },
                          })}>
                          <Trash2 className="h-4 w-4" />
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      )}

      {section === "assignments" && (
      <Section id="assignments" title="Assignments"
        description="Which Dev Type handles each mission type.">
        {dr.draft.assignments?.EXECUTE?.dev_type
          && dr.draft.assignments?.REVIEW?.dev_type
          && dr.draft.assignments.EXECUTE.dev_type === dr.draft.assignments.REVIEW.dev_type && (
          <div className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-200">
            EXECUTE and REVIEW share the same Dev Type. Independent AI review is the
            default configuration, not a hard invariant — consider assigning different
            types (and models) for review independence.
          </div>
        )}
        <div className="overflow-x-auto">
          <table className="w-full min-w-[32rem] text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
              <tr><th className="py-1">Mission type</th><th>Dev type</th>
                <th>Extra CLI args (harness-specific)
                  <Help text="Appended to the harness CLI for this mission type, e.g. --max-turns 15. Flags are harness-specific — they rarely survive a dev type change." /></th></tr>
            </thead>
            <tbody>
              {MISSION_TYPES.map((mt) => {
                const adv = argsAdvisory(mt);
                return (
                  <tr key={mt} className="border-t border-neutral-100 dark:border-neutral-800">
                    <td className="py-2 font-mono text-xs font-semibold">{mt}</td>
                    <td className="py-2 pr-3">
                      <Select value={dr.draft.assignments[mt]?.dev_type || ""}
                        onChange={(e) => setField(`assignments.${mt}.dev_type`, e.target.value)}>
                        {dr.order.map((n) => <option key={n}>{n}</option>)}
                      </Select>
                    </td>
                    <td className="py-2">
                      <Input value={dr.draft.assignments[mt]?.extra_cli_args || ""}
                        placeholder="e.g. --max-turns 15"
                        onChange={(e) => setField(`assignments.${mt}.extra_cli_args`, e.target.value)} />
                      {adv && (
                        <p className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400">
                          These args were written for the {adv.oldH} harness; {adv.newDt} uses{" "}
                          {adv.newH} — flags rarely transfer.
                          <button
                            onClick={() => setField(`assignments.${mt}.extra_cli_args`, "")}
                            className="rounded border border-amber-300 px-1.5 py-0.5 font-medium hover:bg-amber-100 dark:border-amber-700 dark:hover:bg-amber-950">
                            clear args
                          </button>
                        </p>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Section>
      )}

      {section === "prompts" && (
      <PromptsSection cfg={cfg} setField={setField}
        devTypeNames={Object.keys(dr.draft.devTypes || {})} />
      )}

      {section === "limits" && (
      <Section id="limits" title="Limits"
        description="Global concurrency and safety ceilings.">
        <div className="divide-y divide-neutral-100 dark:divide-neutral-800">
          <SettingRow label="Global max Devs"
            desc="Effective ceiling = min(global, Σ per-type caps)."
            help="Primary host-protection control. Dagu 2.10.5 cannot apply Docker CPU/memory/PID limits to Dev containers — this cap is the effective throttle; hard per-container limits are planned.">
            <Input type="number" className="w-24" value={cfg.concurrency.global_max}
              aria-label="Global max Devs"
              onChange={(e) => setField("cfg.concurrency.global_max", Number(e.target.value))} />
          </SettingRow>
          <SettingRow label="Dev run timeout"
            desc="Wall-clock limit per Dev run, in minutes."
            help="Applies while dispatched/running only — finalizing is never timeout-killed. A timed-out mission is retried up to its attempt limit.">
            <Input type="number" className="w-24" value={cfg.dev_timeout_minutes}
              aria-label="Dev run timeout (minutes)"
              onChange={(e) => setField("cfg.dev_timeout_minutes", Number(e.target.value))} />
          </SettingRow>
          <SettingRow label="Review-loop warning"
            desc="Warn after every N rejections of EXECUTE's work."
            help="When REVIEW keeps rejecting EXECUTE's work, DevCake posts a warning to the mission's activity feed every N rejections so you can intervene. Must be ≥ 1.">
            <Input type="number" className="w-24" min={1} value={cfg.review_loop_warning_every}
              aria-label="Review-loop warning every N rejections"
              onChange={(e) => setField("cfg.review_loop_warning_every", Number(e.target.value))} />
          </SettingRow>
          <SettingRow label="Service auto-restart"
            desc="Long-lived services restart unless stopped (compose-managed)."
            help='Services use restart: unless-stopped in docker-compose.yml. This panel cannot rewrite compose — set restart: "no" in the file to disable.'>
            <span className="text-sm text-neutral-500 dark:text-neutral-400">managed in compose</span>
          </SettingRow>
        </div>
      </Section>
      )}

      {section === "traffic" && (
      <Section id="traffic" title="Traffic control"
        description="The Relations Mapper. (Mission intake is the master switch in the sidebar — it applies immediately.)">
        <div className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-sm font-semibold">
              Relations Mapper
              <Help text="A Dev tasked strictly with mapping missing 'blocked by' relations between existing missions (it reads every open mission's title and description head). Proposed relations are validated by the app and appear in your PMO; delete a relation there to undo it." />
            </span>
            <div className="flex items-center gap-2">
              <ImmediateBadge text="runs with saved settings" />
              <Button kind="ghost" icon={Play} onClick={runMapper}
                disabled={!serverRm.dev_type || mapperDirty}
                title={mapperDirty
                  ? "Save your mapper changes first — Run now uses the saved settings"
                  : !serverRm.dev_type ? "Pick and save a Dev Type first" : undefined}>
                Run now
              </Button>
            </div>
          </div>
          <div className="divide-y divide-neutral-100 dark:divide-neutral-800">
            <SettingRow label="Dev Type"
              desc="Which Dev Type runs the mapper."
              help="The seeded junior-dev (a cheap, fast model) is the default — ordering judgment from titles and description heads doesn't need a heavyweight.">
              <Select className="w-44" value={rm.dev_type || ""}
                aria-label="Relations Mapper Dev Type"
                onChange={(e) => {
                  setField("cfg.relations_mapper.dev_type", e.target.value || null);
                  if (!e.target.value) setField("cfg.relations_mapper.enabled", false);
                }}>
                <option value="">(none)</option>
                {dr.order.map((n) => <option key={n} value={n}>{n}</option>)}
              </Select>
            </SettingRow>
            <SettingRow label="Interval"
              desc="Minutes between automatic passes."
              help="Cadence of the periodic service when enabled. The first automatic run happens one interval after the app starts; use Run now for an immediate pass.">
              <Input type="number" className="w-24" min="1" value={rm.interval_minutes}
                aria-label="Relations Mapper interval (minutes)"
                onChange={(e) => setField("cfg.relations_mapper.interval_minutes", Number(e.target.value))}
                onBlur={(e) => setField("cfg.relations_mapper.interval_minutes",
                  Math.max(1, Number(e.target.value) || 60))} />
            </SettingRow>
            <SettingRow label="Periodic service"
              desc={rm.enabled ? "ON — runs on the interval." : "OFF — manual only (default)."}>
              <Toggle on={rm.enabled} label="Periodic service"
                onClick={() => {
                  if (!rm.enabled && !rm.dev_type) {
                    setMapperMsg("✗ pick a Dev Type first");
                    return;
                  }
                  setField("cfg.relations_mapper.enabled", !rm.enabled);
                }} />
            </SettingRow>
          </div>
          {healthInfo?.mapper_degraded && (
            <p className="text-sm text-amber-600 dark:text-amber-400">
              ⚠ Periodic service backing off — the last 3 mapper runs failed
              ({healthInfo.mapper_degraded}). Run now still works; a successful run resumes
              the schedule.
            </p>
          )}
          {mapperMsg && (
            <p className={`text-sm ${mapperMsg.startsWith("✗") ? "text-red-600" : "text-green-700 dark:text-green-400"}`}>
              {mapperMsg}
            </p>
          )}
        </div>
      </Section>
      )}

      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()}
        onCancel={() => setConfirm(null)} />
      {oauthFor && <OAuthWizard devType={oauthFor}
        onClose={() => { setOauthFor(null); reload(); }} />}
      {addSkill && <AddSkillDialog
        onClose={() => setAddSkill(false)} onSaved={loadSkills} />}
      {addDev && <NewDevTypeDialog harnesses={harnesses}
        onClose={() => setAddDev(false)} onCreated={reload} />}
      <PromptDialog open={!!renameFor}
        title={`Rename Dev Type "${renameFor}"`}
        label="New name" initial={renameFor || ""}
        hint="Renames immediately — config, credentials and prompt templates follow."
        confirmLabel="Rename" busy={renameBusy} error={renameErr}
        onConfirm={async (nn) => {
          if (nn === renameFor) { setRenameFor(null); return; }
          setRenameBusy(true); setRenameErr("");
          try {
            await send("POST", `/dev-types/${renameFor}/rename`, { new_name: nn });
            setRenameFor(null);
            await reload();
          } catch (e) { setRenameErr(String(e.message || e).replace(/^\d+ /, "")); }
          finally { setRenameBusy(false); }
        }}
        onCancel={() => { setRenameFor(null); setRenameErr(""); }} />
    </div>
  );
}
