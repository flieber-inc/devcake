"""ADR-0030 / CAKE-157: the auto-provisioned default PMO board.

Provisions the board on the bundled Gitea (org/repo/service user/PAT via the
admin-credentialed GiteaProvisioner) and registers/repairs the persisted
managed PMOInstance. Durable identity is `managed=true`; the display name is
operator-owned (default insert name `board`). Idempotent and best-effort at
every call site — boot and config reloads never crash on a Gitea outage; the
board self-heals at the next call once Gitea is reachable.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext

from ..config import MANAGED_BOARD_NAME, PMOInstance, save_config

log = logging.getLogger("devcake")

# boot's lifespan call and reload_connections' opportunistic re-ensure can
# overlap — the PAT liveness check + delete-then-create mint must not race
# itself into storing a revoked token
_lock = asyncio.Lock()


async def ensure_default_board(s) -> None:
    """Provision (or repair) the board and its managed instance row."""
    if s.internal_forge is None:
        return
    async with _lock:
        # Secret store key follows the live managed instance name (CAKE-157);
        # default to MANAGED_BOARD_NAME only when no managed row exists yet.
        managed = next((p for p in s.config.pmos if p.managed), None)
        instance_name = managed.name if managed is not None else MANAGED_BOARD_NAME
        info = await s.internal_forge.ensure_pmo_board(
            instance_name=instance_name)
        # L-2 (2026-08): the config mutation + reload below must not race a
        # suspended poll cycle — same contract as config PUT and the secret
        # endpoints. Lock ordering is always _lock → poll_rt.lock and never
        # inverted: reload_connections is sync and only SPAWNS the re-ensure
        # (services.py create_task), so no cycle-lock holder ever awaits this
        # function inline. Fakes without poll_rt run lock-free, like a
        # missing cycle_lock elsewhere. Held across the network call above?
        # No — acquired after it, so a poll waits only on local work.
        poll_rt = getattr(s, "poll_rt", None)
        async with (poll_rt.lock if poll_rt is not None else nullcontext()):
            # adopt-don't-crash: an operator who manually configured this
            # exact board pre-feature would collide with the duplicate-target
            # validator — their instance IS the board; skip injection and say
            # so
            for inst in s.config.pmos:
                if (not inst.managed and inst.system == "gitea_issues"
                        and inst.team_key == info["team_key"]
                        and (inst.api_base or "").rstrip("/") == info["api_base"]):
                    log.info("default board: operator instance %r already "
                             "targets %s — adopting it, no managed row "
                             "injected", inst.name, info["team_key"])
                    return
            # Identity fields only — name is operator-owned after first insert
            identity = {"system": "gitea_issues",
                        "team_key": info["team_key"],
                        "api_base": info["api_base"], "managed": True}
            row = next((p for p in s.config.pmos if p.managed), None)
            if row is not None and all(getattr(row, k) == v
                                       for k, v in identity.items()):
                if info["minted"]:
                    # the running adapter cached the dead PAT at construction
                    # — rebuild so the fresh one takes (managers reconciled
                    # in place)
                    s.reload_connections()
                return
            if row is None:
                s.config.pmos = [
                    *s.config.pmos,
                    PMOInstance(name=MANAGED_BOARD_NAME, **identity),
                ]
                log.info("default board: managed instance %r registered (%s)",
                         MANAGED_BOARD_NAME, info["team_key"])
            else:
                for k, v in identity.items():
                    setattr(row, k, v)
                log.info("default board: managed instance %r repaired (%s)",
                         row.name, info["team_key"])
            save_config(s.config)
            s.reload_connections()
