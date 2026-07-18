import React, { useEffect, useState } from "react";
import { Plus } from "lucide-react";
import { get, send } from "../api.js";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";
import { relTime } from "../lib/format.js";
import Button from "./Button.jsx";
import { Section } from "./Card.jsx";
import { Select } from "./Field.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import InstantZone from "./InstantZone.jsx";
import { ConfirmDialog, PromptDialog } from "./Modal.jsx";
import MoreMenu from "./MoreMenu.jsx";
import SettingRow from "./SettingRow.jsx";

// Config profiles (ADR-0013): named snapshots of the runtime settings AND
// secret values, applied as a whole. Everything here is Instant — a profile
// carries secret values, which by design never enter the client-side draft;
// destructive safety comes from ConfirmDialog previews, not the draft regime.
// The UI deals in presence and counts only — a secret value never reaches
// this component.

const APPLY_BODY = (name) =>
  `Replaces the runtime settings this profile contains: PMO connections, ` +
  `repositories, Dev Types, prompt templates and active selections, limits — ` +
  `and every stored secret value when the profile carries secrets. Current ` +
  `settings are not kept anywhere unless you saved them as a profile first. ` +
  `Run history, the skill store, internal repos, and .env values are ` +
  `untouched. Blocked while runs are active.`;

function countLine(counts) {
  const bits = [];
  if (counts.pmos) bits.push(`${counts.pmos} PMO${counts.pmos > 1 ? "s" : ""}`);
  if (counts.repos) bits.push(`${counts.repos} repo${counts.repos > 1 ? "s" : ""}`);
  if (counts.dev_types) bits.push(`${counts.dev_types} Dev Type${counts.dev_types > 1 ? "s" : ""}`);
  if (counts.prompt_templates) bits.push(`${counts.prompt_templates} template${counts.prompt_templates > 1 ? "s" : ""}`);
  if (counts.secrets) bits.push(`${counts.secrets} secret${counts.secrets > 1 ? "s" : ""}`);
  return bits.length ? bits.join(" · ") : "empty snapshot";
}

// Compact apply-preview: the diff the backend computed — names and counts,
// never values (ADR-0011).
function DiffPreview({ diff }) {
  if (!diff) return null;
  const cfg = diff.sections?.config;
  const sec = diff.sections?.secrets;
  const rows = [];
  if (cfg) {
    if (cfg.app_changed?.length)
      rows.push(["Settings", `${cfg.app_changed.length} value${cfg.app_changed.length > 1 ? "s" : ""} change (${cfg.app_changed.slice(0, 6).join(", ")}${cfg.app_changed.length > 6 ? ", …" : ""})`]);
    const d = cfg.dev_types || {};
    const devBits = [
      d.added?.length && `+${d.added.length}`,
      d.changed?.length && `${d.changed.length} changed`,
      d.removed?.length && `removes ${d.removed.join(", ")}`,
    ].filter(Boolean);
    if (devBits.length) rows.push(["Dev Types", devBits.join(" · ")]);
    const t = cfg.prompt_templates || {};
    const tplBits = [
      t.added?.length && `+${t.added.length}`,
      t.removed?.length && `removes ${t.removed.length}`,
    ].filter(Boolean);
    if (tplBits.length) rows.push(["Prompt templates", tplBits.join(" · ")]);
  }
  if (sec) {
    const flat = (g) => (g.added?.length || 0) + (g.replaced?.length || 0);
    const dels = [...(sec.connections?.removed || []), ...(sec.harness?.removed || [])];
    const writes = flat(sec.connections || {}) + flat(sec.harness || {});
    const bits = [];
    if (writes) bits.push(`${writes} value${writes > 1 ? "s" : ""} written`);
    if (dels.length) bits.push(`deletes ${dels.join(", ")}`);
    rows.push(["Secrets", bits.length ? bits.join(" · ") : "no changes"]);
  }
  return (
    <div data-testid="apply-preview"
      className="rounded-md border border-neutral-200 bg-stone-50 px-3 py-2 text-xs dark:border-neutral-800 dark:bg-neutral-950">
      {rows.length === 0 && (
        <p className="text-neutral-500 dark:text-neutral-400">
          No differences from the current settings.
        </p>
      )}
      {rows.map(([k, v]) => (
        <p key={k} className="py-0.5">
          <span className="font-medium">{k}:</span>{" "}
          <span className="text-neutral-600 dark:text-neutral-300">{v}</span>
        </p>
      ))}
      {(diff.warnings || []).map((w) => (
        <p key={w} className="py-0.5 text-amber-600 dark:text-amber-400">⚠ {w}</p>
      ))}
    </div>
  );
}

