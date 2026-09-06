# ADR-0041 — Operator MCP server: the API's own description is the tool catalogue

## Context — the recommended way to run DevCake is beside a coding agent

DevCake is set up, inspected and repaired most effectively with a coding
agent as the operator's helper: the README opens with a prompt for that
agent and the operator skill (`skills/devcake-ops/SKILL.md`) teaches it the
product model, the loopback admin API and an evidence-based repair loop.
Today the agent reaches the system by constructing HTTP requests from the
documentation. That works for a capable agent but it is untyped, easy to get
subtly wrong (the intent header, the loopback proxy, credentials in argv),
and undiscoverable: the agent has to know a route exists before it can call
it.

The Model Context Protocol is the vendor-neutral way every relevant coding
agent discovers and calls tools. DevCake already uses it on the other side
of the fence: the Devs reach logs through a separately shipped MCP server.
An operator-side server is the natural counterpart, and it keeps the
vendor-segregation rule intact because MCP is a protocol, not a vendor.

Two constraints shape the design:

1. **Nothing is coded twice.** The codebase honours chokepoints: one wire
   path per adapter, one completion path, one governor. A hand-written MCP
   tool per API route would be a second catalogue that drifts the moment a
   route is added or changed. A new route must become a new tool with no
   further code.
2. **The admin has one principal.** The control plane is HTTP basic auth
   with one credential, on loopback or a dedicated host, by design
   (`docs/14-security.md`). Anything that hands the API to a second party
   must respect that until the model changes on purpose.

## Decision

### 1 — Where it runs and how it is installed

The server is a verb of the CLI, `devcake mcp`, speaking MCP over **stdio**
to the agent that launched it. It talks to the loopback admin proxy exactly
as the CLI's `status` verb does — basic auth read from the deployment's
`.env`, the `X-DevCake-Request: 1` intent header on every mutation — through
one shared admin client in the CLI. It is **never a network listener**:
publishing the admin port publishes every stored token (docs/14 §4), and a
stdio server inherits the operator's own session and host access instead.
An agent on another machine reaches it over SSH, the way it reaches the
host for everything else.

The protocol implementation is the official MCP SDK, installed as an
optional extra (`devcake-cli[mcp]`) so the base CLI keeps its zero
dependencies. Hand-rolling the JSON-RPC surface would save one dependency
and cost protocol drift against every client; not worth it.

### 2 — The catalogue is derived, never written

The FastAPI application already describes every route: method, path,
parameters, request and response schemas, and the handler's docstring. That
description is the catalogue.

- The app publishes its OpenAPI document **under the authenticated API
  prefix** (`/api/v1/openapi.json`; the document was previously disabled).
  The auth middleware protects it like any other GET; the SPA does not use
  it.
- At startup the server fetches the document and builds **one tool per
  operation**: the tool name is the route function's name (the app sets a
  unique-id function so the operation id is that name and nothing else),
  the description is the handler's docstring, the input schema is the
  operation's path, query and body schemas folded into one object. The
  agent sees the same contract the documentation states, taken from the
  code that enforces it.
- Operations whose response is not JSON (artifact zips, exports) are
  excluded by that generic rule; a route that must never be reachable by an
  agent carries **one marker at its definition**
  (`openapi_extra={"x-devcake-mcp": "never"}`). That marker is the only
  per-route knob in the design, and it lives where the route lives.
- A structure test guards the chokepoint from both sides: every API route
  must carry a docstring (so every tool is described), and no module may
  hold a second list of tools. Adding a route without a docstring fails the
  suite; adding a tool anywhere but the route fails the suite.

### 3 — Two flavours from one catalogue

`devcake mcp --read-only` exposes the GET operations only. The default
exposes every operation that is not opted out; each mutation goes out with
the intent header and an actor header (`X-DevCake-Actor: mcp`) that the
app's audit chokepoint records, so a change made through an agent is
distinguishable from one made in the admin panel. The read-only flavour is
a filter over the same derived catalogue, not a second one.

### 4 — What never crosses

Secret values never cross the server. Presence checks and inventories do
(`secrets-check`, `secrets/inventory` report names and presence, never
values); the settings export and import routes, the secrets clear route and
the token-copy route carry the opt-out marker. The agent diagnoses with
presence and connection tests, as the operator skill already teaches.

Tools that reach a tracker or forge ride the request governor (ADR-0040)
like every other caller; nothing here adds a wire path.

### 5 — Phase two: a read-only principal, decided by debate first

The read-only flavour handed to the operator's own agent is honest: the
credential behind it is the operator's. Handed to a **viewer** it is only as
read-only as the tool list, because the same credential can do everything.
Making that safe needs a second principal in the app: a credential the auth
middleware limits to GET, which the admin panel could also use for a viewer
mode. That is a one-way door in the auth model (single operator by design,
docs/14) and is **not decided here**. Candidate shapes for the debate:

- a second basic-auth pair in `.env` (`VIEWER_USER` / `VIEWER_PASSWORD`),
  GET-only by middleware — smallest change, one more secret to manage;
- scoped tokens minted by the app with a role and an expiry — more moving
  parts, revocable, auditable per token;
- no second principal: read-only stays a client-side filter, viewers get a
  read-only admin panel instead — no auth change, no viewer MCP.

Phase one ships without a viewer principal.

### 6 — Documentation follows the landing

Once the server lands, the README's "operate it with your agent" section and
the operator skill's API paragraph gain the one-line setup for the agent's
MCP configuration and the read-only/read-write distinction; the docs/11 REST
table stays the human-readable contract and gains the opt-out marker's
meaning.

## Consequences

- A new API route is a new tool, described by its docstring, with no code
  in the server. The docstring becomes a public contract, and the guard
  makes its absence a test failure rather than an undocumented tool.
- The OpenAPI document becomes part of the authenticated API surface; the
  operation ids change to plain function names (nothing consumed the old
  generated ids).
- The CLI gains an optional extra; its base install is unchanged.
- The audit trail distinguishes agent-made changes.
- Viewer access waits for the principal decision; nothing in phase one
  forecloses either shape.

## Verification plan

- Unit tests for the catalogue builder against a captured OpenAPI document:
  one tool per JSON operation, names and descriptions from the document,
  folded input schemas, non-JSON operations excluded, the opt-out marker
  honoured, the read-only filter keeping GET only.
- The structure guard: every `/api/v1` route has a docstring; no second
  tool list exists.
- An end-to-end test with the SDK client over stdio against the CLI verb
  pointed at a test-served app: `initialize`, `tools/list`, one read call,
  one mutation carrying the intent and actor headers, one opted-out route
  absent from the list.
- Manual: an operator's agent configured with `devcake mcp --read-only`
  reads health, activity and runs on a dedicated host; the read-write
  flavour pauses and resumes intake and the audit row shows the actor.

## Related

ADR-0038 (CLI scope and agent operability), ADR-0034 (chokepoints),
ADR-0040 (request governor), docs/14 (security contract, single principal),
`skills/devcake-ops/SKILL.md` (the operator's helper).
