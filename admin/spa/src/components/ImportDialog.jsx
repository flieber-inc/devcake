import React, { useState } from "react";
import { send, download } from "../api.js";
import { fileToB64 } from "../lib/files.js";
import Button from "./Button.jsx";
import { Modal } from "./Modal.jsx";

// Settings import (ADR-0013): pick a bundle file → (passphrase if encrypted)
// → preview the differences → SAVE AS A PROFILE. Importing never applies —
// applying stays the profiles flow, so there is exactly one world-swap path.
// Steps: pick → preview → done. Secret material renders as counts only.

function Warn({ children }) {
  return (
    <p className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
      {children}
    </p>
  );
}

function PreviewSummary({ preview }) {
  const s = preview.summary?.sections || {};
  const rows = [];
  if (s.config) {
    if (s.config.app_changed?.length)
      rows.push(["Settings", `${s.config.app_changed.length} value${s.config.app_changed.length > 1 ? "s" : ""} differ`]);
    const d = s.config.dev_types || {};
    const bits = [d.added?.length && `+${d.added.length}`,
      d.changed?.length && `${d.changed.length} changed`,
      d.removed?.length && `-${d.removed.length}`].filter(Boolean);
    if (bits.length) rows.push(["Dev Types", bits.join(" · ")]);
    const t = s.config.prompt_templates || {};
    const tb = [t.added?.length && `+${t.added.length}`,
      t.removed?.length && `-${t.removed.length}`].filter(Boolean);
    if (tb.length) rows.push(["Prompt templates", tb.join(" · ")]);
  }
  if (s.secrets) {
    const flat = (g) => (g.added?.length || 0) + (g.replaced?.length || 0);
    const writes = flat(s.secrets.connections || {}) + flat(s.secrets.harness || {});
    const dels = (s.secrets.connections?.removed?.length || 0)
      + (s.secrets.harness?.removed?.length || 0);
    rows.push(["Secrets", `${writes} value${writes === 1 ? "" : "s"} in the bundle`
      + (dels ? ` · ${dels} live secret${dels === 1 ? "" : "s"} absent from it` : "")]);
  }
  if (s.setup_env) rows.push(["Setup values", `${s.setup_env.keys?.length || 0} .env keys (applied via a generated file, not live)`]);
  if (s.skills) rows.push(["Embedded skills", (s.skills.embedded || []).join(", ")]);
  return (
    <div className="rounded-md border border-neutral-200 bg-stone-50 px-3 py-2 text-xs dark:border-neutral-800 dark:bg-neutral-950">
      <p className="pb-1 text-neutral-500 dark:text-neutral-400">
        Captured {preview.created_at ? new Date(preview.created_at).toLocaleString() : "—"}
        {preview.devcake_tag ? ` · devcake ${preview.devcake_tag}` : ""}
        {preview.plaintext_secrets ? " · PLAINTEXT secrets" : ""}
      </p>
      {rows.length === 0 && (
        <p className="text-neutral-500 dark:text-neutral-400">No differences from the current settings.</p>
      )}
      {rows.map(([k, v]) => (
        <p key={k} className="py-0.5">
          <span className="font-medium">{k}:</span>{" "}
          <span className="text-neutral-600 dark:text-neutral-300">{v}</span>
        </p>
      ))}
      {(preview.warnings || []).map((w) => (
        <p key={w} className="py-0.5 text-amber-600 dark:text-amber-400">⚠ {w}</p>
      ))}
    </div>
  );
}

