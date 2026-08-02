import React, { useEffect, useState } from "react";
import { Plus } from "lucide-react";
import { get, send } from "../api.js";
import { Section } from "./Card.jsx";
import Button from "./Button.jsx";
import { ConfirmDialog, Modal, PromptDialog } from "./Modal.jsx";
import ImmediateBadge from "./ImmediateBadge.jsx";
import MoreMenu from "./MoreMenu.jsx";
import ClearSecretsDialog, { CLEAR_SECRETS_ENTRY } from "./ClearSecretsDialog.jsx";
import DevTypeAvatar, { useCredsReady, clearCredsCache } from "./DevTypeAvatar.jsx";
import DevTypeEditor from "./DevTypeEditor.jsx";
import NewDevTypeDialog from "./NewDevTypeDialog.jsx";
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

// ── Roster table ─────────────────────────────────────────────────────────────
// Re-decision of DESIGN.md §2's roster grid (founder-directed 2026-08-02,
// bulk-scale: 12+ Dev Types made tile cards unwieldy). The invariants
// survive the form change: the whole row opens the editor, Rename/Delete
// stay behind the ⋯, the dashed "New Dev Type" row is the SOLE create
// affordance, and credential readiness is never hidden.

function DevTypeRow({ name, draftDt, serverDt, harnesses, edited, onEdit, onRename, onDelete }) {
  const d = draftDt;
  const h = harnesses[d.harness_template] || {};
  const ready = useCredsReady(d, serverDt, h);
  return (
    <tr onClick={() => onEdit(name)}
      title={`Edit dev type ${name}`}
      className="cursor-pointer border-t border-neutral-100 align-middle hover:bg-stone-50 dark:border-neutral-800 dark:hover:bg-neutral-900">
      <td className="py-2 pr-4">
        <button type="button" aria-label={`Edit dev type ${name}`}
          onClick={(e) => { e.stopPropagation(); onEdit(name); }}
          className="inline-flex items-center gap-2.5 rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60">
          <DevTypeAvatar name={name} ready={ready} size="xs" />
          <span className="font-mono text-xs font-semibold">{name}</span>
        </button>
      </td>
      <td className="py-2 pr-4 text-xs">{d.harness_template}</td>
      <td className="py-2 pr-4 text-xs text-neutral-500 dark:text-neutral-400">
        {d.model || "default model"}
      </td>
      <td className="py-2 pr-4 text-right text-xs tabular-nums">{d.max_concurrency}</td>
      <td className="py-2 pr-4 text-right text-xs tabular-nums">{(d.skills || []).length}</td>
      <td className="py-2 pr-4 whitespace-nowrap text-xs">
        {/* always informative: credential readiness (the avatar dot's meaning,
            in words) — an empty-looking "status" column read as broken */}
        <span className={ready
          ? "text-green-700 dark:text-green-400"
          : "text-amber-600 dark:text-amber-400"}>
          {ready ? "ready" : "no credentials"}
        </span>
        {edited && (
          <span className="ml-1.5 font-medium text-amber-600 dark:text-amber-400">
            · unsaved changes
          </span>
        )}
      </td>
      <td className="w-10 py-1 text-right" onClick={(e) => e.stopPropagation()}>
        <MoreMenu label={`More actions for ${name}`} items={[
          { label: "Rename",
            desc: "Config, credentials and prompt templates follow the new name.",
            onClick: () => onRename(name) },
          { label: "Delete Dev Type", danger: true,
            desc: "Removes its config and this Dev Type's credential files; shared model keys and connection secrets stay.",
            onClick: () => onDelete(name) },
        ]} />
      </td>
    </tr>
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
        description="Your agent roster — open a row to configure harness, model, skills and credentials."
        actions={
          <>
            <ImmediateBadge text="create/delete apply immediately" />
            {/* creation lives on the table's dashed row — no header twin */}
            <MoreMenu label="More Dev Types actions" items={[
              { label: CLEAR_SECRETS_ENTRY.menuLabel, danger: true,
                desc: CLEAR_SECRETS_ENTRY.desc,
                onClick: () => setClearSecrets(true) },
            ]} />
          </>
        }>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[36rem] text-left text-sm">
            <thead className="text-xs uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
              <tr>
                <th className="py-1.5 pr-4">dev type</th>
                <th className="pr-4">harness</th>
                <th className="pr-4">model</th>
                <th className="pr-4 text-right">max conc</th>
                <th className="pr-4 text-right">skills</th>
                <th className="pr-4">status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {names.map((name) => (
                <DevTypeRow key={`${name}-${secretsEpoch}`} name={name}
                  draftDt={dr.draft.devTypes[name]}
                  serverDt={dr.server.devTypes[name]}
                  harnesses={harnesses}
                  edited={dr.diff.some((x) => x.path.startsWith(`devTypes.${name}.`))}
                  onEdit={setEditFor}
                  onRename={onRename}
                  onDelete={onDelete} />
              ))}
              <tr className="border-t border-neutral-100 dark:border-neutral-800">
                <td colSpan={7} className="py-2">
                  <button type="button" onClick={() => setAddDev(true)}
                    className="flex w-full items-center justify-center gap-2 rounded-card border-2 border-dashed border-neutral-300 py-2.5 text-sm font-medium text-neutral-600 transition hover:border-accent-400 hover:bg-accent-50/40 hover:text-accent-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500/60 dark:border-neutral-700 dark:text-neutral-300 dark:hover:border-accent-700 dark:hover:bg-accent-950/20 dark:hover:text-accent-200">
                    <Plus size={16} aria-hidden />
                    New Dev Type
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
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
              clearCredsCache();          // env-key presence may have changed
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
