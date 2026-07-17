import React, { useEffect, useRef, useState } from "react";
import { Play, Plus, Trash2, KeyRound, Upload } from "lucide-react";
import { get, send } from "../api.js";
import PageHeader from "../components/PageHeader.jsx";
import { Section } from "../components/Card.jsx";
import { Field, Help, SecretField, Input, Select, Textarea } from "../components/Field.jsx";
import Button from "../components/Button.jsx";
import Toggle from "../components/Toggle.jsx";
import { ConfirmDialog, Modal } from "../components/Modal.jsx";
import ImmediateBadge from "../components/ImmediateBadge.jsx";
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-[2px]">
      <div className="w-full max-w-lg rounded-card border border-neutral-200 bg-white p-6 shadow-2xl dark:border-neutral-800 dark:bg-neutral-900">
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
            <p className="text-xs text-neutral-400">
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
      </div>
    </div>
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
function DevTypeCard({ name, draftDt, serverDt, harnesses, setField, onDelete, onOAuth, onCredChange, skillsCatalog }) {
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
          <Button kind="ghost" onClick={async () => {
            const nn = window.prompt(`Rename Dev Type "${name}" to:`, name);
            if (!nn || nn === name) return;
            try { await send("POST", `/dev-types/${name}/rename`, { new_name: nn }); }
            catch (e) { window.alert(String(e.message || e)); }
            onCredChange && onCredChange();   // reload the draft
          }}>Rename</Button>
          <Button kind="danger-ghost" icon={Trash2} onClick={() => onDelete(name)}>Delete</Button>
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
      <p className="text-xs text-neutral-400">
        Identifying prompt is template-managed — edit it (or switch workflow)
        in the <a className="underline" href="#/config/prompts">Prompts
        section</a> below.
      </p>
      <Field
        label="MCP setup commands (one per line)"
        hint="⚠ Run inside the Dev container before the agent starts — arbitrary code execution by design. A failing command fails the run."
      >
        <Textarea
          rows={2}
          value={(d.mcp_setup_commands || []).join("\n")}
          onChange={(e) =>
            set("mcp_setup_commands", e.target.value.split("\n").filter(Boolean))}
        />
      </Field>
      <SelectionChips label="Skills"
        help="Skill-store skills installed to ~/.claude/skills inside the Dev container before the agent starts. The catalog lives in the Skills section below."
        options={(skillsCatalog?.skills || []).map((s) => ({
          name: s.name, title: s.description || undefined }))}
        selected={d.skills || []}
        disabled={d.harness_template !== "claude-code"}
        disabledNote={`Skills run on the claude-code harness only in this version${
          d.harness_template !== "claude-code" && (d.skills || []).length
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
      <div className="space-y-2 rounded-md bg-stone-50 p-3 text-xs dark:bg-neutral-800/50">
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
          <p className="text-neutral-400">
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
                <span className="text-neutral-400"> → {cf.path_hint}</span>
              </span>
              <UploadButton devType={name} secretFile={cf.secret_file} onDone={onCredChange} />
            </li>
          ))}
        </ul>
        <p className="text-neutral-400">
          Any one ✓ is enough — env keys pass through at dispatch; files are delivered
          per-run over the runspec channel (stored 0600 under /data/secrets/{name}/).
        </p>
      </div>
    </div>
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
      const msg = String(e.message || e);
      setAskOverwrite(msg.startsWith("409"));
      setErr(msg.replace(/^\d+ /, ""));
    } finally { setBusy(false); }
  };

  return (
    <Modal className="max-w-2xl">
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
              <Input value={name} onChange={(e) => setName(e.target.value)}
                placeholder="my-skill" />
            </Field>
            <Field label="When should the agent use it?"
              hint="this description is the trigger — be specific">
              <Input value={desc} onChange={(e) => setDesc(e.target.value)}
                placeholder="Writes release notes: use when a release is being prepared." />
            </Field>
          </div>
          <Field label="Instructions (markdown)">
            <Textarea rows={10} value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder={"# Release notes\n\nStep-by-step guidance the agent should follow…"} />
          </Field>
        </div>
      ) : (
        <div className="space-y-3">
          <Field label="Skill files"
            hint="select the skill's SKILL.md (plus any supporting files) — the name comes from its frontmatter">
            <input type="file" multiple
              className="block w-full text-sm text-neutral-500 file:mr-3 file:rounded-md file:border-0 file:bg-stone-100 file:px-3 file:py-1.5 file:text-sm file:font-medium dark:file:bg-neutral-800"
              onChange={async (e) => {
                setErr(""); setAskOverwrite(false);
                const fs = [];
                for (const f of e.target.files)
                  fs.push({ path: f.name, content_b64: await fileToB64(f) });
                setFiles(fs);
              }} />
          </Field>
          {files.length > 0 && (
            <p className="text-xs text-neutral-400">
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
          <Button kind="danger" disabled={busy} onClick={() => submit(true)}>
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

export default function ConfigPage({ section, onSectionInView }) {
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
  const loadSkills = () =>
    get("/skills").then(setSkillsCatalog).catch(() => {});
  useEffect(() => { loadSkills(); }, []);
  const [addSkill, setAddSkill] = useState(false);
  const [testResult, setTestResult] = useState({});
  const [mapperMsg, setMapperMsg] = useState("");
  const [pageErr, setPageErr] = useState("");
  // PMO cards added/renamed this session stay name-editable even when their
  // name collides with a still-saved one (delete-then-re-add / mid-typing trap)
  const newPmoNames = useNewNames(dr.server?.cfg.pmos, dr.draft?.cfg.pmos);

  const loaded = dr.loaded;

  // deep-link scroll: #/config/<section> — instant on first render, smooth
  // after. Section clicks are authoritative for the sidebar highlight during
  // the programmatic scroll (quiet period), then the scrollspy resumes.
  const scrolledOnce = useRef(false);
  const spyQuietUntil = useRef(0);
  useEffect(() => {
    if (!loaded || !section) return;
    spyQuietUntil.current = Date.now() + 1000;
    onSectionInView && onSectionInView(section);
    document.getElementById(section)?.scrollIntoView({
      behavior: scrolledOnce.current ? "smooth" : "auto",
    });
    scrolledOnce.current = true;
  }, [loaded, section]);

  // scrollspy → sidebar sub-nav highlight. Scroll-position math instead of an
  // IntersectionObserver: active = last section whose top crossed the
  // activation line, clamped to the last section at the bottom of the scroll
  // (bottom sections can never reach the top of the viewport otherwise).
  useEffect(() => {
    if (!loaded || !onSectionInView) return;
    const main = document.querySelector("main");
    if (!main) return;
    const onScroll = () => {
      if (Date.now() < spyQuietUntil.current) return;
      const atBottom = main.scrollHeight - main.scrollTop - main.clientHeight < 8;
      if (atBottom) {
        onSectionInView(CONFIG_SECTIONS[CONFIG_SECTIONS.length - 1].id);
        return;
      }
      const line = main.getBoundingClientRect().top + 96; // activation line
      let active = CONFIG_SECTIONS[0].id;
      for (const s of CONFIG_SECTIONS) {
        const el = document.getElementById(s.id);
        if (el && el.getBoundingClientRect().top <= line) active = s.id;
      }
      onSectionInView(active);
    };
    main.addEventListener("scroll", onScroll, { passive: true });
    return () => main.removeEventListener("scroll", onScroll);
  }, [loaded]);

  if (!loaded) return <p className="text-sm text-neutral-400">Loading…{loadErr}</p>;

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
        subtitle="Connections, Dev Types, assignments and limits — edits apply on Save" />
      {pageErr && <p className="text-sm text-red-600 dark:text-red-400">✗ {pageErr}</p>}

      {/* mobile section chips (sidebar sub-nav is expanded-drawer-only) */}
      <div className="sticky top-0 z-20 -mx-4 flex gap-1.5 overflow-x-auto bg-surface/90 px-4 py-2 backdrop-blur dark:bg-surface-dark/90 lg:hidden">
        {CONFIG_SECTIONS.map((s) => (
          <a key={s.id} href={`#/config/${s.id}`}
            onClick={() => document.getElementById(s.id)?.scrollIntoView({ behavior: "smooth" })}
            className="shrink-0 rounded-full border border-neutral-200 bg-surface-raised px-3 py-1 text-xs font-medium text-neutral-600 dark:border-neutral-800 dark:bg-surface-raised-dark dark:text-neutral-300">
            {s.label}
          </a>
        ))}
      </div>

      <Section id="pmo" title="PMO connections"
        description={`The PMO teams DevCake watches (one instance each), and how it adopts missions. Supported: ${registry.pmo_systems.map((s) => s.display_name).join(", ")}. Instance names prefix branches and run ids (LINEAR-DEV-17).`}>
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
        <div className="flex flex-wrap items-center gap-3">
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
          <Field label="Poll interval (s)"
            help="How often DevCake polls each PMO instance for new or changed missions. Lower = faster pickup, more API calls.">
            <Input type="number"
            value={cfg.poll_interval_seconds}
            onChange={(e) => setField("cfg.poll_interval_seconds", Number(e.target.value))} /></Field>
        </div>
        <Field label="Adoption mode"
          help="opt-in: DevCake only adopts items you label DEVCAKE. opt-out: it adopts every non-completed issue and project in the team, including the backlog.">
          <div className="flex items-center gap-3 text-sm">
            <span className={cfg.adoption_mode === "opt_in" ? "font-semibold" : "text-neutral-400"}>
              opt-in (label required)
            </span>
            <Toggle on={cfg.adoption_mode === "opt_out"} label="Adoption mode"
              onClick={() =>
                cfg.adoption_mode === "opt_in"
                  ? guardedFlip("cfg.adoption_mode", "opt_out", "Adopt the ENTIRE team?",
                      ADOPTION_COPY + "\n\n(Drafted now; applies when you Save.)")
                  : setField("cfg.adoption_mode", "opt_in")} />
            <span className={cfg.adoption_mode === "opt_out" ? "font-semibold" : "text-neutral-400"}>
              opt-out (whole team)
            </span>
          </div>
        </Field>
      </Section>

      <Section id="dev-types" title="Dev Types"
        description="Agent configurations — harness, model, concurrency and credentials."
        actions={
          <>
            <ImmediateBadge text="create/delete apply immediately" />
            <Button kind="ghost" icon={Plus}
              onClick={async () => {
                const name = `new-dev-${Date.now() % 1000}`;
                await send("POST", "/dev-types", {
                  name, harness_template: "codex", identifying_prompt: "",
                  max_concurrency: 1,
                });
                reload();
              }}>
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
              skillsCatalog={skillsCatalog} />
          ))}
        </div>
      </Section>

      <Section id="skills" title="Skills"
        description="Claude Code skills Devs can use — reusable expertise installed into the agent session. Select them per Dev Type above."
        actions={
          <>
            {skillsCatalog.store?.enabled && (
              <Button kind="ghost" icon={Plus} onClick={() => setAddSkill(true)}>
                Add skill
              </Button>
            )}
            {skillsCatalog.store?.enabled && skillsCatalog.store?.html_url && (
              <a className="text-sm underline" target="_blank" rel="noreferrer"
                href={skillsCatalog.store.html_url}>
                Edit in Gitea →
              </a>
            )}
            {skillsCatalog.store?.enabled && (
              <Button kind="ghost" onClick={async () => {
                try { await send("POST", "/skills/sync"); await loadSkills(); }
                catch (e) { setPageErr(`skill re-seed failed: ${String(e.message || e)}`); }
              }}>
                Re-seed built-ins
              </Button>
            )}
          </>
        }>
        {skillsCatalog.store && !skillsCatalog.store.enabled && (
          <p className="mb-3 text-sm text-neutral-400">
            Served from the bundled copies — set GITEA_ADMIN_PASSWORD (bundled
            Gitea) to get the editable skill-store repo.
          </p>
        )}
        {skillsCatalog.store?.enabled && !skillsCatalog.store.ok && (
          <p className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-200">
            Skill store unreachable ({skillsCatalog.store.detail}) — serving the
            bundled copies. Runs keep working; store edits are unavailable.
          </p>
        )}
        {skillsCatalog.skills.length === 0 ? (
          <p className="text-sm text-neutral-400">No skills found.</p>
        ) : (
          <div className="divide-y divide-neutral-200 dark:divide-neutral-800">
            {skillsCatalog.skills.map((s) => (
              <div key={s.name} className="flex items-baseline gap-3 py-2 text-sm">
                <span className="shrink-0 font-mono font-semibold">{s.name}</span>
                <span className="grow text-neutral-500 dark:text-neutral-400">
                  {s.description || "(no description)"}
                </span>
                <span className={"shrink-0 rounded px-1.5 py-0.5 text-xs "
                  + (s.source === "store"
                    ? "bg-stone-100 text-stone-700 dark:bg-neutral-800 dark:text-neutral-300"
                    : "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300")}>
                  {s.source === "store" ? "store" : "bundled"}
                </span>
                {/* built-ins re-seed at boot — only operator skills delete */}
                {skillsCatalog.store?.enabled && !s.builtin && (
                  <button type="button"
                    title={`Delete skill ${s.name}`}
                    className="shrink-0 text-neutral-400 hover:text-red-600 dark:hover:text-red-400"
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
              </div>
            ))}
          </div>
        )}
      </Section>

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
            <thead className="text-left text-xs uppercase tracking-wide text-neutral-400">
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

      <PromptsSection cfg={cfg} setField={setField}
        devTypeNames={Object.keys(dr.draft.devTypes || {})} />

      <Section id="limits" title="Limits"
        description="Global concurrency and safety ceilings. Dev container Docker HostConfig CPU/memory is not available in Dagu 2.10.5 — concurrency caps are the real throttle.">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label="Global max Devs" hint="Effective ceiling = min(global, Σ per-type caps). Primary host-protection control.">
            <Input type="number" value={cfg.concurrency.global_max}
              onChange={(e) => setField("cfg.concurrency.global_max", Number(e.target.value))} />
          </Field>
          <Field label="Dev run timeout (min)"
            help="Wall-clock limit per Dev run (dispatched/running only — finalizing is never timeout-killed). The mission is retried up to its attempt limit.">
            <Input type="number" value={cfg.dev_timeout_minutes}
              onChange={(e) => setField("cfg.dev_timeout_minutes", Number(e.target.value))} />
          </Field>
          <Field label="Loop warning every N rejections"
            help="When REVIEW keeps rejecting EXECUTE's work, DevCake posts a warning to the mission's activity feed every N rejections so you can intervene. Must be ≥ 1.">
            <Input type="number" min={1} value={cfg.review_loop_warning_every}
              onChange={(e) => setField("cfg.review_loop_warning_every", Number(e.target.value))} />
          </Field>
        </div>
        <div className="mt-4 rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm text-neutral-600 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-300">
          <strong className="font-medium text-neutral-800 dark:text-neutral-100">Dev container limits:</strong>{" "}
          Dagu 2.10.5 cannot apply Docker HostConfig CPU/memory/PID limits to Dev
          containers; <code className="font-mono text-xs">dagu/dags/dev-run.yaml</code>{" "}
          carries a best-effort <code className="font-mono text-xs">resources.limits</code>{" "}
          block, and concurrency caps above are the real throttle (docs/07 §7,
          hard limits are v0.1 backlog).
        </div>
        <div className="mt-4 rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2 text-sm text-neutral-600 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-300">
          <strong className="font-medium text-neutral-800 dark:text-neutral-100">Compose restart:</strong>{" "}
          long-lived services use <code className="font-mono text-xs">restart: unless-stopped</code> in
          docker-compose.yml (default on). The SPA cannot rewrite compose — set{" "}
          <code className="font-mono text-xs">restart: &quot;no&quot;</code> in the file to disable.
        </div>
      </Section>

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
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Field label="Dev Type"
              help="Which Dev Type runs the mapper. The seeded junior-dev (a cheap, fast model) is the default — ordering judgment from titles and description heads doesn't need a heavyweight.">
              <Select value={rm.dev_type || ""}
                onChange={(e) => {
                  setField("cfg.relations_mapper.dev_type", e.target.value || null);
                  if (!e.target.value) setField("cfg.relations_mapper.enabled", false);
                }}>
                <option value="">(none)</option>
                {dr.order.map((n) => <option key={n} value={n}>{n}</option>)}
              </Select>
            </Field>
            <Field label="Interval (minutes)"
              help="Cadence of the periodic service when enabled. The first automatic run happens one interval after the app starts; use Run now for an immediate pass.">
              <Input type="number" min="1" value={rm.interval_minutes}
                onChange={(e) => setField("cfg.relations_mapper.interval_minutes", Number(e.target.value))}
                onBlur={(e) => setField("cfg.relations_mapper.interval_minutes",
                  Math.max(1, Number(e.target.value) || 60))} />
            </Field>
            <Field label="Periodic service" hint="Default OFF — use Run now for one-shot passes">
              <div className="flex h-9 items-center gap-3 text-sm">
                <Toggle on={rm.enabled} label="Periodic service"
                  onClick={() => {
                    if (!rm.enabled && !rm.dev_type) {
                      setMapperMsg("✗ pick a Dev Type first");
                      return;
                    }
                    setField("cfg.relations_mapper.enabled", !rm.enabled);
                  }} />
                <span>{rm.enabled ? "ON — runs on the interval" : "OFF — manual only"}</span>
              </div>
            </Field>
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

      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()}
        onCancel={() => setConfirm(null)} />
      {oauthFor && <OAuthWizard devType={oauthFor}
        onClose={() => { setOauthFor(null); reload(); }} />}
      {addSkill && <AddSkillDialog
        onClose={() => setAddSkill(false)} onSaved={loadSkills} />}
    </div>
  );
}
