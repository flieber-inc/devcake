"""Forge adapter contract battery — the ForgePort ACCEPTANCE GATE (docs/06 §5,
docs/16 M11). Exit code is the verdict.

Runs INSIDE the app container (imports devcake.*):
    docker compose exec -T app python - < scripts/contract_tests_forge.py

The **only wired live lane** is bundled Gitea (``DEVCAKE_CONTRACT_FORGE=gitea``,
the default). Zero external tokens; wired into ci_suite.sh / GHA. Creates a
scratch repo (+ scratch reviewer user) and deletes both.

Non-gitea ``DEVCAKE_CONTRACT_FORGE`` values hard-exit by design. GitHub/GitLab
live forge proof is the M12 acceptance ritual / ``scripts/acceptance.py``
(tester-side tokens), not this script. Hermetic GitHub/GitLab coverage lives
in ``app/tests/test_forge.py`` / ``test_forge_http.py``.
"""

import asyncio
import base64
import os
import secrets
import sys

import httpx

from devcake import security
from devcake.adapters.registry import make_forge
from devcake.config import RepoInstance

PASS, FAIL = "PASS", "FAIL"
# Pinned check count (ids 1–13). A vanished check must fail the battery —
# never self-grade N/N from len(results) alone (CAKE-83).
EXPECTED_ROWS = 13
results: list[tuple[str, str, str]] = []


def grade_contract_battery(results, expected_rows):
    """Return process exit code for a live contract battery.

    Fails when ``len(results) != expected_rows`` (even if every present row is
    PASS) or when any row is FAIL. SKIP rows (PMO) count toward expected_rows
    and do not fail the battery.
    """
    if len(results) != expected_rows:
        return 1
    for _num, _name, res in results:
        if res == "FAIL" or res.startswith("FAIL"):
            return 1
    return 0


def check(num: str, name: str, ok: bool, note: str = "") -> None:
    results.append((num, name,
                    PASS if ok else FAIL + (" — " + note if note else "")))


# ── gitea fixture provisioning (admin API; bundled instance) ────────────────

GITEA_URL = os.environ.get("DEVCAKE_CONTRACT_GITEA_URL", "http://gitea:3000")


