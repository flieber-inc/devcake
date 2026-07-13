"""PMO adapter contract battery, tests 1–5 & 8–10 (docs/05 §7) — M2 exit criterion.

Runs INSIDE the app container (imports devcake.*), against the live sandbox team:
    docker compose exec -T app python - < scripts/contract_tests_pmo.py
Creates temp issues prefixed [CONTRACT] and deletes them afterwards.
"""

import asyncio
import os

import httpx

from devcake.config import load_config
from devcake.adapters.linear.adapter import LinearAdapter, PMOTransient
from devcake.domain.model import ALL_LABELS

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(num: str, name: str, ok: bool, note: str = "") -> None:
    results.append((num, name, PASS if ok else FAIL + (" — " + note if note else "")))


async def make_temp_issue(pmo, team, title, label_names):
    t = await pmo._team(team)
    by_name = {l["name"].upper(): l["id"] for l in t["labels"]["nodes"]}
    data = await pmo._gql(
        """mutation($input: IssueCreateInput!) {
             issueCreate(input: $input) { issue { id } } }""",
        {"input": {"teamId": t["id"], "title": title,
                   "labelIds": [by_name[n] for n in label_names]}})
    return data["issueCreate"]["issue"]["id"]


async def delete_issue(pmo, issue_id):
    await pmo._gql("""mutation($id: String!) { issueDelete(id: $id) { success } }""",
                   {"id": issue_id})


async def main():
    cfg = load_config()
    team = cfg.pmo.team_key
    pmo = LinearAdapter(cfg.api_key)

    # 1 — team scoping + terminal exclusion
    missions = await pmo.list_missions(team)
    ok = all(m.key.startswith(f"{team}-") or m.pmo_kind == "project" for m in missions) \
        and all(m.status not in ("done", "canceled") for m in missions)
    check("1", "list_missions scoped to team, terminal excluded", ok)

    # 2 — status normalization round-trips (temp issue)
    tid = await make_temp_issue(pmo, team, "[CONTRACT] status round-trip", ["DEVCAKE"])
    ok2, note = True, ""
    try:
        for status in ("in_progress", "done", "canceled", "backlog"):
            await pmo.set_status(tid, status)
            got = (await pmo.get_mission(tid)).status
            if got != status:
                ok2, note = False, f"{status} → {got}"
                break
    finally:
        pass  # reused below
    check("2", "status normalization round-trips all four", ok2, note)

    # 4 — swap_labels: remove+add in one observable step (reuse temp issue)
    await pmo.swap_labels(tid, remove=set(), add={"DEVCAKE-PLAN"})
    await pmo.swap_labels(tid, remove={"DEVCAKE-PLAN"}, add={"DEVCAKE-EXECUTE"})
    labels = (await pmo.get_mission(tid)).labels
    check("4", "swap_labels atomic remove+add, others preserved",
          "DEVCAKE-EXECUTE" in labels and "DEVCAKE-PLAN" not in labels
          and "DEVCAKE" in labels, str(labels))

    # 8 — activity ordering + attachment extraction (reuse temp issue)
    await pmo.post_comment(tid, "first comment")
    await asyncio.sleep(1.2)
    await pmo.post_comment(
        tid, "second, with file: https://uploads.linear.app/fake/asset-abc123.md")
    act = await pmo.get_activity(tid)
    bodies = [e.body.split(",")[0] for e in act.entries]
    atts = [u for e in act.entries for u in e.attachments]
    check("8", "get_activity chronological + attachments extracted",
          bodies == ["first comment", "second"] and len(atts) == 1, f"{bodies} {atts}")
    await delete_issue(pmo, tid)

    # 3 — priority normalization incl. unset→medium (fixtures from seed_sandbox)
    all_missions = await pmo.list_all(team)
    by_title = {m.title: m for m in all_missions}
    r1 = by_title.get("[FIXTURE] row1: fresh adopted issue → ONBOARD")
    r9 = by_title.get("[FIXTURE] row9: in-progress without stage label")
    check("3", "priority urgent + unset→medium",
          r1 is not None and r1.priority == "urgent"
          and r9 is not None and r9.priority == "medium",
          f"{r1 and r1.priority} {r9 and r9.priority}")

    # 5 — ensure_labels idempotent & case-insensitive
    t = await pmo._team(team)
    before = sorted(l["name"] for l in t["labels"]["nodes"] if l["name"].upper() in ALL_LABELS)
    await pmo.ensure_labels(team, ALL_LABELS)          # second run
    await pmo.ensure_labels(team, {"devcake-plan"})    # lowercase → must match existing
    t = await pmo._team(team)
    after = sorted(l["name"] for l in t["labels"]["nodes"] if l["name"].upper() in ALL_LABELS)
    check("5", "ensure_labels idempotent + case-insensitive",
          before == after and len(after) == len(ALL_LABELS),
          f"{len(before)}→{len(after)}")

    # 9 — 429 → PMO_TRANSIENT (mock transport; never hits the network)
    mock = httpx.MockTransport(lambda req: httpx.Response(429, json={}))
    limited = LinearAdapter("fake-key", transport=mock)
    try:
        await limited._gql("query { viewer { id } }")
        check("9", "429 surfaces as PMO_TRANSIENT", False, "no exception")
    except PMOTransient:
        check("9", "429 surfaces as PMO_TRANSIENT", True)
    except Exception as e:
        check("9", "429 surfaces as PMO_TRANSIENT", False, repr(e))

    # 10 — project normalization + truthful capabilities
    proj = next((m for m in all_missions if m.pmo_kind == "project"), None)
    caps = pmo.capabilities()
    check("10", "project normalized (status/labels/key) + capabilities",
          proj is not None and proj.key.startswith("PRJ-")
          and proj.status == "backlog" and "DEVCAKE" in proj.labels
          and caps.projects_supported and caps.project_labels_supported
          and caps.native_label_swap_atomic,
          proj and f"{proj.key} {proj.status} {sorted(proj.labels)}")

    width = max(len(n) for _, n, _ in results)
    failures = 0
    for num, name, res in results:
        print(f"  test {num:>2}  {name:<{width}}  {res}")
        failures += res != PASS
    print(f"\n{len(results) - failures}/{len(results)} passed")
    raise SystemExit(1 if failures else 0)


asyncio.run(main())
