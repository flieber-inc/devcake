import React, { useEffect, useState } from "react";
import { get, send } from "./../api.js";

const MISSION_TYPES = ["ONBOARD", "PLAN", "EXECUTE", "REVIEW"];
const TEMPLATES = ["claude-code", "grok-build", "codex"];
const OAUTH_TEMPLATES = ["grok-build", "codex"];

// ── primitives ───────────────────────────────────────────────────────────────

function Section({ title, children }) {
  return (
    <section className="space-y-3 border-b border-neutral-100 pb-6 dark:border-neutral-800">
      <h3 className="text-base font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function Field({ label, hint, children }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-neutral-400">{hint}</span>}
    </label>
  );
}

const inputCls =
  "w-full rounded-md border border-neutral-300 bg-white px-2.5 py-1.5 text-sm " +
  "dark:border-neutral-700 dark:bg-neutral-950";

function Button({ children, kind = "primary", ...props }) {
  const styles = {
    primary: "bg-accent text-white hover:brightness-110",
    ghost:
      "border border-neutral-300 hover:bg-neutral-50 dark:border-neutral-700 dark:hover:bg-neutral-800",
    danger: "bg-red-700 text-white hover:brightness-110",
  };
  return (
    <button
      {...props}
      className={`rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-40 ${styles[kind]}`}
    >
      {children}
    </button>
  );
}

function ConfirmDialog({ open, title, body, confirmLabel, onConfirm, onCancel }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-lg rounded-xl bg-white p-6 shadow-xl dark:bg-neutral-900">
        <h4 className="mb-2 text-base font-semibold">{title}</h4>
        <p className="mb-5 whitespace-pre-line text-sm text-neutral-600 dark:text-neutral-300">
          {body}
        </p>
        <div className="flex justify-end gap-2">
          <Button kind="ghost" onClick={onCancel}>Cancel</Button>
          <Button kind="danger" onClick={onConfirm}>{confirmLabel}</Button>
        </div>
      </div>
    </div>
  );
}

// ── OAuth wizard (docs/16 M6): device-code flow driven from the UI ──────────

function OAuthWizard({ harness, onClose }) {
  const [status, setStatus] = useState({ state: "starting" });
  const [runId, setRunId] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let timer;
    send("POST", `/oauth/${harness}/start`)
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
  }, [harness]);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-lg rounded-xl bg-white p-6 shadow-xl dark:bg-neutral-900">
        <h4 className="mb-3 text-base font-semibold">Connect {harness} (OAuth)</h4>
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

function DevTypeCard({ dt, onSave, onDelete, onOAuth }) {
  const [d, setD] = useState(dt);
  const [credFile, setCredFile] = useState(null);
  const set = (k, v) => setD({ ...d, [k]: v });
  const [credMsg, setCredMsg] = useState("");
  const uploadCred = async () => {
    const content = await credFile.text();
    await send("POST", `/dev-types/${d.name}/credentials`, {
      filename: credFile.name,
      content,
    });
    setCredMsg(`✓ stored ${credFile.name}`);
    setCredFile(null);
    setTimeout(() => setCredMsg(""), 4000);
  };
  return (
    <div className="space-y-3 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="flex items-center justify-between">
        <span className="font-mono text-sm font-semibold">{d.name}</span>
        <div className="flex gap-2">
          {OAUTH_TEMPLATES.includes(d.harness_template) && (
            <Button kind="ghost" onClick={() => onOAuth(d.harness_template)}>
              Connect via OAuth…
            </Button>
          )}
          <Button onClick={() => onSave(d)}>Save</Button>
          <Button kind="danger" onClick={() => onDelete(d.name)}>Delete</Button>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Harness template">
          <select
            className={inputCls}
            value={d.harness_template}
            onChange={(e) => set("harness_template", e.target.value)}
          >
            {TEMPLATES.map((t) => <option key={t}>{t}</option>)}
          </select>
        </Field>
        <Field label="Max concurrency">
          <input
            type="number" min="1" className={inputCls} value={d.max_concurrency}
            onChange={(e) => set("max_concurrency", Number(e.target.value))}
          />
        </Field>
      </div>
      <Field label="Identifying prompt" hint="Delivered at the start of every run, before the mission playbook.">
        <textarea
          rows={3} className={inputCls} value={d.identifying_prompt}
          onChange={(e) => set("identifying_prompt", e.target.value)}
        />
      </Field>
      <Field
        label="MCP setup commands (one per line)"
        hint="⚠ Run inside the Dev container before the agent starts — arbitrary code execution by design. A failing command fails the run."
      >
        <textarea
          rows={2} className={inputCls}
          value={(d.mcp_setup_commands || []).join("\n")}
          onChange={(e) =>
            set("mcp_setup_commands", e.target.value.split("\n").filter(Boolean))}
        />
      </Field>
      <Field label="Credentials file (OAuth/JSON)" hint="Stored 0600 under /data/secrets/; delivered per-run over the runspec channel.">
        <div className="flex items-center gap-2">
          <input type="file" className="text-xs" onChange={(e) => setCredFile(e.target.files[0])} />
          <Button kind="ghost" disabled={!credFile} onClick={uploadCred}>Upload</Button>
          {credMsg && <span className="text-xs text-green-700 dark:text-green-400">{credMsg}</span>}
        </div>
      </Field>
    </div>
  );
}