class GiteaFixture:
    def __init__(self):
        self.admin = (os.environ["GITEA_ADMIN_USER"],
                      os.environ["GITEA_ADMIN_PASSWORD"])
        self.suffix = secrets.token_hex(3)
        self.repo = f"contract-{self.suffix}"
        self.reviewer = f"contractrev{self.suffix}"
        self.reviewer_pw = f"Rev-{secrets.token_hex(8)}!x"

    def _req(self, method, path, auth=None, **kw):
        r = httpx.request(method, f"{GITEA_URL}/api/v1{path}",
                          auth=auth or self.admin, timeout=20, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:200]}")
        return r.json() if r.text else None

    def up(self) -> RepoInstance:
        admin_user = self.admin[0]
        self._req("POST", f"/user/repos",
                  json={"name": self.repo, "private": True, "auto_init": True,
                        "default_branch": "main"})
        self._req("POST", "/admin/users",
                  json={"username": self.reviewer,
                        "email": f"{self.reviewer}@contract.example",
                        "password": self.reviewer_pw,
                        "must_change_password": False})
        self._req("PUT", f"/repos/{admin_user}/{self.repo}/collaborators/"
                         f"{self.reviewer}", json={"permission": "read"})
        self.admin_token = self._req(
            "POST", f"/users/{admin_user}/tokens",
            json={"name": f"contract-{self.suffix}",
                  "scopes": ["write:repository", "write:issue"]})["sha1"]
        self.reviewer_token = self._req(
            "POST", f"/users/{self.reviewer}/tokens",
            json={"name": f"contract-{self.suffix}",
                  "scopes": ["write:repository", "write:issue"]})["sha1"]
        # schema v4: token VALUES are stored, not env-referenced
        from devcake import secrets as _s
        _s.write_connection_secret("repo", "contract", "token", self.admin_token)
        _s.write_connection_secret("repo", "contract", "reviewer_token",
                                   self.reviewer_token)
        return RepoInstance(
            name="contract", forge="gitea",
            url=f"{GITEA_URL}/{admin_user}/{self.repo}.git")

    def make_branch_and_pr(self, branch: str, title: str) -> int:
        admin_user = self.admin[0]
        content = base64.b64encode(b"contract battery \xf0\x9f\x8d\xb0").decode()
        self._req("POST", f"/repos/{admin_user}/{self.repo}/contents/out.bin",
                  json={"content": content, "message": "contract",
                        "branch": "main", "new_branch": branch})
        pr = self._req("POST", f"/repos/{admin_user}/{self.repo}/pulls",
                       json={"head": branch, "base": "main", "title": title})
        return pr["number"]

    def protect_main(self):
        admin_user = self.admin[0]
        self._req("POST", f"/repos/{admin_user}/{self.repo}/branch_protections",
                  json={"branch_name": "main", "required_approvals": 1,
                        "enable_approvals_whitelist": True,
                        "approvals_whitelist_username": [self.reviewer]})

    def get_comments(self, pr_number: int) -> list[str]:
        admin_user = self.admin[0]
        cs = self._req("GET", f"/repos/{admin_user}/{self.repo}/issues/"
                              f"{pr_number}/comments")
        return [c["body"] for c in (cs or [])]

    def down(self):
        admin_user = self.admin[0]
        for method, path in (
                ("DELETE", f"/repos/{admin_user}/{self.repo}"),
                # revoke the admin token minted in up(): one live write-
                # capable admin token accumulated per ci_suite run (audit A11)
                ("DELETE", f"/users/{admin_user}/tokens/contract-{self.suffix}"),
                ("DELETE", f"/admin/users/{self.reviewer}?purge=true")):
            try:
                self._req(method, path)
            except Exception as e:
                print(f"cleanup {path}: {e}")
        try:
            # drop the battery's secret-store entries (repo/contract/* and
            # repo/bare/* written by check 6's no-reviewer approve probe)
            from devcake import secrets as _s
            _s.delete_connection_instance("repo", "contract")
            _s.delete_connection_instance("repo", "bare")
        except Exception as e:
            print(f"cleanup secret store: {e}")


