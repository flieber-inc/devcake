"""PMO adapter contract battery — the adapter ACCEPTANCE GATE (docs/05 §0+§7).

System-agnostic: builds the adapter via ``make_pmo``. Fixture create/cleanup
use **only** port methods (``create_mission`` / ``cancel_mission``).

Tests 1–5 & 8–10 (M2) + 11–14 (cancel, markers, attachments, relations).
Forge-issue systems (``gitea_issues``) use the documented profile variants
(open→backlog; no projects; priority always medium).

Runs INSIDE the app container:
    docker compose exec -T app python - < scripts/contract_tests_pmo.py

Instance selection (first match wins):
  1. ``DEVCAKE_CONTRACT_INSTANCE=<name>`` — configured PMO from config.yaml
  2. Direct harness:
       DEVCAKE_CONTRACT_SYSTEM=gitea_issues|linear|gitlab_issues
       DEVCAKE_CONTRACT_TEAM=…  DEVCAKE_CONTRACT_TOKEN=…
       DEVCAKE_CONTRACT_API_BASE=… (gitea_issues)
  3. **Default CI lane** — when ``GITEA_ADMIN_USER`` + ``GITEA_ADMIN_PASSWORD``
     are set (app container / ci_suite): auto-provision a scratch Gitea Issues
     board, run the battery, tear it down (same posture as
     ``contract_tests_forge.py``; zero external tokens; wired into ci_suite.sh).
"""

from __future__ import annotations

import asyncio
import os
import secrets
from types import SimpleNamespace

import httpx

from devcake.adapters.registry import make_pmo
from devcake.config import load_config
from devcake.domain.model import ALL_LABELS, MissionRef
from devcake.ports.pmo import PMOTransient

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []

GITEA_URL = os.environ.get("DEVCAKE_CONTRACT_GITEA_URL", "http://gitea:3000")


def check(num: str, name: str, ok: bool, note: str = "", *,
          skipped: bool = False) -> None:
    if skipped:
        results.append((num, name, SKIP + (" — " + note if note else "")))
        return
    suffix = (" — " + note) if note else ""
    results.append((num, name, (PASS if ok else FAIL) + ("" if ok else suffix)))


async def make_temp_issue(pmo, team: str, title: str,
                          label_names: set[str] | list[str],
                          *, priority: str = "medium",
                          description: str = "") -> str:
    """Create via the port only. Returns pmo_id."""
    _key, pmo_id = await pmo.create_mission(
        team, title, description, priority, set(label_names))
    return pmo_id


async def cleanup_issue(pmo, pmo_id: str) -> None:
    """Best-effort terminalize — no delete on the port (Linear used private
    issueDelete; cancel leaves a durable canceled mission, which is fine)."""
    try:
        await pmo.cancel_mission(MissionRef(pmo_id, "issue"))
    except Exception as e:  # noqa: BLE001 — cleanup must not hide battery failures
        print(f"  cleanup warn {pmo_id}: {e}")


# ── gitea_issues fixture (admin API; bundled instance — ci_suite default) ──


class GiteaPmoFixture:
    """Scratch issues board for the gitea_issues PMO lane.

    Mirrors ``contract_tests_forge.GiteaFixture``: mint under admin, enable
    issue dependencies, tear down repo + token after the battery.
    """

    def __init__(self) -> None:
        self.admin = (os.environ["GITEA_ADMIN_USER"],
                      os.environ["GITEA_ADMIN_PASSWORD"])
        self.suffix = secrets.token_hex(3)
        self.repo = f"pmo-contract-{self.suffix}"
        self.token_name = f"pmo-contract-{self.suffix}"
        self.token = ""
        self.team = ""

    def _req(self, method: str, path: str, **kw):
        r = httpx.request(method, f"{GITEA_URL}/api/v1{path}",
                          auth=self.admin, timeout=20, **kw)
        if r.status_code >= 400:
            raise RuntimeError(
                f"{method} {path} → {r.status_code}: {r.text[:200]}")
        return r.json() if r.text else None

    def up(self) -> SimpleNamespace:
        admin_user = self.admin[0]
        self._req("POST", "/user/repos",
                  json={"name": self.repo, "private": True, "auto_init": True,
                        "default_branch": "main"})
        # dependencies off by default — enable before the adapter's first write
        self._req("PATCH", f"/repos/{admin_user}/{self.repo}",
                  json={"internal_tracker": {
                      "enable_time_tracker": False,
                      "allow_only_contributors_to_track_time": False,
                      "enable_issue_dependencies": True,
                  }})
        self.token = self._req(
            "POST", f"/users/{admin_user}/tokens",
            json={"name": self.token_name,
                  "scopes": ["write:issue", "write:repository"]})["sha1"]
        self.team = f"{admin_user}/{self.repo}"
        return SimpleNamespace(
            name="contract",
            system="gitea_issues",
            team_key=self.team,
            api_base=GITEA_URL,
            api_key=self.token,
        )

    def down(self) -> None:
        admin_user = self.admin[0]
        for method, path in (
                ("DELETE", f"/repos/{admin_user}/{self.repo}"),
                ("DELETE", f"/users/{admin_user}/tokens/{self.token_name}"),
        ):
            try:
                self._req(method, path)
            except Exception as e:  # noqa: BLE001 — teardown best-effort
                print(f"cleanup {path}: {e}")


