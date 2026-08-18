#!/usr/bin/env python3
"""DevCake acceptance (docs/16 M7): the golden path, unattended, with REAL
models. Creates a fresh mission in the sandbox Linear team and asserts it
travels Backlog → In Progress → (labels progressing) → merged PR → Done with a
transcript + token report for every step. Exits nonzero on any violation.

**Manual / pre-release only.** Spends real model + PMO tokens. **Not** part of
default FOSS CI (``scripts/ci_suite.sh`` / GitHub Actions unit path are
model-free). Tester credentials come from the shell/``.env`` — never DevCake
stored secrets. Live founder-token gates may still block a full green run on a
given host; that residual is operational, not a silent CI obligation.

Run manually before releases:
    python3 scripts/acceptance.py [--runs 2] [--forge github|gitlab|gitea]

GitHub is the default. GitLab uses GITLAB_TOKEN and api.gitlab.com (or the
origin of DEVCAKE_REPO_URL). Gitea verifies the bundled zero-repo forge without
external forge credentials; every lane still uses Linear and real models.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.linear.app/graphql"


def env(name, required=True):
    # shell environment first, then .env (audit A19: the docs promise
    # "shell/.env" but only the file was read — an exported override lost)
    v = os.environ.get(name)
    if v:
        return v.strip()
    for line in open(".env"):
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    if required:
        sys.exit(f"missing {name} (export it or set it in .env)")
    return ""


# Tester-side credentials/targets: overridable per run (schema v3 made
# instances plural — the gate must be able to point at any team/key/repo,
# not just the .env seeds).
KEY = env("LINEAR_API_KEY", required=False)
TEAM = env("DEVCAKE_TEAM_KEY", required=False)
REPO_URL = env("DEVCAKE_REPO_URL", required=False)
FORGE = "github"      # overridden in __main__
INSTANCE = "linear"   # the DevCake PMO-instance name watching TEAM (branch prefix)


def gql(q, v=None):
    req = urllib.request.Request(API, data=json.dumps({"query": q, "variables": v or {}}).encode(),
                                 headers={"Authorization": KEY, "Content-Type": "application/json"})
    b = json.load(urllib.request.urlopen(req))
    assert not b.get("errors"), b["errors"]
    return b["data"]


def forge_get(forge: str, path: str):
    if forge == "github":
        token = env("GITHUB_TOKEN")
        repo = REPO_URL.removeprefix("https://github.com/").removesuffix(".git")
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}{path}",
            headers={"Authorization": f"Bearer {token}"})
        return json.load(urllib.request.urlopen(req))
    if forge == "gitlab":
        token = env("GITLAB_TOKEN")
        parts = urllib.parse.urlsplit(REPO_URL)
        base = f"{parts.scheme}://{parts.netloc}" if parts.scheme else "https://gitlab.com"
        project = urllib.parse.quote(parts.path.strip("/").removesuffix(".git"), safe="")
        req = urllib.request.Request(
            f"{base}/api/v4/projects/{project}{path}",
            headers={"PRIVATE-TOKEN": token})
        return json.load(urllib.request.urlopen(req))
    if forge == "gitea":
        # the internal fallback forge — verified via GITEA_ADMIN_* (a stack
        # bootstrap secret; no external forge credential). The zero-repo
        # mission's repo is devcake-internal/{instance}-{key} (lowercased);
        # its merged PR is the deliverable's source.
        import base64
        user, pw = env("GITEA_ADMIN_USER", required=False) or "devcakeadmin", env("GITEA_ADMIN_PASSWORD")
        gitea = env("GITEA_UI_URL", required=False) or "http://localhost:3300"
        auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
        req = urllib.request.Request(f"{gitea}/api/v1{path}",
                                     headers={"Authorization": f"Basic {auth}"})
        return json.load(urllib.request.urlopen(req))
    sys.exit(f"unknown forge {forge!r}")


# Backward-compatible alias used by the rest of this script (GitHub-shaped paths)
def gh(path):
    return forge_get("github", path)


MISSIONS = [
    ("Add a --quiet flag to the calc CLI that suppresses all non-result output, with tests",
     "The CLI currently prints results plainly; add a `--quiet` flag that guarantees ONLY "
     "the numeric result is printed on stdout (errors still to stderr), update help text, "
     "and add pytest coverage for quiet + error interactions."),
    ("Support exponent (**) in the calculator with tests",
     "Add `**` (exponentiation) to evaluate()/the CLI, including negative and fractional "
     "exponents, division-style error handling for invalid forms, and pytest coverage."),
]


def run_once(idx: int) -> None:
    title = f"[ACCEPT-{int(time.time())}] {MISSIONS[idx % len(MISSIONS)][0]}"
    desc = MISSIONS[idx % len(MISSIONS)][1]
    team = gql('query($k: String!) { teams(filter: {key: {eq: $k}}) { nodes { id '
               'labels(first:100){nodes{id name}} states{nodes{id type position}} } } }',
               {"k": TEAM})["teams"]["nodes"][0]
    labels = {l["name"].upper(): l["id"] for l in team["labels"]["nodes"]}
    backlog = next(s["id"] for s in sorted(team["states"]["nodes"],
                                           key=lambda s: s.get("position") or 0)
                   if s["type"] == "backlog")
    issue = gql('mutation($i: IssueCreateInput!) { issueCreate(input: $i) '
                '{ issue { id identifier } } }',
                {"i": {"teamId": team["id"], "title": title, "description": desc,
                       "priority": 2, "stateId": backlog,
                       "labelIds": [labels["DEVCAKE"]]}})["issueCreate"]["issue"]
    key = issue["identifier"]
    print(f"[{idx+1}] created {key}: {title[:60]}", flush=True)

    seen_labels, deadline = set(), time.time() + 45 * 60
    while time.time() < deadline:
        d = gql('query($id: String!) { issue(id: $id) { state { type } '
                'labels(first:10){nodes{name}} comments(first:50){nodes{body}} } }',
                {"id": issue["id"]})["issue"]
        stage = {l["name"] for l in d["labels"]["nodes"] if l["name"].startswith("DEVCAKE-")}
        seen_labels |= stage
        state = d["state"]["type"]
        if state == "completed":
            comments = [c["body"] for c in d["comments"]["nodes"]]
            transcripts = [c for c in comments if "DevCake transcript" in c]
            reports = [c for c in comments if "token report" in c]
            assert transcripts, "no transcripts posted"
            assert len(reports) >= len(transcripts), "token report missing for a step (INV-5)"
            assert "DEVCAKE-REVIEW" in seen_labels, "REVIEW was skipped"
            branch = f"devcake/{INSTANCE.upper()}-{key}"   # schema v3 prefix
            if FORGE == "github":
                owner = REPO_URL.removeprefix("https://github.com/").split("/")[0]
                pr = forge_get("github", f"/pulls?head={owner}:{branch}&state=all")
                assert pr and pr[0].get("merged_at"), f"PR for {key} not merged"
                pr_ref = f"PR #{pr[0]['number']}"
            elif FORGE == "gitlab":
                mrs = forge_get(
                    "gitlab", f"/merge_requests?source_branch={branch}&state=all")
                assert mrs and mrs[0].get("state") == "merged", \
                    f"MR for {key} not merged"
                pr_ref = f"MR !{mrs[0]['iid']}"
            else:  # gitea = the zero-repo internal-forge lane
                assert any("Deliverable attached" in c for c in comments), \
                    "no deliverable zip attached to the PMO feed"
                repo = f"{INSTANCE}-{key}".lower()
                prs = forge_get("gitea",
                                f"/repos/devcake-internal/{repo}/pulls?state=all")
                merged = [p for p in prs if p.get("merged")]
                assert merged, f"internal PR for {key} not merged"
                pr_ref = f"internal PR #{merged[0]['number']} + deliverable zip"
            print(f"[{idx+1}] {key} DONE: {len(transcripts)} transcripts, "
                  f"{len(reports)} token reports, labels seen {sorted(seen_labels)}, "
                  f"{pr_ref} merged ✓", flush=True)
            return
        if state == "canceled" or "DEVCAKE-FAILED" in stage:
            sys.exit(f"[{idx+1}] {key} FAILED: state={state} labels={sorted(stage)}")
        time.sleep(30)
    sys.exit(f"[{idx+1}] {key} TIMEOUT after 45min (labels seen: {sorted(seen_labels)})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--forge", choices=("github", "gitlab", "gitea"),
                    default="github", help="Forge under test. 'gitea' is the "
                    "zero-repo internal-forge lane (no external forge "
                    "credentials)")
    ap.add_argument("--team", default=None,
                    help="Linear team key to seed the mission in "
                         "(default: DEVCAKE_TEAM_KEY from .env)")
    ap.add_argument("--instance", default=None,
                    help="DevCake PMO-instance name watching --team "
                         "(default: linear) — sets the branch/repo prefix")
    ap.add_argument("--api-key-env", default=None,
                    help="env-var NAME (read from .env/shell) holding the "
                         "Linear key (default: LINEAR_API_KEY)")
    ap.add_argument("--repo-url", default=None,
                    help="repo the merged PR is asserted in "
                         "(default: DEVCAKE_REPO_URL from .env; unused for gitea)")
    args = ap.parse_args()
    FORGE = args.forge
    if args.api_key_env:
        KEY = env(args.api_key_env)
    if args.team:
        TEAM = args.team
    if args.instance:
        INSTANCE = args.instance
    if args.repo_url:
        REPO_URL = args.repo_url
    if not KEY or not TEAM:
        sys.exit("need a Linear key and team: set LINEAR_API_KEY + "
                 "DEVCAKE_TEAM_KEY (or pass --api-key-env/--team). Tester "
                 "credentials come from the shell/.env, never DevCake's "
                 "stored secrets.")
    if FORGE != "gitea" and not REPO_URL:
        sys.exit(f"--forge {FORGE} needs --repo-url or DEVCAKE_REPO_URL")
    for i in range(args.runs):
        run_once(i)
    print(f"ACCEPTANCE GREEN: {args.runs}/{args.runs} unattended golden paths "
          f"({FORGE})", flush=True)
