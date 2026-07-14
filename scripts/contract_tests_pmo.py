"""PMO adapter contract battery — the adapter ACCEPTANCE GATE (docs/05 §0+§7).

Tests 1–5 & 8–10 (M2 exit criterion) + 11–13 (M9: cancel_mission idempotency,
marker/markdown fidelity, attachment round-trip). Exit code is the verdict.

Runs INSIDE the app container (imports devcake.*), against a live team:
    docker compose exec -T app python - < scripts/contract_tests_pmo.py
Instance selection (schema v3): set DEVCAKE_CONTRACT_INSTANCE=<name> in the
exec env (default: the first configured instance). Run once per instance —
the additivity proof runs it against both sandbox teams.
Creates temp issues prefixed [CONTRACT] and deletes them afterwards.
"""

import asyncio
import os

import httpx

from devcake.config import load_config
from devcake.adapters.linear.adapter import LinearAdapter
from devcake.domain.model import MissionRef
from devcake.ports.pmo import PMOTransient
from devcake.domain.model import ALL_LABELS

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(num: str, name: str, ok: bool, note: str = "") -> None:
    results.append((num, name, PASS if ok else FAIL + (" — " + note if note else "")))


# NOTE: _team/_gql below are adapter-internal fixture plumbing (issue create/
# delete has no port method and should not grow one for tests' sake). This
# battery is Linear-specific by design; only port methods are contract-tested.
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
    wanted = os.environ.get("DEVCAKE_CONTRACT_INSTANCE", "")
    inst = next((i for i in cfg.pmos if i.name == wanted), None) if wanted \
        else next((i for i in cfg.pmos if i.configured), None)
    if inst is None:
        raise SystemExit(f"no configured PMO instance"
                         f"{f' named {wanted!r}' if wanted else ''}")
    print(f"instance: {inst.name} (team {inst.team_key})")
    team = inst.team_key
    pmo = LinearAdapter(inst.api_key, instance=inst.name)

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
            await pmo.set_status(MissionRef(tid, "issue"), status)
            got = (await pmo.get(MissionRef(tid, "issue"))).status
            if got != status:
                ok2, note = False, f"{status} → {got}"
                break
    finally:
        pass  # reused below
    check("2", "status normalization round-trips all four", ok2, note)

    # 4 — swap_labels: remove+add in one observable step (reuse temp issue)
    await pmo.swap_labels(MissionRef(tid, "issue"), remove=set(), add={"DEVCAKE-PLAN"})
    await pmo.swap_labels(MissionRef(tid, "issue"), remove={"DEVCAKE-PLAN"}, add={"DEVCAKE-EXECUTE"})
    labels = (await pmo.get(MissionRef(tid, "issue"))).labels
    check("4", "swap_labels atomic remove+add, others preserved",
          "DEVCAKE-EXECUTE" in labels and "DEVCAKE-PLAN" not in labels
          and "DEVCAKE" in labels, str(labels))

    # 8 — activity ordering + attachment extraction (reuse temp issue)
    await pmo.post_feed(MissionRef(tid, "issue"), "first comment")
    await asyncio.sleep(1.2)
    await pmo.post_feed(
        MissionRef(tid, "issue"),
        "second, with file: https://uploads.linear.app/fake/asset-abc123.md")
    act = await pmo.get_activity(MissionRef(tid, "issue"))
    bodies = [e.body.split(",")[0] for e in act.entries]
    atts = [a.url for e in act.entries for a in e.attachments]
    check("8", "get_activity chronological + attachments extracted",
          bodies == ["first comment", "second"] and len(atts) == 1, f"{bodies} {atts}")
    await delete_issue(pmo, tid)

    # 3 — priority normalization incl. unset→medium (self-sufficient since
    # M9: the M2 seed_sandbox fixtures no longer exist in the live team)
    all_missions = await pmo.list_all(team)
    t3 = await pmo._team(team)
    p_urgent = await pmo._gql(
        """mutation($input: IssueCreateInput!) {
             issueCreate(input: $input) { issue { id } } }""",
        {"input": {"teamId": t3["id"], "title": "[CONTRACT] urgent priority",
                   "priority": 1}})
    uid = p_urgent["issueCreate"]["issue"]["id"]
    nid = await make_temp_issue(pmo, team, "[CONTRACT] unset priority", ["DEVCAKE"])
    try:
        mu = await pmo.get(MissionRef(uid, "issue"))
        mn = await pmo.get(MissionRef(nid, "issue"))
        check("3", "priority urgent + unset→medium",
              mu.priority == "urgent" and mn.priority == "medium",
              f"{mu.priority} {mn.priority}")
    finally:
        await delete_issue(pmo, uid)
        await delete_issue(pmo, nid)

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

    # 5b — health_probe: the public replacement for _team reach-ins
    h = await pmo.health_probe(team)
    check("5b", "health_probe ok + managed labels all present",
          h.ok and h.managed_labels_present == h.managed_labels_expected == len(ALL_LABELS),
          f"{h.managed_labels_present}/{h.managed_labels_expected}")

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

    # 11 — cancel_mission: terminal + idempotent (docs/05 §0d)
    cid = await make_temp_issue(pmo, team, "[CONTRACT] cancel idempotency", ["DEVCAKE"])
    ok11, note11 = True, ""
    try:
        ref = MissionRef(cid, "issue")
        await pmo.cancel_mission(ref)
        m1 = await pmo.get(ref)
        await pmo.cancel_mission(ref)          # second cancel must be success
        m2 = await pmo.get(ref)
        ok11 = m1.status == "canceled" and m2.status == "canceled"
        note11 = f"{m1.status}/{m2.status}"
    except Exception as e:
        ok11, note11 = False, str(e)[:120]
    finally:
        await delete_issue(pmo, cid)
    check("11", "cancel_mission terminal + idempotent", ok11, note11)

    # 12 — marker/markdown fidelity: backticked sentinels survive round-trip
    fid = await make_temp_issue(pmo, team, "[CONTRACT] marker fidelity", ["DEVCAKE"])
    ok12, note12 = True, ""
    try:
        marker = "state check `devcake:v1` and `devcake:conflict-resolve:2` intact"
        await pmo.post_feed(MissionRef(fid, "issue"), marker)
        act = await pmo.get_activity(MissionRef(fid, "issue"))
        ok12 = any("`devcake:v1`" in e.body and "`devcake:conflict-resolve:2`" in e.body
                   for e in act.entries)
        note12 = "" if ok12 else "backticked marker bytes did not round-trip"
    except Exception as e:
        ok12, note12 = False, str(e)[:120]
    finally:
        await delete_issue(pmo, fid)
    check("12", "post_feed marker fidelity (docs/05 §0d)", ok12, note12)

    # 13 — attachment upload → download round-trip
    aid = await make_temp_issue(pmo, team, "[CONTRACT] attachment round-trip", ["DEVCAKE"])
    ok13, note13 = True, ""
    try:
        payload = b"contract-battery attachment \xf0\x9f\x8d\xb0 bytes"
        url = await pmo.upload_attachment(aid, "contract.md", payload)
        got = await pmo.download_asset(url)
        ok13 = got == payload
        note13 = "" if ok13 else f"{len(got)} bytes back, {len(payload)} sent"
    except Exception as e:
        ok13, note13 = False, str(e)[:120]
    finally:
        await delete_issue(pmo, aid)
    check("13", "attachment upload/download round-trip", ok13, note13)

    width = max(len(n) for _, n, _ in results)
    failures = 0
    for num, name, res in results:
        print(f"  test {num:>2}  {name:<{width}}  {res}")
        failures += res != PASS
    print(f"\n{len(results) - failures}/{len(results)} passed")
    raise SystemExit(1 if failures else 0)


asyncio.run(main())
