"""OpenObserve password-policy gate used by devcake up (CAKE-131).

Public seam: scripts/lib/oo_password.sh — `require_oo_password VAR value`
mirrors OpenObserve v0.91.5 src/config/src/utils/password.rs.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _lib() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "scripts" / "lib" / "oo_password.sh",
        Path("/srv/repo-scripts/lib/oo_password.sh"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None, (
        "oo_password.sh missing — bind scripts → /srv/repo-scripts"
    )
    return path


def _require(var: str, value: str) -> subprocess.CompletedProcess[str]:
    # Invoke bash directly: source the lib, call the public seam.
    script = (
        f'source "{_lib()}" && '
        f'require_oo_password {var} {value!r}; echo rc=$?'
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )


_RULE_SNIPPET = (
    "must be 8-128 characters and contain at least one lowercase letter, "
    "one uppercase letter, one digit, and one special character"
)


def test_require_oo_password_rejects_all_hex():
    """Long hex looks strong but fails OO's class requirements."""
    got = _require("OO_ROOT_PASSWORD", "deadbeefcafebabe0123456789abcdef")
    # Function returns 1; bash -c continues to echo rc= unless set -e.
    assert "rc=1" in got.stdout
    err = got.stderr
    assert "OO_ROOT_PASSWORD" in err
    assert "OpenObserve v0.91.5" in err
    assert _RULE_SNIPPET in err


def test_require_oo_password_rejects_too_short():
    """All classes present but under MIN_PASSWORD_LEN=8."""
    got = _require("OO_INGEST_PASSWORD", "Ab1!")
    assert "rc=1" in got.stdout
    assert "OO_INGEST_PASSWORD" in got.stderr
    assert _RULE_SNIPPET in got.stderr


def test_require_oo_password_accepts_minimum_compliant():
    got = _require("OO_ROOT_PASSWORD", "Abcdef1!")
    assert "rc=0" in got.stdout
    assert got.stderr == ""


def test_require_oo_password_accepts_ci_shaped():
    got = _require("OO_INGEST_PASSWORD", "Ci-oo-root-Pass1!")
    assert "rc=0" in got.stdout
    assert got.stderr == ""


def test_require_oo_password_skips_empty():
    """Empty stays with app-boot _refuse_insecure_passwords — not this gate."""
    got = _require("OO_ROOT_PASSWORD", "")
    assert "rc=0" in got.stdout
    assert got.stderr == ""
