# Tutorial 3 — Give Your Devs Production Logs (Datadog or CloudWatch)

A Dev debugging "payment-api throws 500s since the deploy" works blind unless
it can see the same logs you can. Every Dev image ships `devcake-logs-mcp`
(`07-dev-runtime.md` §6a) — a read-only MCP server over your log platform —
but it stays **inert until a Dev Type opts in**. Wiring it up is pure
configuration; this takes about two minutes in the admin panel.

Backends: **Datadog** (steps 1–3 below) and **AWS CloudWatch Logs** (see the
last section). Auth is key-based in both cases — non-interactive, so it works
in the headless ephemeral Dev containers.

## 1. Get the Datadog keys

In Datadog: **Organization Settings → API Keys** (create or reuse one) and
**→ Application Keys** (create one; scope it to `logs_read_data` if your org
uses key scoping — the server only reads).

## 2. Store the keys as secret env vars

Config page → the Dev Type you want to empower (e.g. `senior-dev`):

1. **Secret env vars** — add two lines:

   ```
   DD_API_KEY
   DD_APP_KEY
   ```

2. Paste each value into the paste field that appears under the list. Values
   land `0600` under `/data/secrets/harness/` and are redaction-registered
   (ADR-0011: names in config, values in the secret store — never the other
   way around). The stored value is global: other Dev Types listing the same
   name reuse it.

## 3. Register the MCP server

Same card → **MCP setup commands**, one line (claude-code harness):

```
claude mcp add devcake-logs -e DD_API_KEY=$DD_API_KEY -e DD_APP_KEY=$DD_APP_KEY -e DD_SITE=datadoghq.com -- devcake-logs-mcp
```

`$DD_API_KEY`/`$DD_APP_KEY` are expanded inside the Dev container from the
secret env vars you just stored. `DD_SITE` is a plain literal — use
`datadoghq.eu` for EU orgs. Save.

## 4. What the Dev can now do

Four read-only tools, responses trimmed for token economy
(`08-harness-templates.md` §7):

| Tool | The Dev uses it to… |
|---|---|
| `list_services` | discover what emits logs before searching |
| `search_logs` | find events: `service:payment-api status:error "card declined"` |
| `get_log_context` | pull the trace-correlated logs (or a ±2 min window) around one hit |
| `aggregate_logs` | see which services/statuses dominate an incident window |

A good smoke mission: create a Linear issue like *"Check error logs for
service X over the last hour and summarize the top failure modes"*, label it
`DEVCAKE`, and watch the run log for `mcp__devcake-logs__search_logs` calls.

## Troubleshooting

- **Run fails immediately with exit 14** — the `claude mcp add` line itself
  failed (typo, wrong flag). The run log shows the command and stderr.
- **Tool calls answer `error: DD_API_KEY/DD_APP_KEY not set…`** — the Dev
  Type's Secret env vars list is missing a name, or a value was never pasted
  (the card shows ✗ per missing var). Missing values never fail the run —
  the Dev just gets that message.
- **`error: datadog 403…`** — the application key lacks Logs read
  permission, or key and org don't match the `DD_SITE`.
- **Different Datadog orgs per Dev Type** — store a second value under
  different names (e.g. `DD_API_KEY_STAGING`) and reference those in that
  Dev Type's `mcp add` line (`-e DD_API_KEY=$DD_API_KEY_STAGING …`).

## AWS CloudWatch Logs instead

Same shape, different names. Use an IAM user (or STS credentials) allowed
`logs:StartQuery`, `logs:GetQueryResults`, `logs:DescribeLogGroups`.

1. **Secret env vars** on the Dev Type: `AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY` (add `AWS_SESSION_TOKEN` if using STS); paste the
   values.
2. **MCP setup commands** — one line:

   ```
   claude mcp add devcake-logs -e DEVCAKE_LOGS_BACKEND=cloudwatch -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY -e AWS_REGION=us-east-1 -e DEVCAKE_LOGS_GROUPS=/app/payment,/app/checkout -- devcake-logs-mcp
   ```

   `AWS_REGION` and `DEVCAKE_LOGS_GROUPS` are non-secret literals. Omit
   `DEVCAKE_LOGS_GROUPS` to auto-discover up to 50 log groups per query.

Dialect notes (the tool docstrings teach the Dev the same things): plain-text
queries filter the message (`"card declined"`); a query containing a pipe is
passed through as a raw **Logs Insights** query; log groups play the
"service" role; there is no status field and no pagination cursor; searches
take a few seconds (Insights is an async query API). For `get_log_context`
prefer **timestamp** mode — CloudWatch has no trace facet, so trace mode
falls back to a message-substring match.
