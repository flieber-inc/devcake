"""ADR-0014 D4: per-mission activity repos — the app-written, Dev-read-only
record of what each step's Dev actually received. Name rule, dispatch
pre-push hook (never gates), runspec carriage."""

import asyncio

from devcake.ports.internal_forge import (ACTIVITY_PREFIX, activity_repo_name,
                                          internal_repo_name)


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def test_activity_repo_name_prefix_and_cap():
    assert activity_repo_name("linear", "DEV-17") == "activity-linear-dev-17"
    # prefix applied AFTER the 60-char sanitize/cap: ≤69 total, and no
    # re-truncation (re-truncating could collide two long mission keys)
    long = activity_repo_name("linear", "X" * 200)
    assert long == ACTIVITY_PREFIX + internal_repo_name("linear", "X" * 200)
    assert len(long) <= 69
    # the sweeper discriminator: operator card names (^[a-z][a-z0-9]{0,11}$)
    # can never start with the hyphen-bearing prefix — even one literally
    # named "activity"
    assert not "activity".startswith(ACTIVITY_PREFIX)
    assert long.startswith(ACTIVITY_PREFIX)
