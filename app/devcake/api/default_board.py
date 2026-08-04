"""ADR-0030: the auto-provisioned default PMO board.

Provisions the board on the bundled Gitea (org/repo/service user/PAT via the
admin-credentialed GiteaProvisioner) and registers/repairs the persisted
managed `board` PMOInstance. Idempotent and best-effort at every call site —
boot and config reloads never crash on a Gitea outage; the board self-heals
at the next call once Gitea is reachable.
"""

from __future__ import annotations

import asyncio
import logging

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
        info = await s.internal_forge.ensure_pmo_board()
        # adopt-don't-crash: an operator who manually configured this exact
        # board pre-feature would collide with the duplicate-target validator
        # — their instance IS the board; skip injection and say so
        for inst in s.config.pmos:
            if (not inst.managed and inst.system == "gitea_issues"
                    and inst.team_key == info["team_key"]
                    and (inst.api_base or "").rstrip("/") == info["api_base"]):
                log.info("default board: operator instance %r already targets "
                         "%s — adopting it, no managed row injected",
                         inst.name, info["team_key"])
                return
        desired = {"name": MANAGED_BOARD_NAME, "system": "gitea_issues",
                   "team_key": info["team_key"], "api_base": info["api_base"],
                   "managed": True}
        row = next((p for p in s.config.pmos
                    if p.managed and p.name == MANAGED_BOARD_NAME), None)
        if row is not None and all(getattr(row, k) == v
                                   for k, v in desired.items()):
            if info["minted"]:
                # the running adapter cached the dead PAT at construction —
                # rebuild so the fresh one takes (managers reconciled in place)
                s.reload_connections()
            return
        if row is None:
            s.config.pmos = [*s.config.pmos, PMOInstance(**desired)]
            log.info("default board: managed instance %r registered (%s)",
                     MANAGED_BOARD_NAME, info["team_key"])
        else:
            for k, v in desired.items():
                setattr(row, k, v)
            log.info("default board: managed instance %r repaired (%s)",
                     MANAGED_BOARD_NAME, info["team_key"])
        save_config(s.config)
        s.reload_connections()
