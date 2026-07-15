import React, { useEffect, useState } from "react";
import { Trash2 } from "lucide-react";
import { get, send } from "../api.js";
import PageHeader from "../components/PageHeader.jsx";
import { Section } from "../components/Card.jsx";
import { Field, SecretField, Input, Select } from "../components/Field.jsx";
import Button from "../components/Button.jsx";
import Toggle from "../components/Toggle.jsx";
import { ConfirmDialog } from "../components/Modal.jsx";
import ImmediateBadge from "../components/ImmediateBadge.jsx";
import { AUTO_MERGE_COPY } from "../lib/configLabels.js";
import { getRegistry, loadRegistry } from "../lib/registry.js";
import { useSharedDraft } from "../lib/ConfigDraftContext.jsx";

// Repositories page (v0.1.1 B4, founder request): the repository cards +
// merge policy lifted out of Configuration, plus the internal-forge
// inventory — repository state in one place. Edits ride the SAME unified
// draft as Configuration (DraftChrome owns Save/DirtyBar/NavGuard).

// Read-only internal-forge repos (M11, founder decision: retain-by-default +
// a manual Clear). Hidden entirely when the internal forge is disabled.
function InternalReposSection({ onClear, refreshKey }) {
  const [data, setData] = useState(null);
  const load = () => get("/internal-repos").then(setData).catch(() => setData({ repos: [] }));
  useEffect(() => { load(); }, [refreshKey]);
  if (!data || data.repos.length === 0) return null;
  return (
    <Section id="internal-forge" title="Internal forge"
      description="Repositories DevCake auto-created for missions with no configured repo. Retained until you clear them; the deliverable is already in the PMO feed.">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-left text-neutral-500">
            <tr><th className="py-1 pr-3">Mission</th><th className="pr-3">Repo</th>
              <th className="pr-3">Size</th><th className="pr-3">Open PRs</th><th></th></tr>
          </thead>
          <tbody>
            {data.repos.map((r) => (
              <tr key={r.name} className="border-t border-neutral-200 dark:border-neutral-800">
                <td className="py-1.5 pr-3 font-mono">{r.mission_key}</td>
                <td className="pr-3">
                  <a className="text-blue-600 hover:underline" href={r.html_url}
                    target="_blank" rel="noreferrer">{r.name}</a>
                </td>
                <td className="pr-3">{Math.round(r.size_kb)} KB</td>
                <td className="pr-3">{r.open_prs}</td>
                <td className="text-right">
                  <Button kind="danger-ghost" icon={Trash2}
                    onClick={() => onClear(r.name)}>Clear</Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.ui_url && (
        <a className="mt-2 inline-block text-sm text-blue-600 hover:underline"
          href={data.ui_url} target="_blank" rel="noreferrer">Open the Gitea UI →</a>
      )}
    </Section>
  );
}

export default function ReposPage() {
  const { dr, loadErr } = useSharedDraft();
  const [registry, setRegistry] = useState(getRegistry());
  useEffect(() => { loadRegistry().then(setRegistry); }, []);
  const [confirm, setConfirm] = useState(null);
  const [testResult, setTestResult] = useState({});
  const [clearErr, setClearErr] = useState("");
  const [internalRefresh, setInternalRefresh] = useState(0);

  if (!dr.loaded) {
    return <p className="text-sm text-neutral-400">Loading…{loadErr}</p>;
  }

  const cfg = dr.draft.cfg;
  const setField = dr.setField;
  // stored tokens key on the repo name — locked once saved (see ConfigPage)
  const savedRepoNames = new Set((dr.server.cfg.repos || []).map((r) => r.name));

  const guardedFlip = (path, value, title, body) =>
    setConfirm({
      title, body, confirmLabel: "I understand — proceed",
      action: () => { setField(path, value); setConfirm(null); },
    });

  const testForge = async (name) =>
    setTestResult({ ...testResult,
                    [`forge:${name}`]: await send("POST", `/connections/forge/${name}/test`) });

  return (
    <div className="space-y-5">
      <PageHeader title="Repositories"
        subtitle="Forge connections, tokens, merge policy, and the internal forge — edits apply on Save" />

      <Section id="repository" title="Repositories"
        description="Forge connections, access tokens and merge policy. Missions route to a repo via a `devcake-repo:<name>` line in their description, else the PMO instance's default repo; unrouted missions wait.">
        {cfg.repos.map((repo, idx) => {
          const tr = testResult[`forge:${repo.name}`];
          return (
            <div key={idx} className="space-y-3 rounded-card border border-neutral-200 p-4 dark:border-neutral-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono text-sm font-semibold">{repo.name || "(unnamed)"}</span>
                {cfg.repos.length > 0 && (
                  <Button kind="danger-ghost" onClick={() => {
                    const doRemove = () =>
                      setField("cfg.repos", cfg.repos.filter((_, i) => i !== idx));
                    if (savedRepoNames.has(repo.name)) {
                      setConfirm({
                        title: `Remove repository "${repo.name}"?`,
                        body: "Removing it and saving permanently deletes its stored tokens (write / read-only / reviewer); missions that used this repo gate until a human closes them out. Nothing changes until you Save.",
                        confirmLabel: "Remove from draft",
                        action: () => { doRemove(); setConfirm(null); },
                      });
                    } else doRemove();
                  }}>
                    Remove
                  </Button>
                )}
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <Field label="Repo name"
                  help="Operator-chosen identity (lowercase letters/digits, ≤12, no hyphens). Missions reference it in `devcake-repo:` markers and PMO default-repo settings. Locked once saved — stored tokens key on it; remove and re-add to rename.">
                  <Input value={repo.name} disabled={savedRepoNames.has(repo.name)}
                  onChange={(e) => setField(`cfg.repos.${idx}.name`, e.target.value)} /></Field>
                <Field label="Forge"
                  help="Where the repository lives. Selects the API DevCake uses for pull/merge requests, approvals and merges.">
                  <Select value={repo.forge}
                    onChange={(e) => setField(`cfg.repos.${idx}.forge`, e.target.value)}>
                    {registry.forges.map((f) => (
                      <option key={f.id} value={f.id}>{f.id}</option>
                    ))}
                  </Select>
                </Field>
                <Field label="Repository URL"
                  help="HTTPS URL of the repository, e.g. https://github.com/you/repo.git. Devs clone it; the app opens and merges PRs on it. Empty = repo stays idle.">
                  <Input value={repo.url}
                  onChange={(e) => setField(`cfg.repos.${idx}.url`, e.target.value)} /></Field>
                <SecretField label="Access token"
                  help="This repo's forge token (repo read/write + PR scopes). Stored securely — never echoed, never in .env."
                  refKey={`repo:${repo.name}:token`} paste
                  locked={!savedRepoNames.has(repo.name)} />
                <SecretField label="Read-only token" hint="Optional → clone-only for PLAN/REVIEW/ONBOARD"
                  help="Optional read-only token used by non-EXECUTE stages so a prompt-injected Dev can't push. Leave empty to give every stage the write token."
                  refKey={`repo:${repo.name}:token_ro`} paste
                  locked={!savedRepoNames.has(repo.name)} />
                <SecretField label="Reviewer token" hint="Optional 2nd account → formal PR approvals"
                  help="Optional second account's token. When set, REVIEW posts a formal approval from that account before merging."
                  refKey={`repo:${repo.name}:reviewer_token`} paste
                  locked={!savedRepoNames.has(repo.name)} />
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <Button kind="ghost" onClick={() => testForge(repo.name)}>Test connection</Button>
                <ImmediateBadge text="tests saved values" />
                {tr && (
                  <span className={`text-sm ${tr.ok ? "text-green-700 dark:text-green-400" : "text-red-600"}`}>
                    {tr.ok
                      ? `✓ ${tr.forge} reachable · reviewer token: ${tr.reviewer_token_configured ? "yes" : "no"}`
                      : `✗ ${tr.error}`}
                  </span>
                )}
              </div>
            </div>
          );
        })}
        <Button kind="ghost" onClick={() =>
          setField("cfg.repos", [...cfg.repos,
            { name: `repo${cfg.repos.length + 1}`, forge: "github", url: "",
              api_base: null, default_branch: "main" }])}>
          + Add repository
        </Button>
        <Field label="Auto-merge"
          help="ON: after DevCake's REVIEW step approves a PR, it merges itself (squash). OFF: DevCake stops at DEVCAKE-MERGE and waits for you to merge.">
          <div className="flex items-center gap-3 text-sm">
            <Toggle on={cfg.auto_merge} label="Auto-merge"
              onClick={() =>
                cfg.auto_merge
                  ? setField("cfg.auto_merge", false)
                  : guardedFlip("cfg.auto_merge", true, "Merge without human review?",
                      AUTO_MERGE_COPY + "\n\n(Drafted now; applies when you Save.)")} />
            <span>{cfg.auto_merge ? "ON — approved PRs merge themselves" : "OFF — every merge is yours (DEVCAKE-MERGE handoff)"}</span>
          </div>
        </Field>
        {/* dependent controls: only meaningful while auto-merge is ON (drafted value) */}
        <div className={cfg.auto_merge ? "" : "opacity-50 pointer-events-none"}
          aria-disabled={!cfg.auto_merge}>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Auto-resolve merge conflicts"
              help="Only applies when auto-merge is ON. When a merge fails on conflicts, DevCake sends the mission back to EXECUTE to sync the branch and resolve them (max 2 attempts) instead of waiting for you at DEVCAKE-MERGE.">
              <div className="flex items-center gap-3 text-sm">
                <Toggle on={cfg.auto_resolve_merge_conflicts} label="Auto-resolve merge conflicts"
                  disabled={!cfg.auto_merge}
                  onClick={() => cfg.auto_merge &&
                    setField("cfg.auto_resolve_merge_conflicts", !cfg.auto_resolve_merge_conflicts)} />
                <span>{cfg.auto_resolve_merge_conflicts ? "ON — conflicts go back to EXECUTE (max 2 tries)" : "OFF — conflicts wait for you (DEVCAKE-MERGE)"}</span>
              </div>
            </Field>
            <Field label="Merge retry window (min)"
              help="When a merge isn't possible yet (CI running, mergeability computing), DevCake keeps retrying via the merge sweep for this long before handing off with DEVCAKE-MERGE. Lower it on CI-light repos to surface unmergeable PRs faster; raise it on CI-heavy repos. 0 = hand off immediately.">
              <Input type="number" min="0"
                disabled={!cfg.auto_merge}
                value={cfg.merge_retry_window_minutes}
                onChange={(e) => setField("cfg.merge_retry_window_minutes",
                  Math.max(0, Number(e.target.value)))} />
            </Field>
          </div>
        </div>
      </Section>

      {clearErr && <p className="text-sm text-red-600 dark:text-red-400">✗ {clearErr}</p>}
      <InternalReposSection refreshKey={internalRefresh}
        onClear={(name) => setConfirm({
          title: `Clear internal repo '${name}'?`,
          body: "Deletes the auto-created repository, its machine user (revoking "
            + "its tokens), and the stored credentials. The deliverable already "
            + "attached to the PMO feed is untouched. This cannot be undone.",
          confirmLabel: "Delete the internal repo",
          danger: true,
          action: async () => {
            setConfirm(null);
            try {
              await send("DELETE", `/internal-repos/${name}`);
              setInternalRefresh((n) => n + 1);
            } catch (e) { setClearErr(String(e.message || e)); }
          },
        })} />

      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()}
        onCancel={() => setConfirm(null)} />
    </div>
  );
}
