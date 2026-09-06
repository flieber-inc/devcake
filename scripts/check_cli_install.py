#!/usr/bin/env python3
"""Exercise an installed CLI from outside its source checkout (no Docker writes)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path)
    args = parser.parse_args()
    python = args.python.absolute()
    console = python.parent / "devcake"
    root = Path(__file__).resolve().parents[1]
    expected = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    with tempfile.TemporaryDirectory(prefix="devcake-installed-") as scratch:
        def run(command: list[str], code: int = 0) -> subprocess.CompletedProcess:
            result = subprocess.run(command, cwd=scratch, env=env, text=True,
                                    capture_output=True, timeout=60)
            assert result.returncode == code, result.stdout + result.stderr
            return result

        # Import every shipped module and prove it came from the installation.
        result = run([str(python), "-I", "-c", '''
import importlib, importlib.metadata, json, pathlib, pkgutil
import devcake_cli
for module in pkgutil.iter_modules(devcake_cli.__path__):
    if module.name != "__main__":
        importlib.import_module("devcake_cli." + module.name)
print(json.dumps({"version": importlib.metadata.version("devcake-cli"),
                  "module_version": devcake_cli.__version__,
                  "path": str(pathlib.Path(devcake_cli.__file__).resolve())}))
'''])
        installed = json.loads(result.stdout)
        assert installed["version"] == installed["module_version"] == expected
        assert not Path(installed["path"]).is_relative_to(root)
        for command in ([], ["up"], ["down"], ["status"], ["doctor"], ["setup"], ["baker"]):
            output = run([str(console), *command, "--help"]).stdout
            assert "usage:" in output.lower(), output
        assert "usage:" in run([str(python), "-I", "-m", "devcake_cli", "--help"]).stdout
        assert "unknown verb" in run([str(console), "not-a-command"], 2).stderr
        assert "not a DevCake checkout" in run(
            [str(console), "up", "--dry-run"], 3).stderr
        doctor = json.loads(run([str(console), "doctor", "--json"], 3).stdout)
        assert doctor["schema_version"] == 1 and doctor["ok"] is False
        layout = next(c for c in doctor["checks"] if c["id"] == "checkout_layout")
        assert layout["ok"] is False
    print(f"installed CLI {expected}: imports, entry points, help, diagnostics and exit codes passed")


if __name__ == "__main__":
    main()
