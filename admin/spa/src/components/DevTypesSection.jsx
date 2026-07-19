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
import SelectionChips from "./SelectionChips.jsx";
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
function DevTypeCard({ name, draftDt, serverDt, harnesses, setField, onDelete, onOAuth, onRename, onCredChange, skillsCatalog, catalogErr }) {
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
        help={`Skill-store skills installed to ~/${h.skills_dir || ".claude/skills"} inside the Dev container before the agent starts. The catalog lives in the Skills section.`}
        options={(skillsCatalog?.skills || []).map((s) => ({
          name: s.name, title: s.description || undefined }))}
        selected={d.skills || []}
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

export default function DevTypesSection({ setPageErr }) {
  const { dr, reload, harnesses } = useSharedDraft();
  const [confirm, setConfirm] = useState(null); // delete confirms
  const [oauthFor, setOauthFor] = useState(null);
  const [addDev, setAddDev] = useState(false);
  const [renameFor, setRenameFor] = useState(null);
  const [renameBusy, setRenameBusy] = useState(false);
  const [renameErr, setRenameErr] = useState("");
  // skill store catalog — read here only for DevTypeCard's skill chips;
  // authoring (add/delete/restore) lives in the Skills section. On a fetch
  // failure `catalogErr` is set so DevTypeCard can render selected skills as
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

  return (
    <>
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
              skillsCatalog={skillsCatalog} catalogErr={catalogErr} />
          ))}
        </div>
      </Section>

      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()}
        onCancel={() => setConfirm(null)} />
      {oauthFor && <OAuthWizard devType={oauthFor}
        onClose={() => { setOauthFor(null); reload(); }} />}
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
    </>
  );
}