// ── the tab ──────────────────────────────────────────────────────────────────

export default function ConfigTab() {
  const [cfg, setCfg] = useState(null);
  const [devTypes, setDevTypes] = useState([]);
  const [assignments, setAssignments] = useState({});
  const [confirm, setConfirm] = useState(null); // {title, body, confirmLabel, action}
  const [oauthFor, setOauthFor] = useState(null);
  const [testResult, setTestResult] = useState({});
  const [msg, setMsg] = useState("");

  const reload = () =>
    Promise.all([get("/config"), get("/dev-types"), get("/assignments")]).then(
      ([c, d, a]) => { setCfg(c); setDevTypes(d); setAssignments(a); });
  useEffect(() => { reload().catch((e) => setMsg(String(e))); }, []);
  if (!cfg) return <p className="text-sm text-neutral-400">Loading…{msg}</p>;

  const putCfg = async (patch) => {
    setCfg(await send("PUT", "/config", patch));
    setMsg("Saved.");
    setTimeout(() => setMsg(""), 2000);
  };
  const guardedToggle = (key, value, title, body) =>
    setConfirm({
      title, body, confirmLabel: "I understand — proceed",
      action: async () => { await putCfg({ [key]: value }); setConfirm(null); },
    });

  const test = async (kind) =>
    setTestResult({ ...testResult, [kind]: await send("POST", `/connections/${kind}/test`) });

  const saveAssignment = async (mt, patch) => {
    const next = { ...assignments, [mt]: { ...assignments[mt], ...patch } };
    // harness-specific extra args: warn when the dev type (→ harness) changes
    if (patch.dev_type && assignments[mt].extra_cli_args) {
      const oldH = devTypes.find((d) => d.name === assignments[mt].dev_type)?.harness_template;
      const newH = devTypes.find((d) => d.name === patch.dev_type)?.harness_template;
      if (oldH !== newH) {
        setConfirm({
          title: `Keep ${mt}'s extra CLI args?`,
          body: `"${assignments[mt].extra_cli_args}" was written for the ${oldH} harness; ` +
                `${patch.dev_type} uses ${newH}. Harness flags rarely transfer.`,
          confirmLabel: "Clear the args (recommended)",
          action: async () => {
            next[mt].extra_cli_args = "";
            setAssignments(await send("PUT", "/assignments", next));
            setConfirm(null);
          },
          cancelAction: async () => {
            setAssignments(await send("PUT", "/assignments", next));
            setConfirm(null);
          },
        });
        return;
      }
    }
    setAssignments(await send("PUT", "/assignments", next));
  };

  return (
    <div className="space-y-6">
      {msg && <p className="text-sm text-green-700 dark:text-green-400">{msg}</p>}

      <Section title="PMO connection (Linear)">
        <div className="grid grid-cols-3 gap-3">
          <Field label="Team key"><input className={inputCls} value={cfg.pmo.team_key}
            onChange={(e) => setCfg({ ...cfg, pmo: { ...cfg.pmo, team_key: e.target.value } })} /></Field>
          <Field label="API key env var"><input className={inputCls} value={cfg.pmo.api_key_env}
            onChange={(e) => setCfg({ ...cfg, pmo: { ...cfg.pmo, api_key_env: e.target.value } })} /></Field>
          <Field label="Poll interval (s)"><input type="number" className={inputCls}
            value={cfg.poll_interval_seconds}
            onChange={(e) => setCfg({ ...cfg, poll_interval_seconds: Number(e.target.value) })} /></Field>
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={() => putCfg({ pmo: cfg.pmo, poll_interval_seconds: cfg.poll_interval_seconds })}>Save</Button>
          <Button kind="ghost" onClick={() => test("pmo")}>Test connection</Button>
          {testResult.pmo && (
            <span className={`text-sm ${testResult.pmo.ok ? "text-green-700 dark:text-green-400" : "text-red-600"}`}>
              {testResult.pmo.ok
                ? `✓ team ${testResult.pmo.team}: ${testResult.pmo.labels}/9 labels, ${testResult.pmo.missions_visible} items visible`
                : `✗ ${testResult.pmo.error}`}
            </span>
          )}
        </div>
        <Field label="Adoption mode">
          <div className="flex items-center gap-3 text-sm">
            <span className={cfg.adoption_mode === "opt_in" ? "font-semibold" : "text-neutral-400"}>
              opt-in (label required)
            </span>
            <button
              onClick={() =>
                cfg.adoption_mode === "opt_in"
                  ? guardedToggle("adoption_mode", "opt_out", "Adopt the ENTIRE team?",
                      "DevCake will adopt EVERY non-completed Issue and Project in this team — " +
                      "including the entire existing backlog — and start working through them by " +
                      "priority, consuming tokens.\n\nIn opt-in mode it only touches items you " +
                      "label DEVCAKE.")
                  : putCfg({ adoption_mode: "opt_in" })}
              className={`h-6 w-11 rounded-full p-0.5 transition ${cfg.adoption_mode === "opt_out" ? "bg-accent" : "bg-neutral-300 dark:bg-neutral-700"}`}
            >
              <span className={`block h-5 w-5 rounded-full bg-white transition ${cfg.adoption_mode === "opt_out" ? "translate-x-5" : ""}`} />
            </button>
            <span className={cfg.adoption_mode === "opt_out" ? "font-semibold" : "text-neutral-400"}>
              opt-out (whole team)
            </span>
          </div>
        </Field>
      </Section>

      <Section title="Repository">
        <div className="grid grid-cols-4 gap-3">
          <Field label="Forge">
            <select className={inputCls} value={cfg.repo.forge}
              onChange={(e) => setCfg({ ...cfg, repo: { ...cfg.repo, forge: e.target.value } })}>
              <option>github</option><option>gitlab</option>
            </select>
          </Field>
          <Field label="Repository URL"><input className={inputCls} value={cfg.repo.url}
            onChange={(e) => setCfg({ ...cfg, repo: { ...cfg.repo, url: e.target.value } })} /></Field>
          <Field label="Token env var"><input className={inputCls} value={cfg.repo.token_env}
            onChange={(e) => setCfg({ ...cfg, repo: { ...cfg.repo, token_env: e.target.value } })} /></Field>
          <Field label="Reviewer token env var" hint="Optional 2nd account → formal PR approvals">
            <input className={inputCls} value={cfg.repo.reviewer_token_env || ""}
              onChange={(e) => setCfg({ ...cfg, repo: { ...cfg.repo, reviewer_token_env: e.target.value || null } })} />
          </Field>
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={() => putCfg({ repo: cfg.repo })}>Save</Button>
          <Button kind="ghost" onClick={() => test("forge")}>Test connection</Button>
          {testResult.forge && (
            <span className={`text-sm ${testResult.forge.ok ? "text-green-700 dark:text-green-400" : "text-red-600"}`}>
              {testResult.forge.ok
                ? `✓ ${testResult.forge.forge} reachable · reviewer token: ${testResult.forge.reviewer_token_configured ? "yes" : "no"}`
                : `✗ ${testResult.forge.error}`}
            </span>
          )}
        </div>
        <Field label="Auto-merge">
          <div className="flex items-center gap-3 text-sm">
            <button
              onClick={() =>
                cfg.auto_merge
                  ? putCfg({ auto_merge: false })
                  : guardedToggle("auto_merge", true, "Merge without human review?",
                      "DevCake will merge its own pull requests to the default branch without " +
                      "human intervention (after its REVIEW step approves).\n\nWithout a reviewer " +
                      "token, merges proceed without a formal approval on the forge.")}
              className={`h-6 w-11 rounded-full p-0.5 transition ${cfg.auto_merge ? "bg-accent" : "bg-neutral-300 dark:bg-neutral-700"}`}
            >
              <span className={`block h-5 w-5 rounded-full bg-white transition ${cfg.auto_merge ? "translate-x-5" : ""}`} />
            </button>
            <span>{cfg.auto_merge ? "ON — approved PRs merge themselves" : "OFF — every merge is yours (DEVCAKE-MERGE handoff)"}</span>
          </div>
        </Field>
      </Section>

      <Section title="Dev Types">
        <div className="grid gap-4 lg:grid-cols-2">
          {devTypes.map((dt) => (
            <DevTypeCard key={dt.name} dt={dt}
              onSave={async (d) => { await send("PUT", `/dev-types/${d.name}`, d); reload(); }}
              onDelete={(name) =>
                setConfirm({
                  title: `Delete dev type ${name}?`,
                  body: "Its config file is removed; credentials under /data/secrets stay until cleaned manually.",
                  confirmLabel: "Delete",
                  action: async () => {
                    try { await send("DELETE", `/dev-types/${name}`); reload(); }
                    catch (e) { setMsg(String(e)); }
                    setConfirm(null);
                  },
                })}
              onOAuth={setOauthFor} />
          ))}
        </div>
        <Button kind="ghost"
          onClick={async () => {
            const name = `new-dev-${Date.now() % 1000}`;
            await send("POST", "/dev-types", {
              name, harness_template: "codex", identifying_prompt: "",
              max_concurrency: 1, docker_image: "devcake/dev-codex:latest",
            });
            reload();
          }}>
          + New Dev Type <span className="ml-1 text-xs text-neutral-400">(rename via its card)</span>
        </Button>
      </Section>

      <Section title="Assignments (mission type → dev type)">
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase text-neutral-400">
            <tr><th className="py-1">Mission type</th><th>Dev type</th><th>Extra CLI args (harness-specific)</th></tr>
          </thead>
          <tbody>
            {MISSION_TYPES.map((mt) => (
              <tr key={mt} className="border-t border-neutral-100 dark:border-neutral-800">
                <td className="py-2 font-mono text-xs font-semibold">{mt}</td>
                <td>
                  <select className={inputCls} value={assignments[mt]?.dev_type || ""}
                    onChange={(e) => saveAssignment(mt, { dev_type: e.target.value })}>
                    {devTypes.map((d) => <option key={d.name}>{d.name}</option>)}
                  </select>
                </td>
                <td>
                  <input className={inputCls} value={assignments[mt]?.extra_cli_args || ""}
                    placeholder="e.g. --max-turns 15"
                    onChange={(e) => setAssignments({ ...assignments, [mt]: { ...assignments[mt], extra_cli_args: e.target.value } })}
                    onBlur={(e) => saveAssignment(mt, { extra_cli_args: e.target.value })} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="Limits">
        <div className="grid grid-cols-3 gap-3">
          <Field label="Global max Devs" hint="Effective ceiling = min(global, Σ per-type caps)">
            <input type="number" className={inputCls} value={cfg.concurrency.global_max}
              onChange={(e) => setCfg({ ...cfg, concurrency: { global_max: Number(e.target.value) } })} />
          </Field>
          <Field label="Dev run timeout (min)">
            <input type="number" className={inputCls} value={cfg.dev_timeout_minutes}
              onChange={(e) => setCfg({ ...cfg, dev_timeout_minutes: Number(e.target.value) })} />
          </Field>
          <Field label="Loop warning every N rejections">
            <input type="number" className={inputCls} value={cfg.review_loop_warning_every}
              onChange={(e) => setCfg({ ...cfg, review_loop_warning_every: Number(e.target.value) })} />
          </Field>
        </div>
        <Button onClick={() => putCfg({
          concurrency: cfg.concurrency,
          dev_timeout_minutes: cfg.dev_timeout_minutes,
          review_loop_warning_every: cfg.review_loop_warning_every,
        })}>Save</Button>
      </Section>

      <ConfirmDialog open={!!confirm} {...(confirm || {})}
        onConfirm={() => confirm.action()}
        onCancel={() => (confirm.cancelAction ? confirm.cancelAction() : setConfirm(null))} />
      {oauthFor && <OAuthWizard harness={oauthFor} onClose={() => setOauthFor(null)} />}
    </div>
  );
}
