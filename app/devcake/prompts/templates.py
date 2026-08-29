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

from ..pathsafety import confined
from . import DEFAULT_PLAYBOOKS, PLAYBOOK_VARS, _VAR

log = logging.getLogger("devcake.prompts")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")

# Built-in WORKFLOW presets (read-only, re-canonicalized at boot). The
# founder-named default is "Development" (the original playbooks); "default"
# is accepted everywhere as a legacy alias for it. "Customer Success" ships
# as a second preset (prompts/customer_success.py).
def _builtins() -> dict[str, dict[str, str]]:
    from .customer_success import CS_PLAYBOOKS
    return {"Development": DEFAULT_PLAYBOOKS, "Customer Success": CS_PLAYBOOKS}


def _canon(name: str | None) -> str:
    return "Development" if not name or name == "default" else name
_MAX_TEMPLATE_BYTES = 64 * 1024   # the prompt rides a Redis runspec reply


def _prompt_templates_root() -> Path:
    """Constant trusted root for mission-type prompt templates (CAKE-140).

    Tainted ``mission_type`` values must ride as ``confined`` parts under this
    root — never be joined into the base before confinement.
    """
    from ..config import CONFIG_PATH
    return CONFIG_PATH.parent / "prompt_templates"


def _dir(mission_type: str) -> Path:
    """Per-type subdirectory for read/list/seed (type from PLAYBOOK_VARS or disk)."""
    return _prompt_templates_root() / mission_type


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
    """Write (or re-canonicalize) each type's built-in workflow presets
    ("Development", "Customer Success"). Called at boot. The pre-rename
    default.yaml files are removed (superseded by Development.yaml)."""
    from ..config import _atomic_yaml
    for name, playbooks in _builtins().items():
        for mt, text in playbooks.items():
            path = _dir(mt) / f"{name}.yaml"
            current = _load(path)
            if current is None or current.get("template") != text:
                _atomic_yaml(path, {"schema_version": 1, "mission_type": mt,
                                    "name": name, "template": text})
                log.info("prompt templates: seeded %s/%s", mt, name)
    for mt in PLAYBOOK_VARS:
        (_dir(mt) / "default.yaml").unlink(missing_ok=True)


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — unreadable template logged and read as absent; seeded defaults still serve
        log.error("unreadable prompt template %s", path)
        return None


