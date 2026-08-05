import React, { useEffect, useState } from "react";
import { get, download } from "../api.js";
import Button from "./Button.jsx";
import { Select } from "./Field.jsx";
import { Modal } from "./Modal.jsx";
import Toggle from "./Toggle.jsx";

// Settings export (ADR-0013): pick a source and sections, choose how secret
// material is protected, download one bundle file. Encrypted is the default;
// plaintext is an explicit red path with an honest label on the button.

function CheckRow({ id, label, desc, checked, disabled, onChange }) {
  return (
    <div className="flex items-start gap-2.5 py-1.5">
      <input id={id} type="checkbox" checked={checked} disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 accent-accent-600" />
      <label htmlFor={id} className="min-w-0 cursor-pointer">
        <span className="block text-sm font-medium">{label}</span>
        <span className="block text-xs text-neutral-500 dark:text-neutral-400">{desc}</span>
      </label>
    </div>
  );
}

export default function ExportDialog({ profiles, onClose }) {
  const [source, setSource] = useState("current");
  const [incCfg, setIncCfg] = useState(true);
  const [incSec, setIncSec] = useState(false);
  const [incEnv, setIncEnv] = useState(false);
  const [embedSkills, setEmbedSkills] = useState(false);
  const [mode, setMode] = useState("passphrase");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [summary, setSummary] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => { get("/settings/export/summary").then(setSummary).catch(() => {}); }, []);

  const fromProfile = source !== "current";
  const needsEnc = incSec || incEnv;
  const plaintext = needsEnc && mode === "plaintext";
  const pwProblem = needsEnc && mode === "passphrase"
    ? (pw.length < 8 ? "Passphrase must be at least 8 characters."
      : pw !== pw2 ? "Passphrases don't match." : "")
    : "";
  const canSubmit = (incCfg || incSec || incEnv) && !pwProblem && !busy;

  const secretsDesc = summary
    ? `every stored secret value — ${summary.secrets.total} secret${summary.secrets.total === 1 ? "" : "s"} across ${summary.secrets.connections} connection${summary.secrets.connections === 1 ? "" : "s"} + ${summary.secrets.harness} harness key${summary.secrets.harness === 1 ? "" : "s"}`
    : "every stored secret value";

  const submit = async () => {
    setBusy(true); setErr("");
    try {
      await download("/settings/export", {
        source: fromProfile ? { profile: source } : "current",
        sections: { config: incCfg, secrets: incSec, setup_env: incEnv },
        include_skills: incCfg && !fromProfile && embedSkills,
        ...(needsEnc && {
          encryption: mode === "passphrase"
            ? { mode: "passphrase", passphrase: pw }
            : { mode: "plaintext", acknowledge_plaintext: true },
        }),
      });
      onClose();
    } catch (e) {
      setErr(String(e.message || e));
    } finally { setBusy(false); }
  };

  return (
    <Modal className="max-w-2xl" onClose={busy ? undefined : onClose}>
      <h4 className="mb-1 text-base font-semibold tracking-tight">Export settings</h4>
      <p className="mb-4 text-sm text-neutral-500 dark:text-neutral-400">
        Downloads one bundle file. Import it on another DevCake to restore
        these settings as a profile there.
      </p>

      <label className="mb-3 block text-sm">
        <span className="mb-1 block font-medium">Source</span>
        <Select className="w-64" value={source} aria-label="Export source"
          onChange={(e) => setSource(e.target.value)}>
          <option value="current">Current settings</option>
          {(profiles || []).map((p) => (
            <option key={p.name} value={p.name}>profile: {p.name}</option>
          ))}
        </Select>
      </label>

      <div className="divide-y divide-neutral-100 rounded-md border border-neutral-200 px-3 py-1 dark:divide-neutral-800 dark:border-neutral-800">
        <CheckRow id="exp-config" label="Runtime configs" checked={incCfg}
          desc="connections, repos, Dev Types, mission-type assignments, prompt templates, limits — no secret values"
          onChange={setIncCfg} />
        <CheckRow id="exp-secrets" label="Runtime secrets" checked={incSec}
          desc={secretsDesc}
          onChange={setIncSec} />
        <CheckRow id="exp-env" label="Setup values (.env)" checked={incEnv}
          disabled={fromProfile}
          desc={fromProfile
            ? "profiles never hold setup values — export them from current settings"
            : "the stack bootstrap values: admin auth, Redis, Dagu, OpenObserve, Gitea admin"}
          onChange={setIncEnv} />
      </div>

      {incCfg && !fromProfile && (
        <div className="mt-3 flex items-center justify-between gap-3">
          <div>
            <span className="block text-sm font-medium">Embed skill contents</span>
            <span className="block text-xs text-neutral-500 dark:text-neutral-400">
              Include every skill-store skill&apos;s files so another install can
              restore them{summary ? ` (${summary.skills} skills)` : ""}. Off:
              Dev Types reference skills by name only.
            </span>
          </div>
          <Toggle on={embedSkills} onClick={() => setEmbedSkills(!embedSkills)}
            label="Embed skill contents" />
        </div>
      )}

      {needsEnc && (
        <fieldset className="mt-4">
          <legend className="mb-1 text-sm font-medium">Secret protection</legend>
          <div className="space-y-2">
            <label className="flex items-start gap-2.5 text-sm">
              <input type="radio" name="enc" checked={mode === "passphrase"}
                onChange={() => setMode("passphrase")}
                className="mt-0.5 accent-accent-600" />
              <span>
                <span className="font-medium">Encrypt with a passphrase</span>{" "}
                <span className="text-neutral-500 dark:text-neutral-400">(recommended)</span>
                {mode === "passphrase" && (
                  <span className="mt-2 grid max-w-sm grid-cols-1 gap-2">
                    <input type="password" value={pw} autoComplete="new-password"
                      placeholder="passphrase (min 8 chars)"
                      aria-label="Export passphrase"
                      onChange={(e) => setPw(e.target.value)}
                      className="w-full rounded-md border border-neutral-300 bg-white px-2.5 py-1.5 text-sm focus:border-accent-400 focus:outline-none focus:ring-2 focus:ring-accent-500/50 dark:border-neutral-700 dark:bg-neutral-950" />
                    <input type="password" value={pw2} autoComplete="new-password"
                      placeholder="confirm passphrase"
                      aria-label="Confirm export passphrase"
                      onChange={(e) => setPw2(e.target.value)}
                      className="w-full rounded-md border border-neutral-300 bg-white px-2.5 py-1.5 text-sm focus:border-accent-400 focus:outline-none focus:ring-2 focus:ring-accent-500/50 dark:border-neutral-700 dark:bg-neutral-950" />
                    {pwProblem && (pw || pw2) && (
                      <span className="text-xs text-red-600 dark:text-red-400">{pwProblem}</span>
                    )}
                  </span>
                )}
              </span>
            </label>
            <label className="flex items-start gap-2.5 text-sm">
              <input type="radio" name="enc" checked={mode === "plaintext"}
                onChange={() => setMode("plaintext")}
                className="mt-0.5 accent-accent-600" />
              <span className="font-medium">Plaintext</span>
            </label>
            {plaintext && (
              <p className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
                This file will contain every selected secret in clear text —
                API keys, forge tokens, admin passwords. Anyone who reads it
                controls those accounts. Treat it like a password-manager
                export and delete it after use.
              </p>
            )}
          </div>
        </fieldset>
      )}

      {err && (
        <p className="mt-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
          {err}
        </p>
      )}
      <div className="mt-5 flex justify-end gap-2">
        <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
        <Button kind={plaintext ? "danger" : "primary"} disabled={!canSubmit}
          onClick={submit}>
          {busy ? "Working…"
            : plaintext ? "Download with plaintext secrets" : "Download bundle"}
        </Button>
      </div>
    </Modal>
  );
}
