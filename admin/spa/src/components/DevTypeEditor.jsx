import React, { useState } from "react";
import { KeyRound, Upload } from "lucide-react";
import { send } from "../api.js";
import { Field, Help, ListTextarea, SecretField, Input, Select } from "./Field.jsx";
import InstantZone from "./InstantZone.jsx";
import Button from "./Button.jsx";
import { Modal } from "./Modal.jsx";
import SkillModeChips from "./SkillModeChips.jsx";
import DevTypeAvatar, { useCredsReady } from "./DevTypeAvatar.jsx";

// Dev Type editor modal, split out of DevTypesSection (2026-08-02, with the
// roster-table conversion — the section was the SPA's largest component).
// Behavior-identical move.

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

// Fully controlled: the editor renders and edits the shared draft — closing
// keeps the edits, the page-level Save applies them (draft semantics are
// untouchable). Only OAuth / upload / secret values act immediately.
export default function DevTypeEditor({ name, draftDt, serverDt, harnesses, setField, onOAuth, onCredChange, skillsCatalog, catalogErr, editCount, onClose }) {
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
            Advanced
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
