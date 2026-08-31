"""Shared apply_default_branch_protection orchestration (CAKE-181).

Adapters supply vendor-local discover / read / write callables; shape
derivation and the no-weaken rule live in ports/forge.py so all three
forges share one comparison model.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from ..ports.forge import (
    ApplyProtectionResult,
    ForgeCapabilities,
    ForgeError,
    ProtectionShape,
    derive_protection_shape,
    distinct_reviewer_configured,
    is_as_strict_as,
    merge_strictest,
)

DiscoverFn = Callable[[str], Awaitable[list[str]]]
ReadFn = Callable[[str], Awaitable[ProtectionShape | None]]
WriteFn = Callable[[str, ProtectionShape], Awaitable[None]]

T = TypeVar("T")


def protection_write_forbidden(
        *, forge: str, permission: str, detail: str = "") -> ForgeError:
    """Actionable 403 for protection writes — names the write token and the
    permission/scope the forge requires."""
    base = (
        f"{forge} write token lacks permission to set branch protection "
        f"(needs {permission})"
    )
    if detail:
        base = f"{base}: {detail}"
    return ForgeError(base, status=403)


async def run_apply_default_branch_protection(
        *,
        capabilities: ForgeCapabilities,
        write_token: str,
        reviewer_token: str | None,
        branch: str,
        discover_status_checks: DiscoverFn,
        read_protection_shape: ReadFn,
        write_protection_shape: WriteFn,
        forge_label: str,
        write_permission: str,
) -> ApplyProtectionResult:
    if not capabilities.branch_protection_write:
        raise ForgeError(
            f"{forge_label} does not support writing branch protection "
            f"(capabilities.branch_protection_write=False)",
            status=None,
        )
    checks = await discover_status_checks(branch)
    desired = derive_protection_shape(
        discovered_status_checks=checks,
        has_distinct_reviewer=distinct_reviewer_configured(
            write_token, reviewer_token),
    )
    current = await read_protection_shape(branch)
    if is_as_strict_as(current, desired):
        assert current is not None
        return ApplyProtectionResult(
            outcome="already_as_strict", shape=current)
    to_write = merge_strictest(current, desired)
    try:
        await write_protection_shape(branch, to_write)
    except ForgeError as e:
        if e.status == 403:
            raise protection_write_forbidden(
                forge=forge_label,
                permission=write_permission,
                detail=str(e),
            ) from e
        raise
    return ApplyProtectionResult(outcome="applied", shape=to_write)
