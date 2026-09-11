"""One value from a dotenv-style file, read the way compose reads it.

Stdlib-only and importable by path: the host baker (scripts/dev_factory),
the nested probe (a bash script, via `python3 env_value.py FILE KEY`) and
any other host-side reader take the SAME value `devcake up` wrote and
compose handed to the dagu service — last assignment wins, `export ` and
surrounding quotes stripped, an unquoted trailing ` # comment` dropped,
CR and a BOM ignored. Never a second parser.
"""
from __future__ import annotations

import sys
from pathlib import Path


def env_file_value(path: Path | str, key: str) -> str | None:
    """None when the file is unreadable or the key is absent."""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    value: str | None = None
    for raw in lines:
        line = raw.strip().lstrip("\ufeff").rstrip("\r")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        else:
            v = v.split(" #", 1)[0].rstrip()
        value = v
    return value


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: env_value.py FILE KEY")
    sys.stdout.write(env_file_value(sys.argv[1], sys.argv[2]) or "")
