# Tutorial 3 — Give a Dev Type an MCP plugin

DevCake Devs gain tools through **MCP plugins**: standalone MCP servers
installed and registered inside the Dev container just before the harness
starts. The core app knows nothing about any vendor — a plugin is two Dev
Type fields on the Config page:

- **Secret env vars** — the NAMES of the credentials the plugin needs
  (values pasted separately into the GUI secret store, never in config).
- **MCP setup commands** — the shell lines that install and register the
  plugin. Admin-equivalent execution inside the disposable container by
  design (`../14-security.md` §2 Zone B).

Worked example throughout: **devcake-logs-mcp**, a log-platform connector
(Datadog / AWS CloudWatch Logs). Treat it as a **pattern**, not a guaranteed
public install: the official companion may be private or unpublished. Substitute
an `OWNER/REPO` (and pin a release tag) for a plugin git URL **you** control.
Backend-specific detail (key scopes, IAM permissions, query dialects) lives in
that plugin repo's README, not here.

## 1. Declare the secret names

Config page → the Dev Type's card → **Secret env vars**, one per line:

```
LOGS_MCP_GIT_TOKEN
DD_API_KEY
DD_APP_KEY
```

A paste field appears per name — paste each value there (stored `0600`
under `/data/secrets/harness/`, redaction-registered, ADR-0011). The store
is global: one stored value serves any number of Dev Types declaring the
same name.

`LOGS_MCP_GIT_TOKEN` is only needed while the plugin repo is private: a
fine-grained GitHub PAT with repository access limited to that repo and
permission **Contents: Read-only**.

## 2. Add the setup commands

**MCP setup commands**, in order (install, then register):

```
pip install --user --quiet "git+https://${LOGS_MCP_GIT_TOKEN}@github.com/OWNER/REPO@v0.1.0"
claude mcp add devcake-logs -e DD_API_KEY=$DD_API_KEY -e DD_APP_KEY=$DD_APP_KEY -e DD_SITE=datadoghq.com -- python -m logs_mcp.server
```

Mechanics worth knowing (`../07-dev-runtime.md` §5, `../08-harness-templates.md` §7):

- Commands run in the cloned repo directory as uid 1000, stdin closed,
  **300 s cap each**, full outbound network (Zone B by design).
- `$VAR` / `${VAR}` expand from the declared secret env vars in the
  entrypoint shell — values never enter config.
- **Pin a release tag** (`@v0.1.0`) — runs must not float with a moving
  branch.
- Register with `python -m <module>` or the installed console script:
  `pip install --user` lands scripts in `~/.local/bin`, which is **on**
  `PATH` in every registry harness image (ADR-0023 toolchain floor).
- When the plugin repo is public, drop `${LOGS_MCP_GIT_TOKEN}@` from the
  install line and delete that secret.

## 3. Save and verify

- Headless provisioning check:
  `curl -su "$ADMIN_USER:$ADMIN_PASSWORD" localhost:8080/api/v1/dev-types | jq '.[].secret_env_present'`
  — every declared name `true`.
- Run any mission on the Dev Type: the run log shows the install and
  register lines executing, then `mcp__devcake-logs__*` tool calls once the
  agent reaches for them.

## Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| Mission never dispatches; `/api/v1/health` `blocked_reasons` names a var | The var is **referenced** by a setup command but has no stored value — dispatch refuses deterministically (`../14-security.md` §8). Paste the value; the next poll cycle dispatches. |
| Run fails with `DEV_MCP_SETUP: <command>: exit N: …` | That additive setup command failed — the run error carries its stderr tail (typo'd install URL, revoked/mis-scoped token, nonexistent tag, bad `mcp add` flag). Counted attempt; fix the command or secret and the scheduler retries. Override-mode script aborts land here too (`set -e` / non-zero). |
| Run fails with `DEV_MCP_SETUP: <command>: timed out after 300s` | The additive setup command hung (registry stall, interactive prompt). Each additive line has a hard 300 s cap — split slow installs across lines or pin a closer mirror. (Override-mode hangs are the run wall-clock / `DEV_TIMEOUT`, not this row.) |
| Declared-but-unused name shows ✗ | Harmless: unreferenced missing values warn-and-proceed; only referenced ones gate. |
| Plugin tools error at call time (e.g. `DD_API_KEY … not set`) | The register line doesn't pass the var with `-e`, or the stored value is wrong — the plugin's own error message names the variable. |

Registration syntax for the grok/codex harnesses: `../08-harness-templates.md`
§7. Its verification labels are version-specific: Grok 0.2.93 and the pinned
Codex CLI 0.144.4 were live-probed (codex mcp syntax re-probed at 0.147.0);
Grok house pin is 0.2.112 — recheck the syntax when `GROK_VERSION` is bumped.