def resolve_instance():
    """Return (inst_like, system, team, source_label, fixture_or_None)."""
    # 1 — explicit config instance
    wanted = os.environ.get("DEVCAKE_CONTRACT_INSTANCE", "").strip()
    if wanted:
        cfg = load_config()
        inst = next((i for i in cfg.pmos if i.name == wanted), None)
        if inst is None:
            raise SystemExit(f"no PMO instance named {wanted!r}")
        if not inst.configured or not inst.api_key:
            raise SystemExit(
                f"PMO instance {wanted!r} is idle or has no stored API key")
        return inst, inst.system, inst.team_key, f"config:{inst.name}", None

    # 2 — direct env harness
    direct_system = os.environ.get("DEVCAKE_CONTRACT_SYSTEM", "").strip()
    if direct_system:
        team = os.environ.get("DEVCAKE_CONTRACT_TEAM", "").strip()
        api_base = os.environ.get("DEVCAKE_CONTRACT_API_BASE") or None
        token = os.environ.get("DEVCAKE_CONTRACT_TOKEN", "")
        if not team or not token:
            raise SystemExit(
                "direct harness needs DEVCAKE_CONTRACT_TEAM and "
                "DEVCAKE_CONTRACT_TOKEN")
        inst = SimpleNamespace(
            name="contract",
            system=direct_system,
            team_key=team,
            api_base=api_base,
            api_key=token,
        )
        return inst, direct_system, team, f"direct:{direct_system}", None

    # 3 — default CI lane: auto-provision gitea_issues (needs GITEA_ADMIN_*).
    # Prefer this over "first configured PMO" so ci_suite is deterministic even
    # when a Linear instance is present in the operator's config.yaml.
    if os.environ.get("GITEA_ADMIN_USER") and os.environ.get("GITEA_ADMIN_PASSWORD"):
        fixture = GiteaPmoFixture()
        inst = fixture.up()
        return (inst, "gitea_issues", inst.team_key,
                f"auto:gitea_issues:{inst.team_key}", fixture)

    # 4 — first configured config instance (no bundled Gitea available)
    try:
        cfg = load_config()
        inst = next((i for i in cfg.pmos if i.configured and i.api_key), None)
        if inst is not None:
            return (inst, inst.system, inst.team_key,
                    f"config:{inst.name}", None)
    except Exception:  # noqa: BLE001 — fall through to error
        pass

    raise SystemExit(
        "no PMO contract target: set DEVCAKE_CONTRACT_INSTANCE, or the "
        "direct harness env vars, or run with GITEA_ADMIN_* (ci_suite default)")


def make_429_probe(system: str, team: str):
    """Adapter instance whose every HTTP call 429s — for test 9."""
    mock = httpx.MockTransport(lambda req: httpx.Response(429, json={}))
    if system == "linear":
        from devcake.adapters.linear.adapter import LinearAdapter
        return LinearAdapter("fake-key", transport=mock, instance="contract")
    if system == "gitea_issues":
        from devcake.adapters.gitea_issues import GiteaIssuesAdapter
        return GiteaIssuesAdapter(
            GITEA_URL, "fake-token", team or "o/r",
            instance="contract", transport=mock)
    if system == "gitlab_issues":
        from devcake.adapters.gitlab_issues import GitLabIssuesAdapter
        return GitLabIssuesAdapter(
            "https://gitlab.com", "fake-token", team or "o/r",
            instance="contract", transport=mock)
    raise SystemExit(f"no 429 probe for system {system!r}")


async def main() -> None:
    fixture = None
    code = 1
    try:
        inst, system, team, source, fixture = resolve_instance()
        print(f"instance: {source} system={system} team={team}")
        pmo = make_pmo(inst)
        code = await _run_battery(pmo, system, team)
    finally:
        if fixture is not None:
            fixture.down()
    raise SystemExit(code)


