"""``.env`` upsert, permission floor, and bootstrap auto-init (ADR-0038 Decision 1)."""

from __future__ import annotations

import os
import re
import secrets
import string
from pathlib import Path

# Keys app boot refuses when empty/weak (api/main._refuse_insecure_passwords)
# plus OO_INGEST_EMAIL (blank email → silent 401s).
REQUIRED_BOOTSTRAP_KEYS: tuple[str, ...] = (
    "ADMIN_PASSWORD",
    "REDIS_PASSWORD",
    "DAGU_PASSWORD",
    "OO_ROOT_PASSWORD",
    "OO_INGEST_PASSWORD",
    "GITEA_ADMIN_PASSWORD",
    "OO_INGEST_EMAIL",
)

_WEAK = frozenset(
    {"", "change-me", "change-me-too", "change-me-as-well", "password", "admin"}
)

_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; comments and blanks ignored. Last wins."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _KEY_LINE.match(raw)  # keep raw (no strip) so values preserve spaces
        if not m:
            # try stripped form for indented oddities
            m = _KEY_LINE.match(line)
        if not m:
            continue
        out[m.group(1)] = m.group(2)
    return out


def read_env_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)


def upsert_env_var(key: str, value: str, path: Path) -> None:
    """Atomic same-filesystem upsert (mirrors up.sh OPS-L6 sibling-temp rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = read_env_lines(path) if path.is_file() else []
    replaced = False
    new_lines: list[str] = []
    for line in lines:
        raw = line.rstrip("\n\r")
        if raw.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            replaced = True
        else:
            if line.endswith("\n") or line.endswith("\r\n"):
                new_lines.append(line if line.endswith("\n") else line + "\n")
            else:
                new_lines.append(line + "\n")
    if not replaced:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
        if new_lines and new_lines[-1].strip():
            new_lines.append("\n")
        new_lines.append(f"{key}={value}\n")

    tmp = path.with_name(f".env.tmp.{secrets.token_hex(4)}")
    tmp.write_text("".join(new_lines), encoding="utf-8")
    if path.is_file():
        try:
            mode = path.stat().st_mode & 0o777
            tmp.chmod(mode)
        except OSError:
            tmp.chmod(0o600)
    else:
        tmp.chmod(0o600)
    tmp.replace(path)


def ensure_permission_floor(path: Path, mode: int = 0o600) -> None:
    """Force mode every run (up.sh permission floor)."""
    if path.is_file():
        path.chmod(mode)


def generate_strong_password(*, length: int = 24) -> str:
    """Password meeting OpenObserve v0.91.5 classes + ADMIN 12-char floor."""
    if length < 12:
        length = 12
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c not in string.ascii_letters + string.digits for c in pw)
            and pw.strip() not in _WEAK
        ):
            return pw


def generate_ingest_email() -> str:
    return f"ingest-{secrets.token_hex(4)}@localhost.local"


def needs_generation(key: str, value: str) -> bool:
    """True when auto-init should fill this required bootstrap key."""
    stripped = (value or "").strip()
    if key == "OO_INGEST_EMAIL":
        return not stripped
    if key == "ADMIN_PASSWORD":
        return stripped in _WEAK or len(stripped) < 12
    return stripped in _WEAK


def oo_password_ok(password: str) -> bool:
    """Mirror scripts/lib/oo_password.sh composition rule (non-empty assumed)."""
    if not password:
        return True  # empty deferred to boot / auto-init
    if not (8 <= len(password) <= 128):
        return False
    if not re.search(r"[a-z]", password):
        return False
    if not re.search(r"[A-Z]", password):
        return False
    if not re.search(r"[0-9]", password):
        return False
    if not re.search(r"[^a-zA-Z0-9]", password):
        return False
    return True


def seed_env_from_example(env_path: Path, example_path: Path) -> bool:
    """Copy .env.example → .env when missing. Returns True if created."""
    if env_path.is_file():
        return False
    if not example_path.is_file():
        raise FileNotFoundError(
            "no .env and no .env.example — create .env with bootstrap passwords first"
        )
    env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
    env_path.chmod(0o600)
    return True


def auto_init_bootstrap(env_path: Path) -> list[str]:
    """Fill missing/empty required bootstrap keys. Returns generated key names.

    Never echoes values. Existing non-weak values are preserved.
    Process env does NOT silently override file contents here — callers that
    want process-env precedence should upsert before calling this.
    """
    generated: list[str] = []
    current = parse_env_file(env_path)
    for key in REQUIRED_BOOTSTRAP_KEYS:
        # Process env wins when explicitly set (ADR precedence rule 1).
        proc_val = os.environ.get(key)
        if proc_val is not None and not needs_generation(key, proc_val):
            if current.get(key) != proc_val:
                upsert_env_var(key, proc_val, env_path)
                current[key] = proc_val
            continue
        existing = current.get(key, "")
        if not needs_generation(key, existing):
            continue
        if key == "OO_INGEST_EMAIL":
            value = generate_ingest_email()
        else:
            value = generate_strong_password()
        upsert_env_var(key, value, env_path)
        current[key] = value
        generated.append(key)
    ensure_permission_floor(env_path)
    return generated


def validate_oo_passwords(env_path: Path) -> None:
    """Raise ValueError when non-empty OO passwords violate composition."""
    data = parse_env_file(env_path)
    for key in ("OO_ROOT_PASSWORD", "OO_INGEST_PASSWORD"):
        val = data.get(key, "")
        if val and not oo_password_ok(val):
            raise ValueError(
                f"{key} does not meet OpenObserve v0.91.5 password policy: "
                f"must be 8-128 characters and contain at least one lowercase "
                f"letter, one uppercase letter, one digit, and one special character"
            )