async def run_battery(inst: RepoInstance, fixture) -> None:
    forge = make_forge(inst)
    branch = "devcake/CONTRACT-1"

    # 1 — health probe: ok + can_push with a write token
    h = await forge.health_probe()
    check("1", "health_probe ok + can_push", h.ok and h.can_push, h.detail)

    # 2 — get_pr_by_branch: None before any PR exists
    check("2", "get_pr_by_branch None pre-PR",
          await forge.get_pr_by_branch(branch) is None)

    n = fixture.make_branch_and_pr(branch, "[CONTRACT-1] battery")

    # 3 — newest-PR-by-branch found (client-side filter on gitea)
    pr = await forge.get_pr_by_branch(branch)
    check("3", "get_pr_by_branch finds the PR",
          pr is not None and pr.number == n and pr.state == "open",
          str(pr))

    # 4 — comment posted AND redacted (a planted runtime secret must mask)
    secret_value = f"contract-secret-{secrets.token_hex(12)}"
    security.register_runtime_secret("contract-test", secret_value)
    try:
        await forge.post_pr_comment(n, f"contract comment; leak: {secret_value}")
        bodies = fixture.get_comments(n)
        check("4", "post_pr_comment lands + redacts",
              any("contract comment" in b for b in bodies)
              and not any(secret_value in b for b in bodies),
              f"{len(bodies)} comments")
    finally:
        security.unregister_runtime_secret("contract-test")

    # 5 — protection FIRST (officialness is computed when the review is
    # created — live-verified: a pre-whitelist approval stays unofficial and
    # never satisfies required_approvals), then read it back
    fixture.protect_main()
    prot = await forge.default_branch_protection("main")
    check("5", "branch protection read (requires_reviews)",
          prot is not None and prot.protected and prot.requires_reviews,
          str(prot))

    # 6 — approve: False without a reviewer token, True with one (official —
    # the fixture whitelists the reviewer)
    from devcake import secrets as _s
    _s.write_connection_secret("repo", "bare", "token",
                               _s.read_connection_secret("repo", "contract", "token"))
    bare = make_forge(RepoInstance(name="bare", forge=inst.forge, url=inst.url))
    check("6", "approve False without reviewer / True with",
          (await bare.approve(n)) is False and (await forge.approve(n)) is True)

    # 7 — mergeable tri-state returns a value without raising
    verdict = await forge.mergeable(n)
    check("7", "mergeable returns tri-state", verdict in (True, False, None),
          str(verdict))

    # 8 — squash merge (handles gitea's overloaded 405 retry internally)
    ok8, note8 = True, ""
    try:
        await forge.merge(n)
        state = await forge.pr_state(n)
        ok8, note8 = state.merged, f"merged={state.merged}"
    except Exception as e:
        ok8, note8 = False, str(e)[:150]
    check("8", "squash merge lands", ok8, note8)

    # 9 — already-merged merge() is SUCCESS (ISSUES #6 redelivery honesty)
    ok9, note9 = True, ""
    try:
        await forge.merge(n)
    except Exception as e:
        ok9, note9 = False, str(e)[:150]
    check("9", "already-merged merge() is success", ok9, note9)

    # 10 — approval_footer renders with the PR url
    footer = forge.approval_footer(pr.url)
    check("10", "approval_footer renders", pr.url in footer)

    # 11 — pr_files lists the merged change set (deliverable packaging)
    # merge SHA comes from ForgePort.pr_state (same path deliver uses);
    # a pr_state failure fails the battery loudly — no silent "main" default
    state_after = await forge.pr_state(n)
    merge_sha = state_after.merge_commit_sha or "main"
    files = await forge.pr_files(n)
    check("11", "pr_files lists changed files",
          any(f.path == "out.bin" for f in files), f"{[f.path for f in files]}")

    # 12 — file_content returns EXACT bytes (base64-safe binary)
    ok12, note12 = True, ""
    try:
        raw = await forge.file_content("out.bin", merge_sha)
        ok12 = raw == b"contract battery \xf0\x9f\x8d\xb0"
        note12 = "" if ok12 else f"{raw!r}"
    except Exception as e:
        ok12, note12 = False, str(e)[:150]
    check("12", "file_content exact bytes (binary-safe)", ok12, note12)

    # 13 — capabilities declared
    caps = forge.capabilities
    check("13", "capabilities declared",
          caps.branch_protection_read in ("writer", "maintainer", "admin"))


def main():
    lane = os.environ.get("DEVCAKE_CONTRACT_FORGE", "gitea")
    if lane != "gitea":
        sys.exit(f"lane {lane!r}: external-forge lanes run via the M12 "
                 "acceptance ritual (needs tester-side tokens + "
                 "DEVCAKE_CONTRACT_REPO_URL) — ISSUES #30")
    fixture = GiteaFixture()
    inst = fixture.up()
    print(f"lane: gitea ({inst.url})")
    try:
        asyncio.run(run_battery(inst, fixture))
    finally:
        fixture.down()
    width = max((len(nm) for _, nm, _ in results), default=0)
    failures = 0
    for num, nm, res in results:
        print(f"  test {num:>2}  {nm:<{width}}  {res}")
        failures += res != PASS
    code = grade_contract_battery(results, EXPECTED_ROWS)
    if len(results) != EXPECTED_ROWS:
        ids = [num for num, _, _ in results]
        print(f"\nexpected {EXPECTED_ROWS} checks, got {len(results)} "
              f"(ids={ids})")
    else:
        print(f"\n{len(results) - failures}/{EXPECTED_ROWS} passed")
    raise SystemExit(code)


main()
