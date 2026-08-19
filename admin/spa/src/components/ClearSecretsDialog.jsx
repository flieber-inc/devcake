import React, { useEffect, useMemo, useState } from "react";
import { get, send } from "../api.js";
import { CONNECTION_FIELDS } from "../lib/connectionFields.js";
import Button from "./Button.jsx";
import { ConfirmDialog, Modal } from "./Modal.jsx";

// Multi-select clear of GUI-stored secrets (Dev Types ⋯ / PMO / Repos).
// Presence-only inventory — never values. Picker → ConfirmDialog (pause
// checkbox default on — freezes new dispatch unless the operator unchecks).
// Profile snapshots and internal_forge are excluded.
//
// `context` reorders groups (and connection rows) so the page-relevant
// secrets float to the top; the full catalog is still always shown.

export const CLEAR_SECRETS_ENTRY = {
  label: "Clear secrets…",
  menuLabel: "Clear secrets",
  desc: "Pick OAuth files, model keys, and other stored secrets to delete.",
};

/** @typedef {"dev-types" | "pmo" | "repos"} ClearSecretsContext */

// Group render order per entry point — all groups still appear when non-empty.
const GROUP_ORDER = {
  "dev-types": ["harness", "credential_files", "connections"],
  pmo: ["connections", "harness", "credential_files"],
  repos: ["connections", "harness", "credential_files"],
};

function CheckRow({ id, label, desc, checked, onChange }) {
  return (
    <div className="flex items-start gap-2.5 py-1.5">
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-4 w-4 accent-accent-600"
      />
      <label htmlFor={id} className="min-w-0 cursor-pointer">
        <span className="block font-mono text-sm font-medium">{label}</span>
        {desc && (
          <span className="block text-xs text-neutral-500 dark:text-neutral-400">
            {desc}
          </span>
        )}
      </label>
    </div>
  );
}

function Group({ title, help, items, selected, setSelected, idOf, labelOf, descOf }) {
  if (!items.length) return null;
  const ids = items.map(idOf);
  const allOn = ids.every((id) => selected.has(id));
  const someOn = ids.some((id) => selected.has(id));
  const toggleAll = (on) => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const id of ids) {
        if (on) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  };
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h5 className="text-xs font-semibold uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
            {title}
          </h5>
          {help && (
            <p className="text-xs text-neutral-500 dark:text-neutral-400">{help}</p>
          )}
        </div>
        <button
          type="button"
          className="text-xs text-accent-700 underline-offset-2 hover:underline dark:text-accent-400"
          onClick={() => toggleAll(!allOn)}
        >
          {allOn ? "Deselect all" : someOn ? "Select remaining" : "Select all"}
        </button>
      </div>
      <div className="divide-y divide-neutral-100 rounded-md border border-neutral-200 px-3 py-1 dark:divide-neutral-800 dark:border-neutral-800">
        {items.map((it) => {
          const id = idOf(it);
          return (
            <CheckRow
              key={id}
              id={domId(id)}
              label={labelOf(it)}
              desc={descOf?.(it)}
              checked={selected.has(id)}
              onChange={(on) => {
                setSelected((prev) => {
                  const next = new Set(prev);
                  if (on) next.add(id);
                  else next.delete(id);
                  return next;
                });
              }}
            />
          );
        })}
      </div>
    </div>
  );
}

// Operator-facing labels over secrets.CONNECTION_FIELDS vocabulary
// (pinned via connectionFields.js / spa-contracts).
const _CONN_FIELD_LABELS = {
  api_key: "API key",
  token: "Access token",
  token_ro: "Read-only token",
  reviewer_token: "Reviewer token",
};
const CONN_FIELD_LABEL = Object.fromEntries(
  [...new Set(Object.values(CONNECTION_FIELDS).flat())].map(
    (f) => [f, _CONN_FIELD_LABELS[f] || f]));

// Internal selection keys use \0 (collision-proof for any printable filename).
// DOM id/htmlFor cannot use NUL — encode only for attributes (Fable PR #54).
const idHarness = (it) => `h\0${it.var}`;
const idConn = (it) => `c\0${it.scope}\0${it.instance}\0${it.field}`;
const idFile = (it) => `f\0${it.dev_type}\0${it.filename}`;
const domId = (id) => `clear-sec-${encodeURIComponent(id)}`;

function allIds(inv) {
  const ids = [];
  for (const it of inv.harness || []) ids.push(idHarness(it));
  for (const it of inv.connections || []) ids.push(idConn(it));
  for (const it of inv.credential_files || []) ids.push(idFile(it));
  return ids;
}