def list_templates() -> dict[str, list[dict]]:
    """{mission_type: [{name, template, builtin}]}, default first."""
    out: dict[str, list[dict]] = {}
    builtins = _builtins()
    for mt in PLAYBOOK_VARS:
        entries = [{"name": nm, "template": pb[mt], "builtin": True}
                   for nm, pb in builtins.items()]
        d = _dir(mt)
        if d.is_dir():
            for p in sorted(d.glob("*.yaml")):
                if p.stem in builtins or p.stem == "default":
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
    if name in _builtins() or name == "default":
        raise ValueError(f"{name!r} is a built-in preset (read-only — "
                         f"create a copy under a new name)")
    if not _NAME_RE.fullmatch(name):
        raise ValueError("template name must match "
                         "^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
    validate_template(mission_type, text)
    path = confined(_prompt_templates_root(), mission_type, f"{name}.yaml")
    _atomic_yaml(path,
                 {"schema_version": 1, "mission_type": mission_type,
                  "name": name, "template": text})


def delete_template(mission_type: str, name: str) -> None:
    _require_type(mission_type)
    if name in _builtins() or name == "default":
        raise ValueError(f"{name!r} is a built-in preset and cannot be deleted")
    if not _NAME_RE.fullmatch(name):
        raise ValueError("template name must match "
                         "^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
    try:
        path = confined(_prompt_templates_root(), mission_type, f"{name}.yaml")
    except ValueError as e:
        raise ValueError(
            f"template path escapes {mission_type!r} template directory"
        ) from e
    if not path.exists():
        raise FileNotFoundError(f"no template {mission_type}/{name}")
    path.unlink()


def resolve_playbook(mission_type: str,
                     active: str | None) -> tuple[str, str | None]:
    """(playbook_text, warning|None) — the dispatch-time read. A missing or
    corrupt active template falls back to the built-in default so dispatch
    never fails on template trouble; the warning surfaces in /health."""
    _require_type(mission_type)
    active = _canon(active)
    builtins = _builtins()
    if active in builtins:
        return builtins[active][mission_type], None
    data = _load(_dir(mission_type) / f"{active}.yaml")
    text = (data or {}).get("template")
    if isinstance(text, str) and text.strip():
        return text, None
    return DEFAULT_PLAYBOOKS[mission_type], (
        f"{mission_type}: active prompt template '{active}' is missing or "
        f"corrupt — using the built-in default")


def _effective_actives(config) -> dict[str, set[str]]:
    """Mission Type → set of canonically-named templates that are effective
    somewhere (global active ∪ each PMO override). Dedupes identical
    override==global selections so health does not spam."""
    from ..config import active_prompt_template_for

    out: dict[str, set[str]] = {mt: set() for mt in PLAYBOOK_VARS}
    for mt in PLAYBOOK_VARS:
        out[mt].add(_canon((config.active_prompt_templates or {}).get(mt)))
    for inst in getattr(config, "pmos", None) or []:
        for mt in PLAYBOOK_VARS:
            # go through the chokepoint so inherit vs override stay aligned
            # with dispatch (CAKE-150 / ADR-0037)
            out[mt].add(_canon(active_prompt_template_for(config, inst, mt)))
    return out


def template_warnings(config) -> list[str]:
    """Health surface: one warning per (mission type, effective template)
    whose active does not resolve, plus the ADR-0012 inertness check — an
    ONBOARD template without {decomposition_rule} keeps whatever static
    depth sentence it was saved with, so a configured limit above 1
    silently never reaches the Dev. Covers the global actives and every
    PMO override (CAKE-150). Stateless, recomputed per call."""
    warns = []
    seen_warn: set[str] = set()
    actives = _effective_actives(config)

    def _once(msg: str) -> None:
        if msg not in seen_warn:
            seen_warn.add(msg)
            warns.append(msg)

    for mt, names in actives.items():
        for active in sorted(names):
            if active not in _builtins():
                _, warn = resolve_playbook(mt, active)
                if warn:
                    _once(warn)
    # a custom ONBOARD template saved before 2026-07-18 may still instruct
    # the removed executed_trivially outcome — every such run parks as an
    # illegal outcome, so surface it instead of failing silently
    for active in sorted(actives["ONBOARD"]):
        text, _ = resolve_playbook("ONBOARD", active)
        if "executed_trivially" in text:
            _once(
                f"ONBOARD template '{active}' still instructs the removed "
                "executed_trivially outcome — its runs will park with "
                "DEVCAKE-SKIP; re-save it from the current default")
    # ADR-0018: an EXECUTE/PLAN-family template saved before 2026-07-24 still
    # carries the unqualified binding rule "Work ONLY inside /workspace/repo/…"
    # alongside the rule ordering a write to /workspace/out/result.json. Strong
    # models read past the contradiction; weaker ones resolve it by writing a
    # cwd-relative result.json inside the clone, which fails the run as
    # DEV_BAD_OUTPUT and can be swept into the PR by commit-at-end. Built-ins
    # are re-canonicalized at boot, so only operator copies can be stale.
    for mt, names in actives.items():
        for active in sorted(names):
            text, _ = resolve_playbook(mt, active)
            if "Work ONLY inside" in text:
                _once(
                    f"{mt} template '{active}' still carries the unqualified "
                    f"\"Work ONLY inside /workspace/repo/…\" rule, which "
                    f"contradicts the /workspace/out/result.json rule — Devs "
                    f"may write result.json into the repository and fail the "
                    f"run. Re-save it from the current default.")
    if getattr(config, "max_decomposition_depth", 1) != 1:
        for active in sorted(actives["ONBOARD"]):
            text, _ = resolve_playbook("ONBOARD", active)
            if "{decomposition_rule}" not in text:
                shown = config.max_decomposition_depth or "unlimited"
                _once(
                    f"ONBOARD: active prompt template '{active}' has no "
                    "{decomposition_rule} placeholder — the configured "
                    f"decomposition depth ({shown}) cannot reach the Dev "
                    "prompt; re-add the placeholder or switch to a built-in "
                    "template")
    return warns


# ── Dev-Type identifying-prompt templates (founder request 2026-07-15) ───────
# Same workflow-named template idea, applied to each Dev Type's identifying
# prompt. Layout: /data/config/devtype_prompt_templates/{dev_type}/{name}.yaml
# (own root — dev type names are operator-chosen and could collide with the
# mission-type dirs above). "Development" is seeded ONCE per dev type from
# its live identifying_prompt (user data — never re-canonicalized); the
# seeded trio also gets the "Customer Success" preset. Identifying prompts
# are plain prefix text: no {var} rendering, no placeholder validation.

def _devtype_prompt_templates_root() -> Path:
    """Constant trusted root for Dev-Type identifying-prompt templates (CAKE-140).

    Tainted ``dev_type`` values must ride as ``confined`` parts under this
    root — never be joined into the base before confinement.
    """
    from ..config import CONFIG_PATH
    return CONFIG_PATH.parent / "devtype_prompt_templates"


def _dev_dir(dev_type: str) -> Path:
    """Per-dev-type subdirectory for read/list/seed (type from dev_types keys or disk)."""
    return _devtype_prompt_templates_root() / dev_type


def known_devtype_dirs() -> list[Path]:
    """Every dev type that has a prompt-template dir on disk — the settings-
    bundle apply prunes dirs of dev types the bundle removed (ADR-0013)."""
    root = _devtype_prompt_templates_root()
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def seed_devtype_prompts(dev_types: dict) -> None:
    """Top-up seeding at boot and on dev-type create: Development ← the dev
    type's current identifying prompt (once); Customer Success ← the preset
    for the seeded trio (once)."""
    from ..config import _atomic_yaml
    from .customer_success import CS_DEV_PROMPTS
    for name, dt in dev_types.items():
        dev_path = _dev_dir(name) / "Development.yaml"
        if not dev_path.exists():
            _atomic_yaml(dev_path, {"schema_version": 1, "dev_type": name,
                                    "name": "Development",
                                    "template": dt.identifying_prompt or ""})
            log.info("devtype prompts: seeded %s/Development", name)
        if name in CS_DEV_PROMPTS:
            cs_path = _dev_dir(name) / "Customer Success.yaml"
            if not cs_path.exists():
                _atomic_yaml(cs_path, {"schema_version": 1, "dev_type": name,
                                       "name": "Customer Success",
                                       "template": CS_DEV_PROMPTS[name]})
                log.info("devtype prompts: seeded %s/Customer Success", name)


def list_devtype_prompts(dev_types: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for name in dev_types:
        entries = []
        d = _dev_dir(name)
        if d.is_dir():
            for p in sorted(d.glob("*.yaml")):
                data = _load(p)
                if data and isinstance(data.get("template"), str):
                    entries.append({"name": p.stem,
                                    "template": data["template"],
                                    "builtin": False})
        out[name] = entries
    return out


def save_devtype_prompt(dev_type: str, name: str, text: str) -> None:
    from ..config import _atomic_yaml
    if not _NAME_RE.fullmatch(name):
        raise ValueError("template name must match "
                         "^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
    if len(text.encode()) > _MAX_TEMPLATE_BYTES:
        raise ValueError(f"template exceeds {_MAX_TEMPLATE_BYTES // 1024} KB")
    path = confined(_devtype_prompt_templates_root(), dev_type, f"{name}.yaml")
    _atomic_yaml(path,
                 {"schema_version": 1, "dev_type": dev_type,
                  "name": name, "template": text})


def delete_devtype_prompt(dev_type: str, name: str) -> None:
    from ..config import DEV_TYPE_NAME_RE
    if not DEV_TYPE_NAME_RE.fullmatch(dev_type or ""):
        raise ValueError("dev_type must match ^[A-Za-z0-9][A-Za-z0-9_-]*$")
    if not _NAME_RE.fullmatch(name):
        raise ValueError("template name must match "
                         "^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
    try:
        path = confined(_devtype_prompt_templates_root(), dev_type, f"{name}.yaml")
    except ValueError as e:
        raise ValueError(
            f"template path escapes {dev_type!r} template directory"
        ) from e
    if not path.exists():
        raise FileNotFoundError(f"no template {dev_type}/{name}")
    path.unlink()


def resolve_devtype_prompt(dev_type: str, active: str | None,
                           fallback: str) -> tuple[str, str | None]:
    """(identifying_prompt_text, warning|None). Missing/corrupt actives fall
    back to the Development template, then to the DevType's stored
    identifying_prompt — dispatch never fails on template trouble."""
    active = _canon(active)
    data = _load(_dev_dir(dev_type) / f"{active}.yaml")
    text = (data or {}).get("template")
    if isinstance(text, str):
        return text, None
    dev = _load(_dev_dir(dev_type) / "Development.yaml")
    dev_text = (dev or {}).get("template")
    warn = (None if active == "Development" else
            f"{dev_type}: active identifying-prompt template '{active}' is "
            f"missing or corrupt — using Development")
    if isinstance(dev_text, str):
        return dev_text, warn
    return fallback, warn


def devtype_prompt_warnings(config, dev_types: dict) -> list[str]:
    warns = []
    for name, dt in dev_types.items():
        active = _canon((config.active_devtype_prompts or {}).get(name))
        if active != "Development":
            _, warn = resolve_devtype_prompt(name, active,
                                             dt.identifying_prompt)
            if warn:
                warns.append(warn)
    return warns
