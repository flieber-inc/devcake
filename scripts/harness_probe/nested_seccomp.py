"""Extract the dev-run DAG's inline nested-engine seccomp profile.

Stdlib-only — this runs on operator hosts (nested_probe.sh), which have no
PyYAML and no venv, so the profile is lifted by regex from the one-line
anchor in dev-run.yaml. The structural test asserts this extraction equals
the profile the DAG actually sends on both steps, so a YAML reshape breaks
CI instead of failing at probe time on some operator's laptop.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DAG = Path(__file__).resolve().parents[2] / "dagu" / "dags" / "dev-run.yaml"


def extract(path: Path = DAG) -> str:
    """The seccomp JSON blob exactly as the DAG ships it (no seccomp= prefix)."""
    m = re.search(r"'seccomp=(\{.*\})'", path.read_text())
    if not m:
        raise SystemExit(f"nested_seccomp: no inline seccomp profile in {path}")
    blob = m.group(1)
    json.loads(blob)  # refuse to emit something a container runtime would reject
    return blob


if __name__ == "__main__":
    print(extract(Path(sys.argv[1]) if len(sys.argv) > 1 else DAG))
