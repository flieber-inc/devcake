"""Structure ratchet (ADR-0015): the orchestrator god module must not return.

C1 rule: ``domain/orchestrator/manager.py`` contains the class and nothing
executable after it — no module-level ``MissionManager.<attr> = ...`` bindings
(the old façade mechanism) and no module-level ``def`` below the class. The
API route-body rule (every ``@app.<verb>`` body ≤ 4 statements, allowlist
shrinking to ``{"dispatch_hello"}``) arrives with the C6 close-out.
"""

import ast
from pathlib import Path

MANAGER = (Path(__file__).parents[1] / "devcake" / "domain" / "orchestrator"
           / "manager.py")


def test_manager_has_no_post_class_bindings():
    tree = ast.parse(MANAGER.read_text())
    class_idx = next(i for i, n in enumerate(tree.body)
                     if isinstance(n, ast.ClassDef) and n.name == "MissionManager")
    offenders = []
    for node in tree.body[class_idx + 1:]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            offenders.append(f"module-level def {node.name} after the class")
        for tgt in getattr(node, "targets", []):
            if (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == "MissionManager"):
                offenders.append(f"binding MissionManager.{tgt.attr}")
    assert not offenders, (
        "manager.py grew post-class bindings — ADR-0015 forbids resurrecting "
        "the façade: " + "; ".join(offenders))
