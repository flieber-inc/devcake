# ADR-0011 — Single-mode GUI secret store (env-indirection deleted)

- **Status:** accepted (2026-07-14, docs/16 M12, feature F5)
- **Context:** Through v0.1 the operator supplied credentials by putting the secret VALUE in `.env` and referencing its env-var NAME in config (`token_env: GITHUB_TOKEN`). This dual-source model was the origin of the first real-world incident: the operator pasted the PAT *value* into the "token env var" field, yielding an empty token and cryptic `Illegal header value b'Bearer '` failures. Two sources of truth for one secret is a UX trap.

## Decision

**One mode.** Every operator-supplied secret — PMO API keys, forge tokens (write/read-only/reviewer), model/harness keys — is entered as a VALUE through the admin Config page and stored on the app volume. Env-var indirection is **deleted, not kept as a fallback**.

- Storage: `/data/secrets/connections/{scope}-{instance}.json` (scope ∈ pmo|repo) and `/data/secrets/harness/{VAR}.json`, written 0600 (`mkstemp` + `fsync` + `os.replace`). The two-level path means the existing redaction glob (`security._known_values`, `glob("*/*")`) auto-covers every value; each write also `register_runtime_secret`s it.
- Config schema **v4**: the `*_env` fields are gone; `RepoInstance.token`/`token_ro`/`reviewer_token` and `PMOInstance.api_key` are read-through properties over the store. `pmos`/`repos` relax to 0..N so a truly empty first boot is a defined idle state (GUI-only setup).
- **Never echoed:** `GET /config` carries no secret material. `GET /secrets-check` returns `{present, updated_at}` only — deliberately **no value-derived fingerprint** (an unsalted hash would let anyone who sees the UI confirm a guessed/leaked candidate secret offline). `PUT` accepts a value and returns status; the SPA's write-only `SecretField` clears after submit.
- **`.env` exceptions:** stack bootstrap secrets only — the things needed before the GUI is up: Dagu, Redis, OpenObserve (root + ingest), the nginx admin basic-auth, `GITEA_ADMIN_*`, `DOCKER_GID`. Boot still refuses empty/weak values for these.

## Standing posture breadcrumb

A dismissable `gui-secrets-basic-auth` info warning ships in `/health`: "GUI-stored secrets behind basic auth — revisit before exposing beyond localhost." The decision (secrets on disk, admin behind basic auth) is intentional for the localhost/dedicated-host posture DevCake targets — see the product security contract in **docs/14-security.md §0 / §4**. OIDC/SSO + a secret manager remain optional if you expose beyond that posture (docs/14 §11), not required for the default deploy.

## Consequences

- Fresh-`/data` operator drill is GUI-only (`docs/tutorials/operator-drill.md`): from an empty volume to a completed mission with `.env` untouched beyond bootstrap.
- `acceptance.py` sources TESTER credentials (the Linear key it impersonates the end-user with) from the shell/`.env`, never from DevCake's stored secrets — never-echo holds even for tooling.
- Migration from v3 is hand-done (no deployments exist): move each secret's VALUE into the store, drop the `*_env` fields, set `schema_version: 4`. The boot refusal detects a v3 `*_env`-shaped file and prints the recipe (docs/10 §3).
