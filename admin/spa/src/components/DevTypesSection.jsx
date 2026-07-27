import React, { useEffect, useState } from "react";
import { Plus, KeyRound, Upload } from "lucide-react";
import { get, send } from "../api.js";
import { Section } from "./Card.jsx";
import { Field, Help, ListTextarea, SecretField, Input, Select } from "./Field.jsx";
import InstantZone from "./InstantZone.jsx";
import Button from "./Button.jsx";
import { ConfirmDialog, Modal, PromptDialog } from "./Modal.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import MoreMenu from "./MoreMenu.jsx";
import SkillModeChips from "./SkillModeChips.jsx";
import ClearSecretsDialog, { CLEAR_SECRETS_ENTRY } from "./ClearSecretsDialog.jsx";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";

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

// ── Roster grid ──────────────────────────────────────────────────────────────

// Credential readiness for the DRAFTED harness: any stored env key or any
// present credential file counts. Same rule the dispatch checklist applies.
function useCredsReady(draftDt, serverDt, h) {
  const [envSet, setEnvSet] = useState({});
  useEffect(() => {
    setEnvSet({});
    const names = (h.credential_env || []).join(",");
    if (names)
      get(`/secrets-check?harness=${encodeURIComponent(names)}`)
        .then((r) => setEnvSet(Object.fromEntries(
          Object.entries(r.harness || {}).map(([k, v]) => [k, v.present]))))
        .catch(() => {});
  }, [draftDt.harness_template]);
  const filePresent = (sf) => (serverDt.secrets_present || []).includes(sf);
  return Object.values(envSet).some(Boolean) ||
    (h.credential_files || []).some((cf) => filePresent(cf.secret_file));
}

// Monogram avatar — DESIGN.md bans emoji/illustration flourishes, so the
// letterform in the accent tint carries the identity; the dot is credential
// readiness (green = ready, amber = none configured), not liveness.
function DevTypeAvatar({ name, ready, size = "lg" }) {
  const cls = size === "lg" ? "h-16 w-16 text-2xl" : "h-10 w-10 text-base";
  const dot = size === "lg" ? "h-4 w-4" : "h-3 w-3";
  return (
    <span className="relative inline-flex">
      <span
        className={`inline-flex items-center justify-center rounded-full bg-accent-100 font-mono font-semibold uppercase text-accent-800 dark:bg-accent-950 dark:text-accent-200 ${cls}`}
        aria-hidden
      >
        {name.slice(0, 1)}
      </span>
      <span
        className={`absolute -bottom-0.5 -right-0.5 rounded-full border-2 border-surface-raised dark:border-surface-raised-dark ${dot} ${
          ready ? "bg-green-500" : "bg-amber-500"}`}
        title={ready ? "credentials ready" : "no credentials configured"}
      >
        <span className="sr-only">{ready ? "credentials ready" : "no credentials configured"}</span>
      </span>
    </span>
  );
}

// Compact roster tile: identity + harness/model at a glance. The whole tile
// opens the editor; Rename/Delete stay behind the ⋯ per the action hierarchy.
function DevTypeTile({ name, draftDt, serverDt, harnesses, edited, onEdit, onRename, onDelete }) {
  const d = draftDt;
  const h = harnesses[d.harness_template] || {};
  const ready = useCredsReady(d, serverDt, h);
  return (
    <div className="relative rounded-card border border-neutral-200 transition hover:border-neutral-300 hover:shadow-card dark:border-neutral-800 dark:hover:border-neutral-600">
      <div className="absolute right-2 top-2 z-10">
        <MoreMenu label={`More actions for ${name}`} items={[
          { label: "Rename",
            desc: "Config, credentials and prompt templates follow the new name.",
            onClick: () => onRename(name) },
          { label: "Delete Dev Type", danger: true,
            desc: "Removes its config and this Dev Type's credential files; shared model keys and connection secrets stay.",
            onClick: () => onDelete(name) },
        ]} />
      </div>
      <button type="button" onClick={() => onEdit(name)}
        aria-label={`Edit dev type ${name}`}
        className="flex w-full flex-col rounded-card p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60">
        <span className="flex w-full justify-center py-5">
          <DevTypeAvatar name={name} ready={ready} />
        </span>
        <span className="block w-full">
          <span className="block truncate font-mono text-sm font-semibold">{name}</span>
          <span className="mt-0.5 block truncate text-xs text-neutral-500 dark:text-neutral-400">
            {d.harness_template} · {d.model || "default model"}
          </span>
          <span className={`mt-1 block text-xs font-medium text-amber-600 dark:text-amber-400 ${
            edited ? "" : "invisible"}`}>
            unsaved changes
          </span>
        </span>
      </button>
    </div>
  );
}

