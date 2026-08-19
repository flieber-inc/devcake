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


# ADR-0029 closed TokenReport v1 key set — mirrored as a literal because
# hello is a standalone one-file image (no runtime dep on tokens.py).
_TOKEN_REPORT_V1_KEYS = (
    "schema", "model", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "total_tokens", "reasoning_tokens", "num_turns",
    "duration_ms", "cost_usd_native", "cost_usd_estimated", "source", "raw")


def test_hello_token_report_is_token_report_v1():
    """hello_dev.py posts TokenReport v1 (`source`), not pre-ADR-0029
    `extraction_method`."""
    tree = ast.parse(HELLO.read_text())
    reports = []

    class _Find(ast.NodeVisitor):
        def visit_Dict(self, node):
            keys = []
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.append(k.value)
            if "token_report" in keys:
                for k, v in zip(node.keys, node.values):
                    if (isinstance(k, ast.Constant) and k.value == "token_report"
                            and isinstance(v, ast.Dict)):
                        reports.append(v)
            self.generic_visit(node)

    _Find().visit(tree)
    assert reports, "hello_dev.py send_artifacts lost token_report"
    report = reports[0]
    got = {}
    for k, v in zip(report.keys, report.values):
        assert isinstance(k, ast.Constant) and isinstance(k.value, str)
        if isinstance(v, ast.Constant):
            got[k.value] = v.value
        elif isinstance(v, ast.Dict) and not v.keys:
            got[k.value] = {}
        else:
            got[k.value] = v
    assert set(got) == set(_TOKEN_REPORT_V1_KEYS), (
        f"hello token_report keys {sorted(set(got) ^ set(_TOKEN_REPORT_V1_KEYS))} "
        "out of TokenReport v1 shape")
    assert "extraction_method" not in got
    assert got.get("source") == "unavailable"
    assert got.get("schema") == 1
    assert got.get("model") == "stub"
    assert got.get("raw") == {}
