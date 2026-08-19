"""MessagingPort — Redis Streams Dev↔app protocol surface (docs/09).

One adapter today: adapters.redis.Messaging. Slice scope uses create_run_user;
reply/teardown methods are part of the same seam for RunManager and later slices.

Adapters must never leak redis-py exception types upward — surface
`MessagingError` on Protocol methods (create_run_user, reply, …). The
long-running consumer loop handles reconnect internally and does not
raise MessagingError for transient disconnects.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

Handler = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class MessagingError(Exception):
    """Raised by MessagingPort adapters for wire-level failures (docs/09).

    Covers redis connection/timeout/response errors on request/reply and
    ACL lifecycle methods. Adapters must never leak redis.exceptions upward.
    """


class MessagingPort(Protocol):
    async def create_run_user(self, run_id: str) -> str: ...
    async def delete_run_user(self, run_id: str) -> None: ...
    # run_ids with entries still on the ingress stream — watchdog finalize-stall
    # backstop, boot reconcile (docs/04 §6 / CAKE-73), and kill-race promotion
    # distinguish resumable artifacts from wedged / truly-orphan runs
    async def unresolved_run_ids(self) -> set[str]: ...
    async def reply(self, run_id: str, kind: str, payload: dict[str, Any]) -> None: ...
    async def delete_runspec_result(self, run_id: str) -> None: ...
    async def delete_reply_stream(self, run_id: str) -> None: ...
    async def setup(self) -> None: ...
    async def reclaim_pending(
        self,
        handler: Handler,
        verify_auth: Callable[[str, str | None], bool],
    ) -> None: ...
    async def consume_forever(
        self,
        handler: Handler,
        verify_auth: Callable[[str, str | None], bool],
    ) -> None: ...