function NewDevTypeTile({ onClick }) {
  return (
    <button type="button" onClick={onClick}
      className="flex min-h-44 flex-col items-center justify-center gap-2 rounded-card border-2 border-dashed border-neutral-300 p-4 text-sm font-medium text-neutral-600 transition hover:border-accent-400 hover:bg-accent-50/40 hover:text-accent-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:border-neutral-700 dark:text-neutral-300 dark:hover:border-accent-700 dark:hover:bg-accent-950/20 dark:hover:text-accent-200">
      <Plus size={20} aria-hidden />
      New Dev Type
    </button>
  );
}

// ── Editor modal ─────────────────────────────────────────────────────────────

// Fully controlled: the editor renders and edits the shared draft — closing
// keeps the edits, the page-level Save applies them (draft semantics are
// untouchable). Only OAuth / upload / secret values act immediately.
function DevTypeEditor({ name, draftDt, serverDt, harnesses, setField, onOAuth, onCredChange, skillsCatalog, catalogErr, editCount, onClose }) {
  const d = draftDt;
  const set = (k, v) => setField(`devTypes.${name}.${k}`, v);
  const h = harnesses[d.harness_template] || {};   // registry info for the DRAFTED harness
  const pending = d.harness_template !== serverDt.harness_template;   // unsaved switch
  const ready = useCredsReady(d, serverDt, h);
  const filePresent = (sf) => (serverDt.secrets_present || []).includes(sf);
  return (
    <Modal className="max-w-2xl" onClose={onClose}>
      <div className="mb-4 flex items-center gap-3">
        <DevTypeAvatar name={name} ready={ready} size="sm" />
        <div className="min-w-0">
          <h4 className="truncate font-mono text-base font-semibold tracking-tight">{name}</h4>
          <p className="truncate text-xs text-neutral-500 dark:text-neutral-400">
            {d.harness_template} · {d.model || "default model"} ·{" "}
            <span className={ready
              ? "text-green-700 dark:text-green-400"
              : "text-amber-600 dark:text-amber-400"}>
              {ready ? "credentials ready" : "no credentials"}
            </span>
          </p>
        </div>
      </div>
      <div className="max-h-[62vh] space-y-3 overflow-y-auto pr-1">
        {/* Buzz-style progressive disclosure: the first view is harness, model
            and skills — everything operational (credentials, concurrency, MCP
            plumbing) waits behind Advanced. Credential readiness stays visible
            in the header and the Advanced summary so a broken state is never
            hidden by the fold. */}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Harness template"
            help="Which coding agent this Dev runs: claude-code (Claude Code), grok-build (Grok Build) or codex (Codex). Authoritative — the Docker image and credential requirements under Advanced follow it automatically on Save.">
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
        </div>
        {pending && (
          <p className="text-xs text-amber-600 dark:text-amber-400">
            ⚠ Unsaved harness change: on Save this Dev Type runs {h.docker_image} and
            needs the credentials under Advanced. Files under /data/secrets/{name}/
            for the old harness are kept but unused.
          </p>
        )}
        <SkillModeChips
          help={`Domain skills from the Skills section, installed to ~/${h.skills_dir || ".claude/skills"} before the agent starts. Available = consult-optional (description match). Required = same install plus a soft-force “must consult” line in the prompt (not kernel-enforced). Skills are additive domain modules — not mission-step scripts.`}
          options={(skillsCatalog?.skills || []).map((s) => ({
            name: s.name, title: s.description || undefined }))}
          available={d.skills || []}
          required={d.skills_required || []}
          disabled={!h.skills_dir}
          disabledNote={`The ${d.harness_template} harness does not support skill-store skills${
            !h.skills_dir && (d.skills || []).length
              ? ` — ${d.skills.length} selected skill(s) will be skipped`
              : ""}.`}
          optionsUnavailable={catalogErr}
          emptyNote={catalogErr
            ? "skill catalog unavailable (couldn't load /skills) — selected skills shown as-is"
            : "no skills in the catalog yet — see the Skills section"}
          staleNote={catalogErr
            ? "skill catalog unavailable — cannot confirm this is in the store"
            : "not in the skill store — skipped at dispatch; click to remove"}
          onChange={({ skills, skills_required }) => {
            set("skills", skills);
            set("skills_required", skills_required);
          }} />
        <details>
          <summary className="cursor-pointer select-none rounded text-sm font-medium text-neutral-600 hover:text-neutral-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:text-neutral-300 dark:hover:text-neutral-100">
            Advanced — credentials{" "}
            <span className={ready
              ? "text-green-700 dark:text-green-400"
              : "text-amber-600 dark:text-amber-400"}>
              {ready ? "✓" : "✗"}
            </span>
            {" "}· concurrency · MCP setup…
          </summary>
          <div className="mt-3 space-y-3">
        <InstantZone className="text-xs" note="credentials store immediately">
          <div className="flex flex-wrap items-center justify-between gap-2">
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
          {/* keyed on the DRAFTED harness so switching to an OAuth-capable
              harness shows the button immediately; disabled until the switch
              is saved (the device flow runs against the SAVED harness) */}
          {h.oauth_available && (
            <Button kind="ghost" size="sm" icon={KeyRound} disabled={pending}
              title={pending
                ? "Save the harness change first — OAuth runs the saved harness's login flow"
                : undefined}
              onClick={() => !pending && onOAuth(name)}>
              Connect via OAuth…{pending ? " (save first)" : ""}
            </Button>
          )}
          {!h.oauth_available && (
            <p className="text-neutral-500 dark:text-neutral-400">
              Authenticates with a pasted key or token — no OAuth flow.
              <Help text={`${d.harness_template} has no device-code OAuth flow — paste a key/token below.${
                d.harness_template === "claude-code"
                  ? " Run `claude setup-token` locally and paste the CLAUDE_CODE_OAUTH_TOKEN, or use an ANTHROPIC_API_KEY."
                  : ""}`} />
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
            Any one ✓ is enough.
            <Help text={`Env keys pass through at dispatch; credential files are delivered securely to each run (stored 0600 under /data/secrets/${name}/).`} />
          </p>
        </InstantZone>
            <div className="sm:max-w-[10rem]">
              <Field label="Max concurrency"
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
          </div>
        </details>
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-neutral-200 pt-4 dark:border-neutral-800">
        <p className="text-xs text-neutral-500 dark:text-neutral-400">
          {editCount > 0
            ? `${editCount} unsaved change${editCount === 1 ? "" : "s"} — applied by the page-level Save.`
            : "Edits are applied by the page-level Save."}
        </p>
        <Button kind="ghost" onClick={onClose}>Close</Button>
      </div>
    </Modal>
  );
}

// "New Dev Type" dialog: name + harness picked up front — the old flow
// created "new-dev-###" instantly and made the operator rename it after.
// On success the editor opens on the new Dev Type so setup continues there.
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
      await onCreated(name.trim()); onClose();
    } catch (e) { setErr(String(e.message || e).replace(/^\d+ /, "")); }
    finally { setBusy(false); }
  };
  return (
    <Modal onClose={busy ? undefined : onClose}>
      <h4 className="mb-3 text-base font-semibold tracking-tight">New Dev Type</h4>
      <div className="space-y-3">
        <Field label="Name" hint="lowercase letters/digits/dashes — e.g. judgment">
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

export default function DevTypesSection({ setPageErr, onHealthChange }) {
  const { dr, reload, harnesses } = useSharedDraft();
  const [confirm, setConfirm] = useState(null); // delete confirms
  const [oauthFor, setOauthFor] = useState(null);
  const [editFor, setEditFor] = useState(null);
  const [addDev, setAddDev] = useState(false);
  const [clearSecrets, setClearSecrets] = useState(false);
  const [secretsEpoch, setSecretsEpoch] = useState(0);
  const [renameFor, setRenameFor] = useState(null);
  const [renameBusy, setRenameBusy] = useState(false);
  const [renameErr, setRenameErr] = useState("");
  // skill store catalog — read here only for the editor's skill chips;
  // authoring (add/delete/restore) lives in the Skills section. On a fetch
  // failure `catalogErr` is set so the editor can render selected skills as
  // "catalog unavailable" rather than as stale red click-to-remove chips
  // (audit D5 #13): a transient /skills error must not read as "these skills
  // were deleted".
  const [skillsCatalog, setSkillsCatalog] = useState({ skills: [], store: null });
  const [catalogErr, setCatalogErr] = useState(false);
  useEffect(() => {
    get("/skills")
      .then((c) => { setSkillsCatalog(c); setCatalogErr(false); })
      .catch(() => setCatalogErr(true));
  }, []);

  const setField = dr.setField;
  const names = dr.order.filter((n) => dr.draft.devTypes[n] && dr.server.devTypes[n]);
  const onDelete = (nm) => {
    const hasEdits = dr.diff.some((x) => x.path.startsWith(`devTypes.${nm}.`));
    setConfirm({
      title: `Delete dev type ${nm}?`,
      body: "Its config and credential files under /data/secrets/{name}/ are removed. "
        + "Shared model keys and PMO/forge tokens are untouched — use Clear secrets for those."
        + (hasEdits ? "\n\nThis Dev Type has unsaved edits — deleting discards them." : ""),
      confirmLabel: "Delete",
      action: async () => {
        try { await send("DELETE", `/dev-types/${nm}`); await reload(); }
        catch (e) { setPageErr(`dev-type delete failed: ${String(e.message || e)}`); }
        setConfirm(null);
      },
    });
  };
  const onRename = (nm) => { setRenameErr(""); setRenameFor(nm); };
  // render-time guard, not state cleanup: rename/delete/reload can invalidate
  // editFor at any moment — the editor only mounts while the name exists
  const editing = editFor && dr.draft.devTypes[editFor] && dr.server.devTypes[editFor]
    ? editFor : null;

  return (
    <>
      <Section id="dev-types" title="Dev Types"
        description="Your agent roster — open a card to configure harness, model, skills and credentials."
        actions={
          <>
            <ImmediateBadge text="create/delete apply immediately" />
            <Button kind="ghost" icon={Plus} onClick={() => setAddDev(true)}>
              New Dev Type
            </Button>
            <MoreMenu label="More Dev Types actions" items={[
              { label: CLEAR_SECRETS_ENTRY.menuLabel, danger: true,
                desc: CLEAR_SECRETS_ENTRY.desc,
                onClick: () => setClearSecrets(true) },
            ]} />
          </>
        }>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {names.map((name) => (
            <DevTypeTile key={`${name}-${secretsEpoch}`} name={name}
              draftDt={dr.draft.devTypes[name]}
              serverDt={dr.server.devTypes[name]}
              harnesses={harnesses}
              edited={dr.diff.some((x) => x.path.startsWith(`devTypes.${name}.`))}
              onEdit={setEditFor}
              onRename={onRename}
              onDelete={onDelete} />
          ))}
          <NewDevTypeTile onClick={() => setAddDev(true)} />
        </div>
      </Section>

      {editing && (
        <DevTypeEditor key={`${editing}-${secretsEpoch}`} name={editing}
          draftDt={dr.draft.devTypes[editing]}
          serverDt={dr.server.devTypes[editing]}
          harnesses={harnesses}
          setField={setField}
          onCredChange={reload}
          onOAuth={setOauthFor}
          skillsCatalog={skillsCatalog} catalogErr={catalogErr}
          editCount={dr.diff.filter((x) => x.path.startsWith(`devTypes.${editing}.`)).length}
          onClose={() => setEditFor(null)} />
      )}

      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()}
        onCancel={() => setConfirm(null)} />
      {oauthFor && <OAuthWizard devType={oauthFor}
        onClose={() => { setOauthFor(null); reload(); }} />}
      {addDev && <NewDevTypeDialog harnesses={harnesses}
        onClose={() => setAddDev(false)}
        onCreated={async (nm) => { await reload(); setEditFor(nm); }} />}
      {clearSecrets && (
        <ClearSecretsDialog
          context="dev-types"
          onClose={() => setClearSecrets(false)}
          onCleared={async (result) => {
            try {
              await reload();
              setSecretsEpoch((e) => e + 1);
              if (result?.intake_paused) {
                onHealthChange?.((h) => ({ ...h, intake_paused: true }));
              }
            } catch (e) {
              setPageErr?.(`reload after clear secrets failed: ${String(e.message || e)}`);
            }
          }}
        />
      )}
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
    </>
  );
}