export default function ProfilesSection() {
  const { dr, reload } = useSharedDraft();
  const [profiles, setProfiles] = useState(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [note, setNote] = useState(null);          // {kind: "ok"|"warn", text}
  const [saveAs, setSaveAs] = useState(null);      // {busy, error}
  const [overwrite, setOverwrite] = useState(null); // {name, busy, error}
  const [applyPick, setApplyPick] = useState("");
  const [applying, setApplying] = useState(null);  // {name, diff, busy, error}
  const [renaming, setRenaming] = useState(null);  // {name, busy, error}
  const [deleting, setDeleting] = useState(null);  // {name, busy, error}

  const load = () =>
    get("/profiles")
      .then((d) => { setProfiles(d.profiles); setLoadFailed(false); })
      .catch(() => setLoadFailed(true));
  useEffect(() => { load(); }, []);

  const dirty = dr.dirty;

  const doSave = async (name, ow) => {
    const setter = ow ? setOverwrite : setSaveAs;
    setter((s) => ({ ...s, busy: true, error: "" }));
    try {
      const out = await send("POST", "/profiles",
        ow ? { name, overwrite: true } : { name });
      setSaveAs(null);
      setOverwrite(null);
      setNote(out.warnings?.length
        ? { kind: "warn", text: `Saved “${out.name}” — ${out.warnings.join("; ")}` }
        : { kind: "ok", text: `Saved “${out.name}”.` });
      load();
    } catch (e) {
      if (!ow && e.status === 409) { setSaveAs(null); setOverwrite({ name }); }
      else setter((s) => ({ ...s, busy: false, error: String(e.message || e) }));
    }
  };

  const startApply = async (name) => {
    setApplying({ name, diff: null, busy: false, error: "" });
    try {
      const detail = await get(`/profiles/${encodeURIComponent(name)}`);
      setApplying((a) => (a && a.name === name ? { ...a, diff: detail.diff } : a));
    } catch (e) {
      setApplying((a) => (a && a.name === name
        ? { ...a, error: String(e.message || e) } : a));
    }
  };

  const doApply = async () => {
    const name = applying.name;
    setApplying((a) => ({ ...a, busy: true, error: "" }));
    try {
      const out = await send("POST", `/profiles/${encodeURIComponent(name)}/apply`);
      setApplying(null);
      setApplyPick("");
      await reload();                       // the whole world changed
      setNote(out.warnings?.length
        ? { kind: "warn", text: `Applied “${name}” — ${out.warnings.join("; ")}` }
        : { kind: "ok", text: `Applied “${name}”. Settings reloaded.` });
      load();
    } catch (e) {
      setApplying((a) => ({ ...a, busy: false, error: String(e.message || e) }));
    }
  };

  const doRename = async (newName) => {
    setRenaming((s) => ({ ...s, busy: true, error: "" }));
    try {
      await send("POST",
        `/profiles/${encodeURIComponent(renaming.name)}/rename`,
        { new_name: newName });
      setRenaming(null);
      load();
    } catch (e) {
      setRenaming((s) => ({ ...s, busy: false, error: String(e.message || e) }));
    }
  };

  const doDelete = async () => {
    setDeleting((s) => ({ ...s, busy: true, error: "" }));
    try {
      await send("DELETE", `/profiles/${encodeURIComponent(deleting.name)}`);
      setDeleting(null);
      load();
    } catch (e) {
      setDeleting((s) => ({ ...s, busy: false, error: String(e.message || e) }));
    }
  };

  const rows = profiles || [];

  return (
    <Section id="profiles" title="Profiles"
      description="Named snapshots of your settings and secrets — save the current setup, switch between saved ones."
      help="A profile captures the saved runtime settings (connections, repos, Dev Types, prompt templates, limits) and every stored secret value. Applying replaces the current settings with the snapshot; later edits never update a profile. Secret values are stored on the server and never shown here."
      actions={
        <>
          <ImmediateBadge text="everything here applies immediately" />
          <Button icon={Plus} onClick={() => setSaveAs({ busy: false, error: "" })}>
            Save current as profile…
          </Button>
        </>
      }>
      {note && (
        <p className={note.kind === "warn"
          ? "rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
          : "rounded-md border border-green-300 bg-green-50 px-3 py-2 text-sm text-green-700 dark:border-green-900 dark:bg-green-950/40 dark:text-green-300"}>
          {note.text}
        </p>
      )}
      {dirty && (
        <p className="text-xs text-amber-600 dark:text-amber-400">
          You have unsaved changes — snapshots contain only saved settings,
          and applying is disabled until you Save or Discard.
        </p>
      )}

      <InstantZone note="applying replaces settings immediately">
        <SettingRow label="Apply a profile"
          desc="Replace the current settings with a saved snapshot."
          help="Shows a preview of what changes before anything applies. Blocked while runs are active — pause intake and let runs finish first.">
          <Select className="w-48" value={applyPick}
            aria-label="Profile to apply"
            disabled={!rows.length}
            onChange={(e) => setApplyPick(e.target.value)}>
            <option value="">choose a profile…</option>
            {rows.filter((r) => !r.broken).map((r) => (
              <option key={r.name} value={r.name}>{r.name}</option>
            ))}
          </Select>
          <Button disabled={!applyPick || dirty}
            title={dirty
              ? "Save or discard your unsaved changes first — applying replaces the saved settings"
              : undefined}
            onClick={() => startApply(applyPick)}>
            Apply profile…
          </Button>
        </SettingRow>
      </InstantZone>

      {profiles === null && (loadFailed ? (
        <p className="text-sm text-red-700 dark:text-red-300">
          Couldn&apos;t load the profiles.{" "}
          <button type="button" onClick={load}
            className="font-medium underline underline-offset-2">
            Retry
          </button>
        </p>
      ) : (
        <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading profiles…</p>
      ))}
      {profiles !== null && rows.length === 0 && (
        <p className="text-sm text-neutral-500 dark:text-neutral-400">
          No profiles yet — save your current settings as your first profile.
        </p>
      )}
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[36rem] text-left text-sm">
            <thead>
              <tr className="border-b border-neutral-200 text-xs uppercase tracking-wide text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
                <th className="py-2 pr-3 font-medium">Profile</th>
                <th className="py-2 pr-3 font-medium">Captured</th>
                <th className="py-2 pr-3 font-medium">Last applied</th>
                <th className="py-2 font-medium"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
              {rows.map((r) => (
                <tr key={r.name}>
                  <td className="py-2.5 pr-3 align-top">
                    <span className="font-mono text-sm font-medium">{r.name}</span>
                    {r.broken && (
                      <span className="ml-2 text-xs text-red-600 dark:text-red-400">
                        unreadable snapshot
                      </span>
                    )}
                    {!r.broken && !(r.sections || []).includes("secrets") && (
                      <span className="ml-2 rounded bg-stone-100 px-1.5 py-0.5 text-[10px] font-medium text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400">
                        configs only
                      </span>
                    )}
                  </td>
                  <td className="py-2.5 pr-3 align-top text-neutral-600 dark:text-neutral-300">
                    {r.broken ? "—" : (
                      <>
                        {r.created_at ? relTime(r.created_at) : "—"}
                        <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                          {countLine(r.counts || {})}
                        </span>
                      </>
                    )}
                  </td>
                  <td className="py-2.5 pr-3 align-top text-neutral-600 dark:text-neutral-300">
                    {r.last_applied_at ? (
                      <>
                        {relTime(r.last_applied_at)}
                        <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                          {r.diverged === true ? "settings changed since"
                            : r.diverged === false ? "matches as of apply" : ""}
                        </span>
                      </>
                    ) : "—"}
                  </td>
                  <td className="py-2.5 text-right align-top">
                    <MoreMenu label={`More actions for profile ${r.name}`}
                      items={[
                        { label: "Rename",
                          desc: "The snapshot is unchanged — only the name.",
                          onClick: () => setRenaming({ name: r.name, busy: false, error: "" }) },
                        { label: "Delete", danger: true,
                          desc: "Removes the snapshot; current settings are untouched.",
                          onClick: () => setDeleting({ name: r.name, busy: false, error: "" }) },
                      ]} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <PromptDialog open={!!saveAs}
        title="Save current settings as a profile"
        label="Profile name"
        hint="Snapshots the saved runtime settings and secret values. Letters/digits/spaces/dashes/underscores, ≤64 chars."
        placeholder="e.g. staging"
        confirmLabel="Save profile"
        busy={saveAs?.busy}
        error={saveAs?.error}
        onConfirm={(name) => doSave(name, false)}
        onCancel={() => setSaveAs(null)} />

      <ConfirmDialog open={!!overwrite}
        title={`Overwrite profile “${overwrite?.name}”?`}
        body={"Its stored snapshot is replaced with the current settings. The old snapshot cannot be recovered."}
        confirmLabel="Overwrite profile"
        busy={overwrite?.busy}
        error={overwrite?.error}
        onConfirm={() => doSave(overwrite.name, true)}
        onCancel={() => setOverwrite(null)} />

      <ConfirmDialog open={!!applying}
        title={`Apply profile “${applying?.name}”?`}
        body={applying ? APPLY_BODY(applying.name) : ""}
        confirmLabel="Apply profile"
        busy={applying?.busy}
        error={applying?.error}
        onConfirm={doApply}
        onCancel={() => setApplying(null)}>
        {applying && !applying.diff && !applying.error && (
          <p className="text-xs text-neutral-500 dark:text-neutral-400">Computing preview…</p>
        )}
        {applying?.diff && <DiffPreview diff={applying.diff} />}
      </ConfirmDialog>

      <PromptDialog open={!!renaming}
        title={`Rename profile “${renaming?.name}”`}
        label="New name"
        initial={renaming?.name || ""}
        hint="The snapshot is unchanged — only the name."
        confirmLabel="Rename"
        busy={renaming?.busy}
        error={renaming?.error}
        onConfirm={doRename}
        onCancel={() => setRenaming(null)} />

      <ConfirmDialog open={!!deleting}
        title={`Delete profile “${deleting?.name}”?`}
        body="Deletes the stored snapshot, including its secret values. Your current live settings are not affected."
        confirmLabel="Delete profile"
        busy={deleting?.busy}
        error={deleting?.error}
        onConfirm={doDelete}
        onCancel={() => setDeleting(null)} />
    </Section>
  );
}