export default function ImportDialog({ onClose, onImported }) {
  const [contentB64, setContentB64] = useState(null);
  const [fileName, setFileName] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [needsPw, setNeedsPw] = useState(false);
  const [preview, setPreview] = useState(null);
  const [profileName, setProfileName] = useState("");
  const [importSkills, setImportSkills] = useState(true);
  const [askOverwrite, setAskOverwrite] = useState(false);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const runPreview = async (b64, pw) => {
    setBusy(true); setErr("");
    try {
      const out = await send("POST", "/settings/import/preview",
        pw ? { content_b64: b64, passphrase: pw } : { content_b64: b64 });
      if (out.needs_passphrase) { setNeedsPw(true); setPreview(null); }
      else { setNeedsPw(false); setPreview(out); }
    } catch (e) {
      setErr(String(e.message || e));
      setPreview(null);
    } finally { setBusy(false); }
  };

  const onPick = async (file) => {
    if (!file) return;
    setFileName(file.name);
    setPassphrase(""); setPreview(null); setErr(""); setAskOverwrite(false);
    setProfileName(file.name.replace(/\.(yaml|yml)$/i, "").replace(/[^A-Za-z0-9 _-]/g, "-").slice(0, 64));
    const b64 = await fileToB64(file);
    setContentB64(b64);
    await runPreview(b64, "");
  };

  const doImport = async (overwrite) => {
    setBusy(true); setErr("");
    try {
      const out = await send("POST", "/settings/import", {
        content_b64: contentB64,
        ...(passphrase && { passphrase }),
        save_as: profileName.trim(),
        overwrite,
        import_skills: preview?.has_skills ? importSkills : false,
      });
      setResult(out);
      setAskOverwrite(false);
      onImported?.();
    } catch (e) {
      setAskOverwrite(e.status === 409);
      setErr(String(e.message || e).replace(/^\d+ /, ""));
    } finally { setBusy(false); }
  };

  const downloadEnv = async () => {
    setErr("");
    try {
      await download("/settings/import/env", {
        content_b64: contentB64, ...(passphrase && { passphrase }),
      });
    } catch (e) { setErr(String(e.message || e)); }
  };

  return (
    <Modal className="max-w-2xl" ariaLabel="Import settings" onClose={busy ? undefined : onClose}>
      <h4 className="mb-1 text-base font-semibold tracking-tight">Import settings</h4>
      <p className="mb-4 text-sm text-neutral-500 dark:text-neutral-400">
        Upload a bundle file. It lands as a saved profile — nothing applies
        until you apply that profile yourself.
      </p>

      {!result && (
        <div className="space-y-3">
          <div className="flex min-w-0 flex-wrap items-center gap-3">
            <input id="import-bundle-file" type="file" accept=".yaml,.yml"
              className="hidden"
              onChange={(e) => onPick(e.target.files?.[0])} />
            <label htmlFor="import-bundle-file"
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium transition hover:bg-neutral-50 dark:border-neutral-700 dark:hover:bg-neutral-800">
              Choose bundle file…
            </label>
            {fileName && <span className="min-w-0 break-all font-mono text-xs text-neutral-500 dark:text-neutral-400">{fileName}</span>}
          </div>

          {needsPw && (
            <div className="flex max-w-sm flex-col gap-2 sm:flex-row sm:items-center">
              <input type="password" value={passphrase}
                placeholder="bundle passphrase"
                aria-label="Bundle passphrase"
                onChange={(e) => setPassphrase(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && passphrase && runPreview(contentB64, passphrase)}
                className="w-full rounded-md border border-neutral-300 bg-white px-2.5 py-1.5 text-sm focus:border-accent-400 focus:outline-none focus:ring-2 focus:ring-accent-500/50 dark:border-neutral-700 dark:bg-neutral-950" />
              <Button size="sm" disabled={!passphrase || busy}
                onClick={() => runPreview(contentB64, passphrase)}>
                Unlock
              </Button>
            </div>
          )}
          {needsPw && !preview && !err && (
            <p className="text-xs text-neutral-500 dark:text-neutral-400">
              This bundle is encrypted — enter its passphrase to preview it.
            </p>
          )}

          {preview && (
            <>
              <PreviewSummary preview={preview} />
              <label className="block max-w-sm text-sm">
                <span className="mb-1 block font-medium">Save as profile</span>
                <input value={profileName}
                  aria-label="Profile name for the imported bundle"
                  onChange={(e) => { setProfileName(e.target.value); setAskOverwrite(false); setErr(""); }}
                  className="w-full rounded-md border border-neutral-300 bg-white px-2.5 py-1.5 text-sm focus:border-accent-400 focus:outline-none focus:ring-2 focus:ring-accent-500/50 dark:border-neutral-700 dark:bg-neutral-950" />
                <span className="mt-1 block text-xs text-neutral-500 dark:text-neutral-400">
                  Apply it afterwards from the profiles list, with the usual preview and confirm.
                </span>
              </label>
              {preview.has_skills && (
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={importSkills}
                    onChange={(e) => setImportSkills(e.target.checked)}
                    className="h-4 w-4 accent-accent-600" />
                  Also write the embedded skills to the skill store (overwrites same-name skills)
                </label>
              )}
            </>
          )}

          {err && <Warn>{err}</Warn>}
        </div>
      )}

      {result && (
        <div className="space-y-3">
          <p className="rounded-md border border-green-300 bg-green-50 px-3 py-2 text-sm text-green-700 dark:border-green-900 dark:bg-green-950/40 dark:text-green-300">
            Saved as profile “{result.saved_as}”
            {result.skills_imported?.length
              ? ` · ${result.skills_imported.length} skill${result.skills_imported.length > 1 ? "s" : ""} written to the store`
              : ""}.
            Apply it from the profiles list when ready.
          </p>
          {(result.warnings || []).map((w) => (
            <p key={w} className="text-xs text-amber-600 dark:text-amber-400">⚠ {w}</p>
          ))}
          {result.has_setup_env && (
            <div className="rounded-md border border-neutral-200 px-3 py-2 text-sm dark:border-neutral-800">
              <p className="mb-1 font-medium">This bundle also carries setup values (.env)</p>
              <p className="mb-2 text-xs text-neutral-500 dark:text-neutral-400">
                DevCake cannot change the host&apos;s .env. To use them:
                1. download the generated file · 2. review its HOST-SPECIFIC
                lines · 3. replace <code>.env</code> at the repo root ·
                4. <code>docker compose up -d</code> · 5. delete the download.
              </p>
              <Button kind="ghost" size="sm" onClick={downloadEnv}>
                Download generated .env
              </Button>
            </div>
          )}
          {err && <Warn>{err}</Warn>}
        </div>
      )}

      <div className="mt-5 flex flex-wrap justify-end gap-2">
        <Button kind="ghost" disabled={busy} onClick={onClose}>
          {result ? "Close" : "Cancel"}
        </Button>
        {!result && (
          <Button kind={askOverwrite ? "danger" : "primary"}
            disabled={busy || !preview || !profileName.trim()}
            onClick={() => doImport(askOverwrite)}>
            {busy ? "Working…"
              : askOverwrite ? "Overwrite existing profile" : "Save as profile"}
          </Button>
        )}
      </div>
    </Modal>
  );
}
