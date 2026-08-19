"""hello_dev.py ↔ devcake_dev/adapters/bus.py pinned-mirror contract
(ADR-0034). The hello image is deliberately a standalone one-file CI fixture
(no devcake_dev package), so its chunking/shrinking logic is a COPY of the
bus's — and it had already drifted (SHRINKABLE_FIELDS lost last_message_md;
2026-08-12 audit OPS-L3). Both files have import-time env reads and Redis
connections, so the shared pieces are AST-extracted and exec'd instead of
imported."""

import ast
import json
from pathlib import Path

_HERE = Path(__file__).resolve()
HELLO = Path("/srv/images/hello/hello_dev.py")
if not HELLO.exists():
    HELLO = _HERE.parents[2] / "images" / "hello" / "hello_dev.py"
BUS = Path("/srv/images/common/devcake_dev/adapters/bus.py")
if not BUS.exists():
    BUS = _HERE.parents[2] / "images" / "common" / "devcake_dev" / "adapters" / "bus.py"

_CONSTS = ("MAX_ARTIFACT_BYTES", "SHRINKABLE_FIELDS", "TRUNCATE_FLOOR",
           "CHUNK_LIMIT", "CHUNK_SIZE", "SEND_ATTEMPTS_RESILIENT")


def _extract(path: Path) -> dict:
    assert path.exists(), (
        f"mount missing — the pytest runner must bind {path.parent} "
        f"(images/hello + images/common → /srv/images/…)"
    )
    tree = ast.parse(path.read_text())
    keep = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and all(
                isinstance(t, ast.Name) and t.id in _CONSTS
                for t in node.targets):
            keep.append(node)
        elif (isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Tuple)
                and all(isinstance(e, ast.Name) and e.id in _CONSTS
                        for e in node.targets[0].elts)):
            keep.append(node)                # CHUNK_LIMIT, CHUNK_SIZE = …, …
        elif isinstance(node, ast.FunctionDef) and node.name == "_fit_payload":
            keep.append(node)
    ns: dict = {"json": json}
    exec(compile(ast.Module(body=keep, type_ignores=[]),  # noqa: S102 — repo-controlled source, constants + one pure function
                 str(path), "exec"), ns)
    return ns


def test_constants_match_field_by_field():
    hello, bus = _extract(HELLO), _extract(BUS)
    for name in _CONSTS:
        assert name in hello, f"hello_dev.py lost {name}"
        assert name in bus, f"bus.py lost {name}"
        assert hello[name] == bus[name], (
            f"{name} drifted: hello_dev.py={hello[name]!r} "
            f"bus.py={bus[name]!r} — update the pinned mirror"
        )


def test_fit_payload_behavior_parity():
    """Same oversized payload → byte-identical shrink on both sides (the
    constants could match while the algorithm drifted)."""
    hello, bus = _extract(HELLO), _extract(BUS)
    big = {
        "result": {"outcome": "executed"},
        "exit_code": 0,
        "token_report": {"total": 1},
        "transcript_md": "t" * (60 * 1024 * 1024),
        "plan_md": "p" * 20_000,
        "last_message_md": "m" * 20_000,
    }
    out_h = hello["_fit_payload"](dict(big))
    out_b = bus["_fit_payload"](dict(big))
    assert out_h == out_b
    # the invariants both sides promise: never shrink the verdict fields
    for f in ("result", "exit_code", "token_report"):
        assert out_h[f] == big[f]
    assert len(json.dumps(out_h).encode()) <= hello["MAX_ARTIFACT_BYTES"]
