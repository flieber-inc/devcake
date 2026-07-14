"""MessagingPort — Redis Streams Dev↔app protocol surface (docs/09).

One adapter today: adapters.redis.Messaging. Slice scope uses create_run_user;
reply/teardown methods are part of the same seam for RunManager and later slices.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

Handler = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class MessagingPort(Protocol):
    async def create_run_user(self, run_id: str) -> str: ...
    async def delete_run_user(self, run_id: str) -> None: ...
    # run_ids with entries still on the ingress stream — the watchdog's
    # finalize-stall backstop distinguishes resumable finalizing runs from
    # wedged ones with it (domain/watchdog.py)
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
