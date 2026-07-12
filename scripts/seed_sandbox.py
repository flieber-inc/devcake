#!/usr/bin/env python3
"""Seed the sandbox Linear team with fixtures covering EVERY derivation-table row
(docs/02 §2) — M2 exit criterion. Idempotent: fixtures are found by exact title.

Standalone (stdlib only). Reads LINEAR_API_KEY + DEVCAKE_TEAM_KEY from .env/env.
Run AFTER the app has bootstrapped the ten labels (or run the app once first).
"""

import json
import os
import sys
import urllib.request

API = "https://api.linear.app/graphql"


def env(name: str) -> str:
    if name in os.environ:
        return os.environ[name]
    try:
        for line in open(".env"):
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    sys.exit(f"missing {name} (env or .env)")


KEY = env("LINEAR_API_KEY")
TEAM = os.environ.get("DEVCAKE_TEAM_KEY") or env("DEVCAKE_TEAM_KEY")


def gql(query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request(
        API, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": KEY, "Content-Type": "application/json"})
    body = json.load(urllib.request.urlopen(req))
    if body.get("errors"):
        sys.exit(f"graphql error: {body['errors']}")
    return body["data"]


# split queries — Linear enforces a per-query complexity budget
team = gql("""query($key: String!) { teams(filter: {key: {eq: $key}}) { nodes {
    id key
    states { nodes { id name type position } }
    labels(first: 100) { nodes { id name } }
} } }""", {"key": TEAM})["teams"]["nodes"][0]
issues = gql("""query($teamId: ID!) {
    issues(first: 100, filter: {team: {id: {eq: $teamId}}}) { nodes { id title } } }""",
    {"teamId": team["id"]})["issues"]["nodes"]
projects = gql("""query($teamId: ID!) {
    projects(first: 50, filter: {accessibleTeams: {id: {eq: $teamId}}}) { nodes { id name } } }""",
    {"teamId": team["id"]})["projects"]["nodes"]

labels = {l["name"].upper(): l["id"] for l in team["labels"]["nodes"]}
states = sorted(team["states"]["nodes"], key=lambda s: s.get("position") or 0)
state_of = {t: next(s["id"] for s in states if s["type"] == t)
            for t in ("backlog", "started", "completed", "canceled")}
existing = {i["title"] for i in issues}

missing = [n for n in ("DEVCAKE", "DEVCAKE-PLAN", "DEVCAKE-EXECUTE", "DEVCAKE-REVIEW",
                       "DEVCAKE-MERGE", "DEVCAKE-SKIP", "DEVCAKE-FAILED") if n not in labels]
if missing:
    sys.exit(f"labels not bootstrapped yet (missing {missing}) — start the app once first")

# (title, state, labels, priority) — one fixture per derivation row + extras
FIXTURES = [
    ("[FIXTURE] row1: fresh adopted issue → ONBOARD", "backlog", ["DEVCAKE"], 1),          # urgent
    ("[FIXTURE] row2: planned → PLAN", "backlog", ["DEVCAKE", "DEVCAKE-PLAN"], 2),
    ("[FIXTURE] row3: executing → EXECUTE", "started", ["DEVCAKE", "DEVCAKE-EXECUTE"], 3),
    ("[FIXTURE] row4: reviewing → REVIEW", "started", ["DEVCAKE", "DEVCAKE-REVIEW"], 4),
    ("[FIXTURE] row5: completed → terminal ignored", "completed", ["DEVCAKE"], 0),
    ("[FIXTURE] row6: two stage labels → conflict", "backlog",
     ["DEVCAKE", "DEVCAKE-PLAN", "DEVCAKE-EXECUTE"], 0),
    ("[FIXTURE] row7: skipped → SKIP", "backlog", ["DEVCAKE", "DEVCAKE-SKIP"], 0),
    ("[FIXTURE] row8: failed → FAILED", "backlog", ["DEVCAKE", "DEVCAKE-FAILED"], 0),
    ("[FIXTURE] row9: in-progress without stage label", "started", ["DEVCAKE"], 0),        # →medium
    ("[FIXTURE] row10: awaiting merge → MERGE sweep", "started", ["DEVCAKE", "DEVCAKE-MERGE"], 0),
    ("[FIXTURE] gate: unlabeled → not adopted in opt-in", "backlog", [], 0),
]

for title, state, names, priority in FIXTURES:
    if title in existing:
        print(f"= exists  {title}")
        continue
    gql("""mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success } }""",
        {"input": {"teamId": team["id"], "title": title,
                   "description": "DevCake M2 derivation fixture. Safe to delete.",
                   "stateId": state_of[state], "priority": priority,
                   "labelIds": [labels[n] for n in names]}})
    print(f"+ created {title}")

# Project fixture (ADR-0006 / derivation over projects)
PROJECT = "[FIXTURE] Project: adopted project"
if not any(p["name"] == PROJECT for p in projects):
    proj = gql("""mutation($input: ProjectCreateInput!) {
        projectCreate(input: $input) { success project { id } } }""",
        {"input": {"teamIds": [team["id"]], "name": PROJECT,
                   "description": "DevCake M2 project fixture. Safe to delete."}}
        )["projectCreate"]["project"]
    try:
        gql("""mutation($id: String!, $labelIds: [String!]!) {
            projectUpdate(id: $id, input: {labelIds: $labelIds}) { success } }""",
            {"id": proj["id"], "labelIds": [labels["DEVCAKE"]]})
        print(f"+ created {PROJECT} (with DEVCAKE label)")
    except SystemExit:
        print(f"+ created {PROJECT} (label attach failed — check project-label API)")
else:
    print(f"= exists  {PROJECT}")

print("seeding done.")