/** Prefer connection rows matching the entry-point scope (pmo / repo). */
function orderConnections(connections, context) {
  const list = [...(connections || [])];
  if (context === "pmo") {
    list.sort((a, b) => {
      const ap = a.scope === "pmo" ? 0 : 1;
      const bp = b.scope === "pmo" ? 0 : 1;
      return ap - bp || a.instance.localeCompare(b.instance)
        || a.field.localeCompare(b.field);
    });
  } else if (context === "repos") {
    list.sort((a, b) => {
      const ap = a.scope === "repo" ? 0 : 1;
      const bp = b.scope === "repo" ? 0 : 1;
      return ap - bp || a.instance.localeCompare(b.instance)
        || a.field.localeCompare(b.field);
    });
  }
  return list;
}

/** Build API body fragments from the structured selection set + inventory. */
function selectionToBody(selected, inv) {
  const harness = [];
  const connections = [];
  const credential_files = [];
  for (const it of inv.harness || []) {
    if (selected.has(idHarness(it))) harness.push(it.var);
  }
  for (const it of inv.connections || []) {
    if (selected.has(idConn(it))) {
      connections.push({
        scope: it.scope, instance: it.instance, field: it.field,
      });
    }
  }
  for (const it of inv.credential_files || []) {
    if (selected.has(idFile(it))) {
      credential_files.push({
        dev_type: it.dev_type, filename: it.filename,
      });
    }
  }
  return { harness, connections, credential_files };
}

/**
 * @param {{ onClose: () => void, onCleared?: (r: object) => void | Promise<void>,
 *           context?: ClearSecretsContext }} props
 */
