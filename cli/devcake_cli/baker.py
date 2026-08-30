"""``devcake baker run`` — thin wrapper over ``scripts/dev_factory.watch.main``.

Bake/receipt logic stays single-sited in ``dev_factory`` (ADR-0038 Decision 3 /
Honor Chokepoints). Supervisors keep ``PYTHONPATH=repo/scripts:repo/app`` so
``import dev_factory`` resolves the same way as ``python -m dev_factory``.
"""

from __future__ import annotations


def run() -> int:
    """Enter the host baker loop. Returns the baker's exit code."""
    from dev_factory.watch import main as watch_main

    return int(watch_main() or 0)