async def _run_battery(pmo, system: str, team: str) -> int:
    """Run the battery; return process exit code (0 = green)."""
    caps = pmo.capabilities()
    forge_issue = not caps.projects_supported

    # ── 1 team scoping + terminal exclusion ───────────────────────────────
    missions = await pmo.list_missions(team)
    if forge_issue:
        # key is owner/repo#N — team_key is owner/repo
        scoped = all(
            m.key.startswith(f"{team}#") and m.pmo_kind == "issue"
            for m in missions)
    else:
        scoped = all(
            m.key.startswith(f"{team}-") or m.pmo_kind == "project"
            for m in missions)
    terminals_out = all(m.status not in ("done", "canceled") for m in missions)
    check("1", "list_missions scoped to team, terminal excluded",
          scoped and terminals_out,
          f"n={len(missions)} scoped={scoped} terminals_ok={terminals_out}")

    # ── 2 status normalization ────────────────────────────────────────────
    tid = await make_temp_issue(pmo, team, "[CONTRACT] status round-trip",
                                {"DEVCAKE"})
    ok2, note2 = True, ""
    try:
        if forge_issue:
            # open always → backlog; done/canceled are closed variants
            sequence = [
                ("in_progress", "backlog"),  # no vendor in_progress
                ("done", "done"),
                ("canceled", "canceled"),
                ("backlog", "backlog"),
            ]
        else:
            sequence = [
                ("in_progress", "in_progress"),
                ("done", "done"),
                ("canceled", "canceled"),
                ("backlog", "backlog"),
            ]
        for write, expect in sequence:
            await pmo.set_status(MissionRef(tid, "issue"), write)
            got = (await pmo.get(MissionRef(tid, "issue"))).status
            if got != expect:
                ok2, note2 = False, f"{write} → {got} (want {expect})"
                break
    finally:
        await cleanup_issue(pmo, tid)
    check("2", "status normalization round-trips (profile-aware)", ok2, note2)

    # ── 4 swap_labels ─────────────────────────────────────────────────────
    tid = await make_temp_issue(pmo, team, "[CONTRACT] swap labels", {"DEVCAKE"})
    ok4, note4 = True, ""
    try:
        ref = MissionRef(tid, "issue")
        await pmo.swap_labels(ref, remove=set(), add={"DEVCAKE-PLAN"})
        await pmo.swap_labels(ref, remove={"DEVCAKE-PLAN"}, add={"DEVCAKE-EXECUTE"})
        labels = (await pmo.get(ref)).labels
        ok4 = ("DEVCAKE-EXECUTE" in labels and "DEVCAKE-PLAN" not in labels
               and "DEVCAKE" in labels)
        note4 = str(sorted(labels))
    except Exception as e:
        ok4, note4 = False, str(e)[:160]
    finally:
        await cleanup_issue(pmo, tid)
    check("4", "swap_labels atomic remove+add, others preserved", ok4, note4)

    # ── 8 activity ordering + attachment extraction ───────────────────────
    tid = await make_temp_issue(pmo, team, "[CONTRACT] activity order",
                                {"DEVCAKE"})
    ok8, note8 = True, ""
    try:
        ref = MissionRef(tid, "issue")
        await pmo.post_feed(ref, "first comment")
        await asyncio.sleep(1.2)
        if forge_issue and caps.attachments_supported:
            # Real upload → named markdown link (ROOT_URL may differ from api_base;
            # extraction rewrites host for downloads; body still carries a URL).
            asset_url = await pmo.upload_attachment(
                tid, "contract-asset.md", b"asset-body")
            await pmo.post_feed(
                ref, f"second, with file: [contract-asset.md]({asset_url})")
        else:
            await pmo.post_feed(
                ref,
                "second, with file: https://uploads.linear.app/fake/asset-abc123.md")
        act = await pmo.get_activity(ref)
        bodies = [e.body.split(",")[0] for e in act.entries
                  if e.body.startswith("first") or e.body.startswith("second")]
        atts = [a.url for e in act.entries for a in e.attachments]
        need_atts = (not forge_issue) or caps.attachments_supported
        ok8 = (bodies == ["first comment", "second"]
               and (len(atts) >= 1 if need_atts else True))
        note8 = f"{bodies} atts={len(atts)}"
    except Exception as e:
        ok8, note8 = False, str(e)[:160]
    finally:
        await cleanup_issue(pmo, tid)
    check("8", "get_activity chronological + attachments extracted", ok8, note8)

    # ── 3 priority ────────────────────────────────────────────────────────
    uid = await make_temp_issue(
        pmo, team, "[CONTRACT] urgent priority", {"DEVCAKE"}, priority="urgent")
    nid = await make_temp_issue(
        pmo, team, "[CONTRACT] unset priority", {"DEVCAKE"}, priority="medium")
    try:
        mu = await pmo.get(MissionRef(uid, "issue"))
        mn = await pmo.get(MissionRef(nid, "issue"))
        if forge_issue:
            ok3 = mu.priority == "medium" and mn.priority == "medium"
            note3 = f"forge-issue always medium: {mu.priority}/{mn.priority}"
        else:
            ok3 = mu.priority == "urgent" and mn.priority == "medium"
            note3 = f"{mu.priority}/{mn.priority}"
        check("3", "priority urgent + medium (profile-aware)", ok3, note3)
    finally:
        await cleanup_issue(pmo, uid)
        await cleanup_issue(pmo, nid)

    # ── 5 ensure_labels ───────────────────────────────────────────────────
    try:
        await pmo.ensure_labels(team, ALL_LABELS)
        await pmo.ensure_labels(team, ALL_LABELS)       # second run
        await pmo.ensure_labels(team, {"devcake-plan"})  # case fold
        h5 = await pmo.health_probe(team)
        check("5", "ensure_labels idempotent + case-insensitive",
              h5.ok and h5.managed_labels_present == len(ALL_LABELS),
              f"{h5.managed_labels_present}/{h5.managed_labels_expected}")
    except Exception as e:
        check("5", "ensure_labels idempotent + case-insensitive",
              False, str(e)[:160])

    # ── 5b health_probe ───────────────────────────────────────────────────
    h = await pmo.health_probe(team)
    check("5b", "health_probe ok + managed labels all present",
          h.ok and h.managed_labels_present == h.managed_labels_expected
          == len(ALL_LABELS),
          f"{h.managed_labels_present}/{h.managed_labels_expected} {h.detail}")

    # ── 9 429 → PMOTransient ──────────────────────────────────────────────
    limited = make_429_probe(system, team)
    try:
        await limited.list_all(team)
        check("9", "429 surfaces as PMO_TRANSIENT", False, "no exception")
    except PMOTransient:
        check("9", "429 surfaces as PMO_TRANSIENT", True)
    except Exception as e:
        check("9", "429 surfaces as PMO_TRANSIENT", False, repr(e))

    # ── 10 projects / capabilities ────────────────────────────────────────
    all_missions = await pmo.list_all(team)
    if forge_issue:
        check("10", "capabilities truthful (issue-only forge-issue)",
              (not caps.projects_supported
               and not caps.project_labels_supported
               and caps.native_label_swap_atomic
               and all(m.pmo_kind == "issue" for m in all_missions)),
              f"projects={caps.projects_supported} "
              f"relations={caps.relations_supported} n={len(all_missions)}")
    else:
        proj = next((m for m in all_missions if m.pmo_kind == "project"), None)
        check("10", "project normalized + capabilities",
              (proj is not None and proj.key.startswith("PRJ-")
               and proj.status in ("backlog", "in_progress", "done", "canceled")
               and caps.projects_supported and caps.project_labels_supported
               and caps.native_label_swap_atomic),
              proj and f"{proj.key} {proj.status} {sorted(proj.labels)}")

    # ── 11 cancel_mission ─────────────────────────────────────────────────
    cid = await make_temp_issue(pmo, team, "[CONTRACT] cancel idempotency",
                                {"DEVCAKE"})
    ok11, note11 = True, ""
    try:
        ref = MissionRef(cid, "issue")
        await pmo.cancel_mission(ref)
        m1 = await pmo.get(ref)
        await pmo.cancel_mission(ref)
        m2 = await pmo.get(ref)
        ok11 = m1.status == "canceled" and m2.status == "canceled"
        note11 = f"{m1.status}/{m2.status}"
    except Exception as e:
        ok11, note11 = False, str(e)[:160]
    # already canceled — no further cleanup needed
    check("11", "cancel_mission terminal + idempotent", ok11, note11)

    # ── 12 marker/markdown fidelity ───────────────────────────────────────
    fid = await make_temp_issue(pmo, team, "[CONTRACT] marker fidelity",
                                {"DEVCAKE"})
    ok12, note12 = True, ""
    try:
        marker = ("state check `devcake:v1` and "
                  "`devcake:conflict-resolve:2` intact")
        await pmo.post_feed(MissionRef(fid, "issue"), marker)
        act = await pmo.get_activity(MissionRef(fid, "issue"))
        ok12 = any(
            "`devcake:v1`" in e.body
            and "`devcake:conflict-resolve:2`" in e.body
            for e in act.entries)
        note12 = "" if ok12 else "backticked marker bytes did not round-trip"
    except Exception as e:
        ok12, note12 = False, str(e)[:160]
    finally:
        await cleanup_issue(pmo, fid)
    check("12", "post_feed marker fidelity (docs/05 §0d)", ok12, note12)

    # ── 13 attachment upload/download ─────────────────────────────────────
    if not getattr(caps, "attachments_supported", True):
        check("13", "attachment upload/download round-trip",
              True, "attachments_supported=False", skipped=True)
    else:
        aid = await make_temp_issue(pmo, team, "[CONTRACT] attachment round-trip",
                                    {"DEVCAKE"})
        ok13, note13 = True, ""
        try:
            payload = b"contract-battery attachment \xf0\x9f\x8d\xb0 bytes"
            url = await pmo.upload_attachment(aid, "contract.md", payload)
            got = await pmo.download_asset(url)
            ok13 = got == payload
            note13 = "" if ok13 else f"{len(got)} bytes back, {len(payload)} sent"
        except Exception as e:
            ok13, note13 = False, str(e)[:160]
        finally:
            await cleanup_issue(pmo, aid)
        check("13", "attachment upload/download round-trip", ok13, note13)

    # ── 14 relations — always attempt; SKIP if the adapter cannot write
    blocker = await make_temp_issue(
        pmo, team, "[CONTRACT] blocker", {"DEVCAKE"})
    blocked = await make_temp_issue(
        pmo, team, "[CONTRACT] blocked", {"DEVCAKE"})
    ok14, note14, skip14 = True, "", False
    try:
        await pmo.create_relation(blocker, blocked)
        await pmo.create_relation(blocker, blocked)  # duplicate-tolerant
        if not pmo.capabilities().relations_supported:
            skip14, note14 = True, "adapter cannot write blocking links"
        else:
            m = await pmo.get(MissionRef(blocked, "issue"))
            ok14 = blocker in m.blocked_by
            note14 = f"blocked_by={m.blocked_by}"
    except Exception as e:
        ok14, note14 = False, str(e)[:160]
    finally:
        await cleanup_issue(pmo, blocked)
        await cleanup_issue(pmo, blocker)
    check("14", "create_relation + blocked_by (duplicate-tolerant)",
          ok14, note14, skipped=skip14)

    # ── 15 project full-mode activity mirror (project-fidelity fix) ──────
    # Gated on projects_supported AND a discoverable project (the port
    # cannot create projects — the seed fixture provides one on the Linear
    # sandbox lane; skip cleanly otherwise). Posts a sentinel-free update
    # via post_feed and asserts full mode mirrors it byte-for-byte.
    if not caps.projects_supported:
        check("15", "project full-mode mirrors the native feed",
              True, "projects_supported=False", skipped=True)
    else:
        proj15 = next((m for m in await pmo.list_all(team)
                       if m.pmo_kind == "project"), None)
        if proj15 is None:
            check("15", "project full-mode mirrors the native feed",
                  True, "no project in the team snapshot", skipped=True)
        else:
            ok15, note15 = True, ""
            try:
                ref15 = MissionRef(proj15.pmo_id, "project")
                marker = "contract battery update `devcake-contract:15`"
                await pmo.post_feed(ref15, marker)
                shallow = await pmo.get_activity(ref15)
                full15 = await pmo.get_activity(ref15, full=True)
                ok15 = (shallow.entries == []            # shallow stays cheap
                        and any(marker in e.body for e in full15.entries))
                note15 = (f"entries={len(full15.entries)} "
                          f"docs={len(full15.documents)}") if ok15 else \
                    "posted update did not round-trip into full entries"
            except Exception as e:
                ok15, note15 = False, str(e)[:160]
            check("15", "project full-mode mirrors the native feed",
                  ok15, note15)

    width = max(len(n) for _, n, _ in results)
    failures = skips = 0
    for num, name, res in results:
        print(f"  test {num:>2}  {name:<{width}}  {res}")
        failures += res.startswith(FAIL)
        skips += res.startswith(SKIP)
    passed = len(results) - failures - skips
    extra = f" ({skips} skipped)" if skips else ""
    print(f"\n{passed}/{len(results)} passed{extra}")
    return 1 if failures else 0


asyncio.run(main())
