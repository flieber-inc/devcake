# Fresh-`/data` operator drill (GUI-only)

> The stranger-operability test (docs/16 M12): from an empty volume to a
> completed mission touching `.env` **only** for stack bootstrap secrets.
> Everything else — PMO instances, repos, tokens, model keys — goes through
> the Config page. If any step needs `.env` for operator secrets, that is a
> regression (ADR-0011).
>
> **Wipe warning:** `docker volume rm …devcake_data` destroys config **and**
> all GUI secrets on a dedicated host — treat the volume like a secret store
> (`14-security.md` §1).

## 0. Bootstrap `.env` (the only file you edit)

```bash
cp .env.example .env
```

Fill in **only** the stack bootstrap secrets — the things needed before the
GUI is up:

- `REDIS_PASSWORD`, `DAGU_PASSWORD`, `ADMIN_USER`/`ADMIN_PASSWORD` (strong; boot refuses empty/default)
- `OO_ROOT_PASSWORD`, `OO_INGEST_EMAIL`/`OO_INGEST_PASSWORD` (the OO service account; needs a special char)
- `GITEA_ADMIN_PASSWORD` (the internal fallback forge's admin)
- `DOCKER_GID` (`stat -c %g /var/run/docker.sock`)

There are **no** `LINEAR_API_KEY`, `GITHUB_TOKEN`, `DEVCAKE_TEAM_KEY`, … lines
anymore — those are v3 and were removed at v4.

## 1. Wipe + bring up

```bash
docker volume rm devcake_devcake_data          # DESTRUCTIVE — the operator drill
docker buildx bake all
docker compose up -d
# App boot creates the OO ingest user from OO_INGEST_* (fail-loud).
# Optional: dashboard + alerts (docs/12 §5):
#   python3 scripts/provision_oo.py
```

Open `http://localhost:8080` (admin basic auth). `/health` should show `app`,
`redis`, `dagu`, `openobserve`, `oo_ingest`, and the internal forge green;
`pmo_instances` empty (nothing configured yet).

## 2. Configure everything via the GUI

- **Configuration → PMO** → Add PMO instance → name + team key → **Set** the API key (stored 0600, never echoed) → Test connection (expect ✓ team + labels).
- **Repositories** (`#/repos`, not under Configuration) → Add repository → name + forge + URL → **Set** the access token (+ optional read-only / reviewer tokens) → Test connection.
  *(Skip this to exercise the zero-repo / internal-forge path.)*
- **Configuration → Dev Types** → for each harness key (e.g. `ANTHROPIC_API_KEY`) **Set** the value, or **Connect via OAuth** for subscription harnesses.
- Set assignments; leave intake enabled (sidebar master switch). Sidebar health dots should go green.

The `.env` file is not touched by any of this.

## 3. Run two missions

- One **external-repo** mission: create a Linear issue in the configured team, label it `DEVCAKE`, attach it to a repo (via a `` `devcake-repo:<name>` `` line or the instance default) → watch it reach Done with a merged PR.
- One **zero-repo** mission: create a Linear issue with no repo → it routes to the bundled Gitea, completes ONBOARD→…→merge→Done, and the **deliverable zip lands in the Linear feed** (the internal PR sits merged in Gitea at `http://localhost:3300`).

## 4. Assert `.env` was untouched beyond bootstrap

```bash
grep -E '^(LINEAR|GITHUB|GITLAB|ANTHROPIC|XAI|OPENAI|CODEX|CLAUDE_CODE|DEVCAKE_TEAM_KEY|DEVCAKE_REPO_URL)' .env && echo "REGRESSION: connection/harness secret in .env" || echo "OK: .env is bootstrap-only"
```

Scriptable health checks (all should pass):

```bash
PMO_INSTANCE=linear  # replace with the PMO instance name chosen in step 2
# every configured secret shows present
curl -su "$ADMIN_USER:$ADMIN_PASSWORD" \
  "http://localhost:8080/api/v1/secrets-check?conn=pmo:${PMO_INSTANCE}:api_key"
# GET /config carries no secret-bearing or legacy *_env fields
curl -su "$ADMIN_USER:$ADMIN_PASSWORD" http://localhost:8080/api/v1/config \
  | python3 -c '
import json, sys

forbidden = {"api_key", "token", "token_ro", "reviewer_token"}

def exposed_paths(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path + (str(key),)
            if key in forbidden or key.endswith("_env"):
                yield ".".join(child_path)
            yield from exposed_paths(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from exposed_paths(child, path + (str(index),))

bad = list(exposed_paths(json.load(sys.stdin)))
if bad:
    print("secret-bearing config fields: " + ", ".join(bad), file=sys.stderr)
    raise SystemExit(1)
print("config structure clean")
'
```

The second check is structural and prints field paths only, never values. The
automated API regression test additionally plants known secret values and
proves neither the field names nor those values appear in the response.

The drill itself stays manual — it *is* the stranger-operability check. The
`acceptance.py --forge gitea` lane automates the zero-repo assertion (merged
internal PR + deliverable zip) using the bootstrap `GITEA_ADMIN_*`, spending
no external **forge** credentials. It still uses a Linear API key and real
model credentials.
