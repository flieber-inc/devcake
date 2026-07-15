"""Stored per-Mission-Type prompt templates (v0.1.1): the operator-editable
half of the playbook system.

Layout — /data/config/prompt_templates/{TYPE}/{name}.yaml, mirroring the
dev_types/ pattern (config.py): one YAML per template, `default.yaml` seeded
from the Python constants at boot. Unlike dev_types' top-up seeding, a
DRIFTED default.yaml is overwritten too — `default` is API-read-only, so the
overwrite can never destroy operator data, and it keeps the shipped default
canonical across app upgrades (the UI says: copy the default to customize).
User templates are never touched by seeding.

Templates contain no secrets — plain prose with {var} placeholders — so they
live under /data/config, not /data/secrets. Validation happens at save time
(unknown placeholders vs PLAYBOOK_VARS, size cap for the runspec channel);
resolution at dispatch falls back to the built-in default with a warning
that surfaces in /health (`prompt_template_warnings`).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from . import DEFAULT_PLAYBOOKS, PLAYBOOK_VARS, _VAR

log = logging.getLogger("devcake.prompts")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_TEMPLATE_BYTES = 64 * 1024   # the prompt rides a Redis runspec reply


def _dir(mission_type: str) -> Path:
    from ..config import CONFIG_PATH
    return CONFIG_PATH.parent / "prompt_templates" / mission_type


def _require_type(mission_type: str) -> None:
    if mission_type not in PLAYBOOK_VARS:
        raise ValueError(f"unknown mission type {mission_type!r} — "
                         f"templated types: {sorted(PLAYBOOK_VARS)}")


def validate_template(mission_type: str, text: str) -> None:
    _require_type(mission_type)
    if not text or not text.strip():
        raise ValueError("template is empty")
    if len(text.encode()) > _MAX_TEMPLATE_BYTES:
        raise ValueError(f"template exceeds {_MAX_TEMPLATE_BYTES // 1024} KB "
                         f"(the run-spec size budget)")
    allowed = set(PLAYBOOK_VARS[mission_type])
    unknown = sorted(set(_VAR.findall(text)) - allowed)
    if unknown:
        raise ValueError(
            f"unknown placeholder(s) {unknown} — valid variables for "
            f"{mission_type}: {sorted(allowed)} (all other braces are "
            f"treated literally)")


def seed_default_templates() -> None:
    """Write (or re-canonicalize) each type's default.yaml. Called at boot."""
    from ..config import _atomic_yaml
    for mt, text in DEFAULT_PLAYBOOKS.items():
        path = _dir(mt) / "default.yaml"
        current = _load(path)
        if current is None or current.get("template") != text:
            _atomic_yaml(path, {"schema_version": 1, "mission_type": mt,
                                "name": "default", "template": text})
            log.info("prompt templates: seeded %s/default", mt)


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        log.error("unreadable prompt template %s", path)
        return None


def list_templates() -> dict[str, list[dict]]:
    """{mission_type: [{name, template, builtin}]}, default first."""
    out: dict[str, list[dict]] = {}
    for mt in PLAYBOOK_VARS:
        entries = [{"name": "default", "template": DEFAULT_PLAYBOOKS[mt],
                    "builtin": True}]
        d = _dir(mt)
        if d.is_dir():
            for p in sorted(d.glob("*.yaml")):
                if p.stem == "default":
                    continue
                data = _load(p)
                if data and isinstance(data.get("template"), str):
                    entries.append({"name": p.stem,
                                    "template": data["template"],
                                    "builtin": False})
        out[mt] = entries
    return out


def save_template(mission_type: str, name: str, text: str) -> None:
    from ..config import _atomic_yaml
    _require_type(mission_type)
    if name == "default":
        raise ValueError("'default' is reserved (the built-in template is "
                         "read-only — create a copy under a new name)")
    if not _NAME_RE.fullmatch(name):
        raise ValueError("template name must match "
                         "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    validate_template(mission_type, text)
    _atomic_yaml(_dir(mission_type) / f"{name}.yaml",
                 {"schema_version": 1, "mission_type": mission_type,
                  "name": name, "template": text})


def delete_template(mission_type: str, name: str) -> None:
    _require_type(mission_type)
    if name == "default":
        raise ValueError("'default' is reserved and cannot be deleted")
    path = _dir(mission_type) / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no template {mission_type}/{name}")
    path.unlink()


def resolve_playbook(mission_type: str,
                     active: str | None) -> tuple[str, str | None]:
    """(playbook_text, warning|None) — the dispatch-time read. A missing or
    corrupt active template falls back to the built-in default so dispatch
    never fails on template trouble; the warning surfaces in /health."""
    _require_type(mission_type)
    if not active or active == "default":
        return DEFAULT_PLAYBOOKS[mission_type], None
    data = _load(_dir(mission_type) / f"{active}.yaml")
    text = (data or {}).get("template")
    if isinstance(text, str) and text.strip():
        return text, None
    return DEFAULT_PLAYBOOKS[mission_type], (
        f"{mission_type}: active prompt template '{active}' is missing or "
        f"corrupt — using the built-in default")


def template_warnings(config) -> list[str]:
    """Health surface: one warning per mission type whose active template
    doesn't resolve. Stateless, recomputed per call."""
    warns = []
    for mt in PLAYBOOK_VARS:
        active = (config.active_prompt_templates or {}).get(mt)
        if active and active != "default":
            _, warn = resolve_playbook(mt, active)
            if warn:
                warns.append(warn)
    return warns