export default function ClearSecretsDialog({ onClose, onCleared, context = "dev-types" }) {
  const [inv, setInv] = useState(null);
  const [loadErr, setLoadErr] = useState("");
  const [selected, setSelected] = useState(() => new Set());
  const [confirming, setConfirming] = useState(false);
  const [pauseIntake, setPauseIntake] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    get("/secrets/inventory")
      .then((data) => {
        setInv(data);
        setSelected(new Set());
      })
      .catch((e) => setLoadErr(String(e.message || e)));
  }, []);

  const ordered = useMemo(() => {
    if (!inv) return null;
    return {
      harness: [...(inv.harness || [])],
      credential_files: [...(inv.credential_files || [])],
      connections: orderConnections(inv.connections, context),
    };
  }, [inv, context]);

  const totalPresent = useMemo(() => {
    if (!ordered) return 0;
    return ordered.harness.length
      + ordered.connections.length
      + ordered.credential_files.length;
  }, [ordered]);

  const allItemIds = useMemo(() => (ordered ? allIds(ordered) : []), [ordered]);
  const nSelected = selected.size;
  const allSelected = totalPresent > 0 && allItemIds.every((id) => selected.has(id));
  const someSelected = nSelected > 0 && !allSelected;

  const toggleMaster = () => {
    if (allSelected) setSelected(new Set());
    else setSelected(new Set(allItemIds));
  };

  const summary = useMemo(() => {
    if (!ordered) return { harness: 0, connections: 0, files: 0 };
    let harness = 0, connections = 0, files = 0;
    for (const it of ordered.harness) if (selected.has(idHarness(it))) harness += 1;
    for (const it of ordered.connections) if (selected.has(idConn(it))) connections += 1;
    for (const it of ordered.credential_files) if (selected.has(idFile(it))) files += 1;
    return { harness, connections, files };
  }, [ordered, selected]);

  const openConfirm = () => {
    if (!nSelected || busy) return;
    setPauseIntake(true); // default on every confirm open — intake off unless unchecked
    setErr("");
    setConfirming(true);
  };

  const doClear = async () => {
    if (!inv || !nSelected || busy) return;
    setBusy(true);
    setErr("");
    try {
      const body = {
        ...selectionToBody(selected, inv),
        pause_intake: pauseIntake,
      };
      const result = await send("POST", "/secrets/clear", body);
      await onCleared?.(result);
      setConfirming(false);
      onClose();
    } catch (e) {
      setErr(String(e.message || e).replace(/^\d+ /, ""));
    } finally {
      setBusy(false);
    }
  };

  const groupDefs = {
    harness: {
      title: "Model & harness keys",
      help: "API keys and secret env vars delivered to Dev runs.",
      items: ordered?.harness || [],
      idOf: idHarness,
      labelOf: (it) => it.var,
      descOf: () => "harness secret store",
    },
    credential_files: {
      title: "OAuth & credential files",
      help: "Device-code logins and uploaded credential files per Dev Type.",
      items: ordered?.credential_files || [],
      idOf: idFile,
      labelOf: (it) => `${it.dev_type}/${it.filename}`,
      descOf: (it) => `Dev Type ${it.dev_type}`,
    },
    connections: {
      title: "Connection secrets",
      help: context === "pmo"
        ? "PMO API keys first, then forge tokens."
        : context === "repos"
          ? "Forge tokens first, then PMO API keys."
          : "PMO API keys and forge tokens — also clearable from their own cards.",
      items: ordered?.connections || [],
      idOf: idConn,
      labelOf: (it) =>
        `${it.scope}:${it.instance} · ${CONN_FIELD_LABEL[it.field] || it.field}`,
      descOf: (it) => `${it.scope} connection`,
    },
  };

  const groupOrder = GROUP_ORDER[context] || GROUP_ORDER["dev-types"];

  // Confirm open: only ConfirmDialog (no Esc dismiss). Picker underneath is
  // still mounted so Cancel returns to selection without losing checks.
  if (confirming) {
    const bits = [];
    if (summary.harness) bits.push(`${summary.harness} model/harness key${summary.harness === 1 ? "" : "s"}`);
    if (summary.files) bits.push(`${summary.files} credential file${summary.files === 1 ? "" : "s"}`);
    if (summary.connections) bits.push(`${summary.connections} connection secret${summary.connections === 1 ? "" : "s"}`);
    return (
      <ConfirmDialog
        open
        title={`Delete ${nSelected} selected secret${nSelected === 1 ? "" : "s"}?`}
        body={
          `This permanently removes: ${bits.join(", ") || "the selected secrets"}.\n\n`
          + "Dev credentials: in-flight Devs keep what they already received; "
          + "only newly spawned Devs see the change.\n"
          + "Connection secrets: the app drops them immediately — live polls "
          + "and tests will fail until reset."
        }
        confirmLabel="Delete"
        busy={busy}
        error={err}
        onConfirm={doClear}
        onCancel={() => { if (!busy) { setConfirming(false); setErr(""); } }}
      >
        <div className="mb-1 rounded-md border border-neutral-200 px-3 py-2 dark:border-neutral-800">
          <CheckRow
            id="clear-sec-pause-intake"
            label="Turn off mission intake after this"
            desc="Freezes new mission dispatch so the poller does not launch Devs that will fail without credentials. Resume from the sidebar when ready. In-flight runs still finish."
            checked={pauseIntake}
            onChange={setPauseIntake}
          />
        </div>
      </ConfirmDialog>
    );
  }

  return (
    <Modal className="max-w-xl" onClose={busy ? undefined : onClose}>
      <h4 className="mb-1 text-base font-semibold tracking-tight">Clear secrets</h4>
      <p className="mb-3 text-sm text-neutral-500 dark:text-neutral-400">
        Deletes the selected values from the secret store permanently.
        Context-relevant secrets are listed first; every stored secret is still shown.
      </p>
      <ul className="mb-4 list-disc space-y-1 pl-5 text-sm text-neutral-600 dark:text-neutral-300">
        <li>
          <strong className="font-medium">Dev credentials</strong> (model keys, OAuth files):
          in-flight Devs keep what they already received; only newly spawned Devs see the change.
        </li>
        <li>
          <strong className="font-medium">Connection secrets</strong> (PMO / forge tokens):
          the app drops them immediately on clear — live polls and tests will fail until reset.
        </li>
      </ul>
      <p className="mb-4 text-xs text-neutral-500 dark:text-neutral-400">
        Profile snapshots and internal-forge mission tokens are not listed here.
      </p>

      {loadErr && (
        <p className="mb-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300">
          Could not load secrets: {loadErr}
        </p>
      )}

      {!loadErr && !inv && (
        <p className="text-sm text-neutral-500">Loading stored secrets…</p>
      )}

      {inv && totalPresent === 0 && (
        <p className="text-sm text-neutral-500 dark:text-neutral-400">
          No secrets are currently stored.
        </p>
      )}

      {inv && totalPresent > 0 && (
        <>
          <div className="mb-3 flex items-center justify-between gap-2 rounded-md border border-neutral-200 px-3 py-2 dark:border-neutral-800">
            <span className="text-sm text-neutral-600 dark:text-neutral-300">
              {nSelected} of {totalPresent} selected
            </span>
            <button
              type="button"
              className="text-sm font-medium text-accent-700 underline-offset-2 hover:underline dark:text-accent-400"
              onClick={toggleMaster}
            >
              {allSelected
                ? "Deselect all"
                : someSelected
                  ? "Select all remaining"
                  : "Select all"}
            </button>
          </div>
          <div className="max-h-[50vh] space-y-4 overflow-y-auto pr-1">
            {groupOrder.map((key) => {
              const g = groupDefs[key];
              return (
                <Group
                  key={key}
                  title={g.title}
                  help={g.help}
                  items={g.items}
                  selected={selected}
                  setSelected={setSelected}
                  idOf={g.idOf}
                  labelOf={g.labelOf}
                  descOf={g.descOf}
                />
              );
            })}
          </div>
        </>
      )}

      <div className="mt-5 flex justify-end gap-2">
        <Button kind="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
        <Button
          kind="danger"
          disabled={busy || nSelected === 0}
          onClick={openConfirm}
        >
          {nSelected
            ? `Delete ${nSelected} secret${nSelected === 1 ? "" : "s"}…`
            : "Delete selected…"}
        </Button>
      </div>
    </Modal>
  );
}
