"""The canonical settings-bundle representation (ADR-0013): ONE versioned
serialization of the deployment's settings, consumed by both the config
profiles store (profiles.py) and settings export/import.

Sections (founder taxonomy):
    config     (A) AppConfig + dev types + prompt templates + active pointers
    secrets    (B) connection/harness secret VALUES (+ optional credential files)
    setup_env  (C) .env bootstrap values, read from the app's own environment

A bundle is a plain dict (YAML on the wire, `yaml.safe_load` ONLY — bundles
are operator-uploaded input). Section A is always plaintext so a bundle stays
diffable (ADR-0002); B and C travel in one encrypted envelope by default
(settings_crypto, PR2). Applying a bundle is REPLACE-the-world for the
sections it contains, routed through the same choke points as the config PUT
(reject_stale_patch → model_validate → semantic checks → dry-run adapters).

There are no transactions (ADR-0002): apply orders writes so any torn state
is either pre-commit (old config.yaml authoritative, new files inert) or
post-commit (new world authoritative, stale extras pruned by a re-apply).
Rollback re-applies the pre-captured previous bundle through this same path.

Validation errors NEVER echo input values — a malformed secrets section must
not leak a value into a 422 detail or a log line (ADR-0013 hardening).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from opentelemetry import trace
from pydantic import ValidationError

from . import secrets as secrets_store
from .config import (AppConfig, DevType, _INSTANCE_NAME_RE, delete_dev_type,
                     reject_stale_patch, save_config, save_dev_type)
from .prompts import PLAYBOOK_VARS
from .prompts import templates as prompt_templates
from .security import redact

log = logging.getLogger("devcake.settings")
tracer = trace.get_tracer("devcake")

BUNDLE_KIND = "devcake-settings-bundle"
BUNDLE_SCHEMA_VERSION = 1

# Section C — the .env bootstrap set, mirroring .env.example (ONE place; a
# tripwire test diffs this list against the file). (name, host_specific):
# host-specific values ride the bundle but the generated .env flags them for
# verification on the target host. DEVCAKE_ALLOW_INSECURE is deliberately
# ABSENT: exporting an insecure-mode flag onto a production host would
# silently disable its password checks.
SETUP_ENV_VARS: list[tuple[str, bool]] = [
    ("OO_ROOT_EMAIL", False),
    ("OO_ROOT_PASSWORD", False),
    ("OO_INGEST_EMAIL", False),
    ("OO_INGEST_PASSWORD", False),
    ("OO_ALERT_WEBHOOK", False),
    ("OO_DAILY_COST_ALERT_USD", False),
    ("GITEA_ADMIN_USER", False),
    ("GITEA_ADMIN_PASSWORD", False),
    ("GITEA_UI_URL", False),
    ("DAGU_USER", False),
    ("DAGU_PASSWORD", False),
    ("REDIS_PASSWORD", False),
    ("ADMIN_USER", False),
    ("ADMIN_PASSWORD", False),
    ("DAGU_UI_URL", False),
    ("OO_UI_URL", False),
    ("DOCKER_GID", True),
    ("DEVCAKE_TAG", True),
]
_SETUP_ENV_NAMES = {name for name, _ in SETUP_ENV_VARS}

# ~20 MB decoded — bundles are settings, not data; anything bigger is a
# mistake or an attack, refused before parsing
MAX_BUNDLE_BYTES = 20 * 1024 * 1024


class BundleError(ValueError):
    """Carries the HTTP status the API layer should map to (SkillStoreError
    pattern). Messages must never contain secret material."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── audit trail ──────────────────────────────────────────────────────────────

def _audit_path() -> Path:
    return Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "state" / "events.jsonl"


def audit_event(action: str, detail: str = "") -> None:
    """Settings-change audit record on the existing events.jsonl stream —
    same shape as the mission audit (feed._audit) so readers need one parser.
    Detail carries names/counts/section lists ONLY, never a value; redact()
    is belt-and-braces, not the contract."""
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps({"ts": _utcnow(), "instance": "", "pmo_id": "",
                            "action": action, "detail": redact(detail)}) + "\n")
    with tracer.start_as_current_span("audit.event") as span:
        span.set_attribute("devcake.audit.action", action)
        span.set_attribute("devcake.audit.detail", redact(detail)[:500])


# ── serialize ────────────────────────────────────────────────────────────────

def serialize_current(config: AppConfig, dev_types: dict[str, DevType], *,
                      include_config: bool = True,
                      include_secrets: bool = True,
                      include_credential_files: bool = False,
                      include_setup_env: bool = False,
                      skill_payloads: list[dict] | None = None) -> dict:
    """Snapshot the live world into a bundle dict. dismissed_alerts is
    STRIPPED (importing someone else's dismissals could hide active
    advisories); credential files are export-only opt-in (host-migration
    material, never profile material)."""
    bundle: dict = {
        "kind": BUNDLE_KIND,
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "created_at": _utcnow(),
        "devcake_tag": os.environ.get("DEVCAKE_TAG", ""),
        "sections": [],
    }
    if include_config:
        app = config.model_dump()
        app.pop("dismissed_alerts", None)
        operator_templates: dict[str, dict[str, str]] = {}
        for mt, entries in prompt_templates.list_templates().items():
            operator_templates[mt] = {e["name"]: e["template"]
                                      for e in entries if not e["builtin"]}
        devtype_prompts = {
            dev: {e["name"]: e["template"] for e in entries}
            for dev, entries in
            prompt_templates.list_devtype_prompts(dev_types).items()}
        bundle["config"] = {
            "app": app,
            "dev_types": {n: dt.model_dump() for n, dt in dev_types.items()},
            "prompt_templates": operator_templates,
            "devtype_prompts": devtype_prompts,
        }
        bundle["sections"].append("config")
    if include_secrets:
        sec: dict = {
            "connections": secrets_store.list_connection_secrets(),
            "harness": secrets_store.list_harness_secrets(),
        }
        if include_credential_files:
            sec["credential_files"] = _read_credential_files(dev_types)
        bundle["secrets"] = sec
        bundle["sections"].append("secrets")
    if include_setup_env:
        values = {}
        for name, _host in SETUP_ENV_VARS:
            v = os.environ.get(name)
            if v is not None:
                values[name] = v
        bundle["setup_env"] = {
            "values": values,
            "host_specific": [n for n, host in SETUP_ENV_VARS if host],
        }
        bundle["sections"].append("setup_env")
    if skill_payloads:
        bundle["skills"] = {"embedded": skill_payloads}
    return bundle


def _read_credential_files(dev_types: dict) -> dict[str, list[dict]]:
    import base64
    root = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets"
    out: dict[str, list[dict]] = {}
    for name in dev_types:
        d = root / name
        if not d.is_dir():
            continue
        files = [{"filename": p.name,
                  "content_b64": base64.b64encode(p.read_bytes()).decode()}
                 for p in sorted(d.iterdir()) if p.is_file()]
        if files:
            out[name] = files
    return out


# ── validate ─────────────────────────────────────────────────────────────────

def _scrub_validation_error(e: ValidationError) -> str:
    """Paths and reasons only — pydantic's default str() embeds the offending
    INPUT, which for a secrets section would echo a value into the 422."""
    parts = []
    for err in e.errors(include_url=False, include_input=False):
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc or '<root>'}: {err.get('msg', 'invalid')}")
    return "; ".join(parts[:20])


_CONN_FIELDS = secrets_store.CONNECTION_FIELDS
_INSTANCE_RE = re.compile(_INSTANCE_NAME_RE)
_HARNESS_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def validate_bundle(bundle: dict, *, _strict: bool = True) -> dict:
    """Structural + semantic validation. Returns the parsed world:
    {config: AppConfig|None, dev_types: dict|None, prompt_templates,
    devtype_prompts, secrets, setup_env, warnings}. Raises BundleError(422)
    loudly on anything stale or malformed — never a silent drop, never an
    echoed value. _strict=False (rollback only) parses shapes but skips the
    cross-store semantic checks: restoring the world that WAS live must not
    refuse over a consistency rule the live world never had to pass."""
    if not isinstance(bundle, dict):
        raise BundleError(422, "bundle must be a mapping")
    if bundle.get("kind") != BUNDLE_KIND:
        raise BundleError(422, f"not a {BUNDLE_KIND} file")
    v = bundle.get("bundle_schema_version")
    if v != BUNDLE_SCHEMA_VERSION:
        raise BundleError(
            422, f"bundle_schema_version {v!r} is not the current version "
                 f"({BUNDLE_SCHEMA_VERSION}) — re-export from a matching "
                 "DevCake version; the bundle format is not auto-migrated")
    warnings: list[str] = []
    out: dict = {"config": None, "dev_types": None, "prompt_templates": None,
                 "devtype_prompts": None, "secrets": None, "setup_env": None,
                 "warnings": warnings}

    cfg_section = bundle.get("config")
    if cfg_section is not None:
        if not isinstance(cfg_section, dict) or not isinstance(
                cfg_section.get("app"), dict):
            raise BundleError(422, "config section must carry an 'app' mapping")
        app = dict(cfg_section["app"])
        try:
            reject_stale_patch(app)
        except ValueError as e:
            raise BundleError(422, str(e))
        # live dismissed_alerts is preserved on apply; a bundle-carried list
        # is dropped here so serialize→apply round-trips are exact
        app.pop("dismissed_alerts", None)
        try:
            cfg = AppConfig.model_validate(app)
        except ValidationError as e:
            raise BundleError(422, f"config.app invalid — "
                                   f"{_scrub_validation_error(e)}")
        dts: dict[str, DevType] = {}
        for name, data in (cfg_section.get("dev_types") or {}).items():
            if not isinstance(data, dict):
                raise BundleError(422, f"dev_types[{name!r}] must be a mapping")
            try:
                dt = DevType.model_validate({**data, "name": name})
            except ValidationError as e:
                raise BundleError(422, f"dev_types[{name!r}] invalid — "
                                       f"{_scrub_validation_error(e)}")
            dts[name] = dt
        templates = _validate_templates(cfg_section.get("prompt_templates"),
                                        warnings)
        dev_prompts = _validate_devtype_prompts(
            cfg_section.get("devtype_prompts"), dts, warnings)
        if _strict:
            validate_config_semantics(
                cfg, set(dts),
                template_exists=lambda mt, name:
                    name in prompt_templates._builtins() or name == "default"
                    or name in templates.get(mt, {}),
                check_assignments=True)
        out.update(config=cfg, dev_types=dts, prompt_templates=templates,
                   devtype_prompts=dev_prompts)

    sec = bundle.get("secrets")
    if sec is not None:
        out["secrets"] = _validate_secrets(sec, out["config"], warnings)

    env = bundle.get("setup_env")
    if env is not None:
        out["setup_env"] = _validate_setup_env(env, warnings)

    skills = bundle.get("skills")
    if skills is not None:
        emb = skills.get("embedded")
        if not isinstance(emb, list) or not all(
                isinstance(s, dict) and isinstance(s.get("name"), str)
                and isinstance(s.get("files"), list) for s in emb):
            raise BundleError(422, "skills.embedded must be a list of "
                                   "{name, files} entries")
    return out


def _validate_templates(section, warnings: list[str]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    builtins = prompt_templates._builtins()
    for mt, entries in (section or {}).items():
        if mt not in PLAYBOOK_VARS:
            raise BundleError(422, f"prompt_templates: unknown mission type {mt!r}")
        out[mt] = {}
        for name, text in (entries or {}).items():
            if name in builtins or name == "default":
                warnings.append(f"prompt_templates {mt}/{name}: built-in "
                                "preset names are canonical — bundle copy ignored")
                continue
            if not isinstance(text, str):
                raise BundleError(422, f"prompt_templates {mt}/{name}: "
                                       "template must be a string")
            try:
                prompt_templates.validate_template(mt, text)
                if not prompt_templates._NAME_RE.fullmatch(name):
                    raise ValueError("template name must match "
                                     "^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
            except ValueError as e:
                raise BundleError(422, f"prompt_templates {mt}/{name}: {e}")
            out[mt][name] = text
    return out


def _validate_devtype_prompts(section, dts: dict, warnings: list[str]
                              ) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for dev, entries in (section or {}).items():
        if dev not in dts:
            warnings.append(f"devtype_prompts[{dev!r}]: no such Dev Type in "
                            "the bundle — entries dropped")
            continue
        out[dev] = {}
        for name, text in (entries or {}).items():
            if not isinstance(text, str):
                raise BundleError(422, f"devtype_prompts {dev}/{name}: "
                                       "template must be a string")
            if not prompt_templates._NAME_RE.fullmatch(name):
                raise BundleError(422, f"devtype_prompts {dev}/{name}: bad name")
            if len(text.encode()) > 64 * 1024:
                raise BundleError(422, f"devtype_prompts {dev}/{name}: "
                                       "template exceeds 64 KB")
            out[dev][name] = text
    return out


def _validate_secrets(sec, cfg: AppConfig | None, warnings: list[str]) -> dict:
    """Shape checks; messages name keys and paths, NEVER values. Unknown-
    instance entries are kept here (the target world may differ at apply
    time) — apply/diff decide skip-with-warning against their live config."""
    if not isinstance(sec, dict):
        raise BundleError(422, "secrets section must be a mapping")
    conns: dict[str, dict[str, str]] = {}
    for key, fields in (sec.get("connections") or {}).items():
        scope, _, instance = str(key).partition("-")
        if scope not in _CONN_FIELDS or not _INSTANCE_RE.fullmatch(instance):
            raise BundleError(422, f"secrets.connections[{key!r}]: key must be "
                                   "{pmo|repo}-{instance}")
        if not isinstance(fields, dict):
            raise BundleError(422, f"secrets.connections[{key!r}] must be a mapping")
        for field, value in fields.items():
            if field not in _CONN_FIELDS[scope]:
                raise BundleError(
                    422, f"secrets.connections[{key!r}]: field {field!r} not in "
                         f"{sorted(_CONN_FIELDS[scope])}")
            if not isinstance(value, str) or not value:
                raise BundleError(
                    422, f"secrets.connections[{key!r}].{field}: value must be "
                         "a non-empty string")
        conns[key] = dict(fields)
    harness: dict[str, str] = {}
    for var, value in (sec.get("harness") or {}).items():
        if not _HARNESS_RE.fullmatch(str(var)):
            raise BundleError(422, f"secrets.harness[{var!r}]: var must match "
                                   "^[A-Z][A-Z0-9_]{0,63}$")
        if not isinstance(value, str) or not value:
            raise BundleError(422, f"secrets.harness[{var}]: value must be a "
                                   "non-empty string")
        harness[var] = value
    out: dict = {"connections": conns, "harness": harness}
    cred = sec.get("credential_files")
    if cred is not None:
        if not isinstance(cred, dict):
            raise BundleError(422, "secrets.credential_files must be a mapping")
        for dev, files in cred.items():
            for f in files or []:
                fname = str((f or {}).get("filename") or "")
                if not fname or os.path.basename(fname) != fname:
                    raise BundleError(
                        422, f"secrets.credential_files[{dev!r}]: filenames "
                             "must be bare basenames")
        out["credential_files"] = cred
    if cfg is not None:
        known = ({f"pmo-{p.name}" for p in cfg.pmos}
                 | {f"repo-{r.name}" for r in cfg.repos})
        for key in conns:
            if key not in known:
                warnings.append(f"secrets.connections[{key}]: no such instance "
                                "in the bundle config — skipped at apply")
    return out


def _validate_setup_env(env, warnings: list[str]) -> dict:
    if not isinstance(env, dict) or not isinstance(env.get("values"), dict):
        raise BundleError(422, "setup_env section must carry a 'values' mapping")
    values: dict[str, str] = {}
    for name, value in env["values"].items():
        if name == "DEVCAKE_ALLOW_INSECURE":
            warnings.append("setup_env: DEVCAKE_ALLOW_INSECURE never rides a "
                            "bundle — dropped")
            continue
        if name not in _SETUP_ENV_NAMES:
            warnings.append(f"setup_env: unknown variable {name!r} — dropped")
            continue
        if not isinstance(value, str):
            raise BundleError(422, f"setup_env.values[{name}]: must be a string")
        values[name] = value
    return {"values": values,
            "host_specific": [n for n, host in SETUP_ENV_VARS if host]}


# ── transfer helpers (export/import; ADR-0013 decisions 1/2/5) ───────────────

def protect_bundle(bundle: dict, passphrase: str) -> dict:
    """Move the secret-bearing sections (B and C together) into ONE encrypted
    `protected` envelope; section A stays plaintext/diffable."""
    from . import settings_crypto   # lazy: keep the module import-light
    secretish = {k: bundle[k] for k in ("secrets", "setup_env") if k in bundle}
    if not secretish:
        return bundle
    out = {k: v for k, v in bundle.items()
           if k not in ("secrets", "setup_env", "plaintext_secrets")}
    out["protected"] = settings_crypto.encrypt_blob(
        passphrase, json.dumps(secretish).encode())
    return out


def unprotect_bundle(bundle: dict, passphrase: str) -> dict:
    """Inverse of protect_bundle. Raises settings_crypto.DecryptError on a
    wrong passphrase or tampering (one indistinguishable message)."""
    from . import settings_crypto
    plaintext = settings_crypto.decrypt_blob(passphrase, bundle["protected"])
    try:
        sections = json.loads(plaintext)
        assert isinstance(sections, dict)
    except Exception:  # noqa: BLE001 — any parse/shape failure of decrypted bytes maps to one typed 422; never leaks internals
        raise BundleError(422, "decrypted payload is not a bundle section map")
    out = {k: v for k, v in bundle.items() if k != "protected"}
    out.update(sections)
    return out


def generate_env_file(setup_env: dict) -> str:
    """The C-import artifact: a ready-to-place .env. The app cannot write the
    host's .env — the operator downloads this, reviews the host-specific
    lines, and restarts the stack."""
    values = setup_env.get("values") or {}
    lines = [
        "# DevCake .env — generated from a settings bundle (ADR-0013).",
        "# 1. Review the HOST-SPECIFIC lines below for THIS host.",
        "# 2. Place this file at the repo root as `.env`.",
        "# 3. `docker compose up -d` to restart the stack with these values.",
        "# Values reflect the source stack at container start — .env edits",
        "# made there after boot are not included. Handle like a password",
        "# export; delete this file once placed.",
        "",
    ]
    hints = {
        "DOCKER_GID": "# HOST-SPECIFIC — verify: stat -c %g /var/run/docker.sock",
        "DEVCAKE_TAG": "# HOST-SPECIFIC — must match your docker buildx bake tag",
    }
    for name, host in SETUP_ENV_VARS:
        if host:
            lines.append(hints.get(name, "# HOST-SPECIFIC — verify for this host"))
        if name in values:
            lines.append(f"{name}={values[name]}")
        else:
            lines.append(f"# {name}=    # not set on the source stack")
    return "\n".join(lines) + "\n"


# ── choke points shared with PUT /config (extracted from api.main) ───────────

def validate_config_semantics(cfg: AppConfig, dev_type_names: set[str],
                              template_exists: Callable[[str, str], bool],
                              *, check_assignments: bool = False) -> None:
    """Cross-store checks shared by PUT /config and bundle apply.
    template_exists decides against the caller's template universe — disk
    for the PUT, bundle ∪ builtins for an apply. check_assignments is
    apply-only: the PUT keeps today's split where PUT /assignments owns that
    validation (the SPA saves assignments separately, possibly before a new
    Dev Type lands)."""
    rm = cfg.relations_mapper
    if rm.enabled and (not rm.dev_type or rm.dev_type not in dev_type_names):
        raise BundleError(422, "relations_mapper.dev_type must name an "
                               "existing Dev Type when the mapper is enabled")
    for mt, name in (cfg.active_prompt_templates or {}).items():
        if mt not in PLAYBOOK_VARS:
            raise BundleError(422, f"active_prompt_templates: unknown "
                                   f"mission type {mt!r}")
        if not template_exists(mt, name):
            raise BundleError(422, f"active_prompt_templates: no stored "
                                   f"template {mt}/{name}")
    for dt_name in (cfg.active_devtype_prompts or {}):
        if dt_name not in dev_type_names:
            raise BundleError(422, f"active_devtype_prompts: unknown "
                                   f"Dev Type {dt_name!r}")
    if check_assignments:
        for mt, a in (cfg.assignments or {}).items():
            if a.dev_type and a.dev_type not in dev_type_names:
                raise BundleError(422, f"assignments[{mt}]: unknown Dev Type "
                                       f"{a.dev_type!r}")


def dry_run_adapters(cfg: AppConfig) -> None:
    """Construct every adapter before anything persists (ISSUES #11): a bad
    URL or forge shape must never land on disk. Unconfigured instances are
    valid-but-idle."""
    from .adapters.registry import make_forge, make_pmo  # lazy: import-light
    try:
        for inst in cfg.pmos:
            make_pmo(inst)
        for repo in cfg.repos:
            if repo.configured:
                make_forge(repo)
    except Exception as e:
        raise BundleError(422, f"adapter construction failed: {e}") from e


# ── diff (preview) ───────────────────────────────────────────────────────────

def diff_bundle(bundle: dict, config: AppConfig,
                dev_types: dict[str, DevType]) -> dict:
    """Preview payload for apply/import confirms. Secrets are PRESENCE AND
    NAMES only — never a value, never a fingerprint (ADR-0011)."""
    parsed = validate_bundle(bundle)
    warnings = list(parsed["warnings"])
    out: dict = {"sections": {}, "warnings": warnings}

    if parsed["config"] is not None:
        cur = config.model_dump()
        cur.pop("dismissed_alerts", None)
        new = parsed["config"].model_dump()
        new.pop("dismissed_alerts", None)
        changed = sorted(k for k in new if new[k] != cur.get(k))
        cur_dts = {n: dt.model_dump() for n, dt in dev_types.items()}
        new_dts = {n: dt.model_dump() for n, dt in parsed["dev_types"].items()}
        cur_tpl = {(mt, e["name"])
                   for mt, es in prompt_templates.list_templates().items()
                   for e in es if not e["builtin"]}
        new_tpl = {(mt, name) for mt, es in parsed["prompt_templates"].items()
                   for name in es}
        out["sections"]["config"] = {
            "app_changed": changed,
            "dev_types": _delta(cur_dts, new_dts),
            "prompt_templates": {
                "added": sorted(f"{mt}/{n}" for mt, n in new_tpl - cur_tpl),
                "removed": sorted(f"{mt}/{n}" for mt, n in cur_tpl - new_tpl),
            },
        }
        if new.get("intake_paused") != cur.get("intake_paused"):
            state = "paused" if new.get("intake_paused") else "UNPAUSED"
            warnings.append(f"applying changes intake to {state}")

    if parsed["secrets"] is not None:
        cur_conns = secrets_store.list_connection_secrets()
        cur_flat = {f"{k}.{f}" for k, fs in cur_conns.items() for f in fs}
        new_flat = {f"{k}.{f}" for k, fs in parsed["secrets"]["connections"].items()
                    for f in fs}
        cur_h = set(secrets_store.list_harness_secrets())
        new_h = set(parsed["secrets"]["harness"])
        out["sections"]["secrets"] = {
            "connections": {"added": sorted(new_flat - cur_flat),
                            "replaced": sorted(new_flat & cur_flat),
                            "removed": sorted(cur_flat - new_flat)},
            "harness": {"added": sorted(new_h - cur_h),
                        "replaced": sorted(new_h & cur_h),
                        "removed": sorted(cur_h - new_h)},
        }
        warnings.extend(_newer_secret_warnings(bundle, parsed))

    if parsed["setup_env"] is not None:
        out["sections"]["setup_env"] = {
            "keys": sorted(parsed["setup_env"]["values"]),
            "host_specific": parsed["setup_env"]["host_specific"],
        }
    if bundle.get("skills"):
        out["sections"]["skills"] = {
            "embedded": sorted(s["name"] for s in bundle["skills"]["embedded"])}
    return out


def _delta(cur: dict, new: dict) -> dict:
    return {"added": sorted(set(new) - set(cur)),
            "changed": sorted(k for k in set(new) & set(cur)
                              if new[k] != cur[k]),
            "removed": sorted(set(cur) - set(new))}


def _newer_secret_warnings(bundle: dict, parsed: dict) -> list[str]:
    """The rotation trap (ADR-0013): a live secret rotated AFTER the snapshot
    was captured would be silently replaced by the older value. Timestamps
    only — never values."""
    created = str(bundle.get("created_at") or "")
    if not created:
        return []
    warns = []
    for key, fields in parsed["secrets"]["connections"].items():
        scope, _, instance = key.partition("-")
        for field in fields:
            st = secrets_store.connection_status(scope, instance, field)
            if st["present"] and st["updated_at"] and st["updated_at"] > created:
                warns.append(f"secret {key}.{field} was updated after this "
                             "snapshot was captured — applying restores the "
                             "older value")
    for var in parsed["secrets"]["harness"]:
        st = secrets_store.harness_status(var)
        if st["present"] and st["updated_at"] and st["updated_at"] > created:
            warns.append(f"secret harness/{var} was updated after this "
                         "snapshot was captured — applying restores the "
                         "older value")
    return warns


# ── apply ────────────────────────────────────────────────────────────────────

def apply_bundle(bundle: dict, *, config: AppConfig,
                 dev_types: dict[str, DevType],
                 reload: Callable[[], None],
                 _is_rollback: bool = False) -> dict:
    """REPLACE the live world with the bundle's sections. Synchronous end to
    end — no awaits between validation and the in-memory swap, so the asyncio
    poll loop can never observe a half-applied world. The caller holds the
    runs-active guard.

    Ordering (crash honesty, ADR-0013): additive file writes first, then the
    config.yaml commit point, then deletions. A crash before the commit
    leaves the old world authoritative with inert extra files; after it, the
    new world is authoritative and a re-apply prunes the stale extras.
    """
    parsed = validate_bundle(bundle, _strict=not _is_rollback)
    warnings = list(parsed["warnings"])
    new_cfg: AppConfig | None = parsed["config"]
    if new_cfg is not None and not _is_rollback:
        dry_run_adapters(new_cfg)

    previous = serialize_current(
        config, dev_types,
        include_config=new_cfg is not None,
        include_secrets=parsed["secrets"] is not None)

    applied: list[str] = []
    try:
        if new_cfg is not None:
            _apply_config_files(parsed)
        if parsed["secrets"] is not None:
            warnings.extend(_apply_secrets(
                parsed["secrets"],
                new_cfg if new_cfg is not None else config,
                restore_exact=_is_rollback))
        if new_cfg is not None:
            # commit point: config.yaml swaps the authoritative world
            live_alerts = list(config.dismissed_alerts)
            for field in type(new_cfg).model_fields:
                setattr(config, field, getattr(new_cfg, field))
            config.dismissed_alerts = live_alerts
            save_config(config)
            _prune_config_files(parsed, dev_types)
            dev_types.clear()               # shared by reference with managers
            dev_types.update(parsed["dev_types"])
            prompt_templates.seed_devtype_prompts(dev_types)
            applied.append("config")
        if parsed["secrets"] is not None:
            applied.append("secrets")
        if _is_rollback:
            # files restored is the load-bearing part — a rollback reload
            # failure must not abort it (put_config's "restore reload also
            # failed" semantics); adapters heal at the next config change
            try:
                reload()
            except Exception:
                log.exception("rollback reload failed — files restored")
        else:
            reload()
    except BundleError:
        raise
    except Exception as e:
        if _is_rollback:
            raise
        log.exception("bundle apply failed — restoring previous settings")
        try:
            apply_bundle(previous, config=config, dev_types=dev_types,
                         reload=reload, _is_rollback=True)
        except Exception:
            log.exception("rollback also failed")
            raise BundleError(
                500, "apply failed AND the rollback failed — the deployment "
                     "may hold a mix of old and new settings; re-apply "
                     "either the profile or your last export to converge "
                     f"(apply error: {e})")
        raise BundleError(500, f"apply failed; previous settings restored: {e}")
    return {"applied": applied, "warnings": warnings,
            "untouched": ["runs", "dev-type credential files",
                          "internal forge", "profiles"]}


def _apply_config_files(parsed: dict) -> None:
    """Additive writes through the existing per-store choke points — each
    file individually atomic, validation stays load-bearing."""
    for dt in parsed["dev_types"].values():
        save_dev_type(dt)
    for mt, entries in parsed["prompt_templates"].items():
        for name, text in entries.items():
            prompt_templates.save_template(mt, name, text)
    for dev, entries in parsed["devtype_prompts"].items():
        for name, text in entries.items():
            prompt_templates.save_devtype_prompt(dev, name, text)


def _prune_config_files(parsed: dict, old_dev_types: dict) -> None:
    """Replace-the-world deletions, post-commit. Never touches dev-type
    credential dirs, internal_forge/, profiles/, or /data/state. A pruned
    seeded default Dev Type returns at next boot (load_dev_types top-up) —
    documented, and the divergence indicator surfaces it honestly."""
    import shutil
    for name in set(old_dev_types) - set(parsed["dev_types"]):
        delete_dev_type(name)
    builtins = prompt_templates._builtins()
    for mt, entries in prompt_templates.list_templates().items():
        keep = set(parsed["prompt_templates"].get(mt, {}))
        for e in entries:
            if not e["builtin"] and e["name"] not in keep:
                prompt_templates.delete_template(mt, e["name"])
    for d in prompt_templates.known_devtype_dirs():
        if d.name not in parsed["dev_types"]:
            shutil.rmtree(d, ignore_errors=True)
            continue
        keep = set(parsed["devtype_prompts"].get(d.name, {})) | set(builtins)
        keep.add("Development")             # seeded user data, never pruned
        for p in d.glob("*.yaml"):
            if p.stem not in keep:
                p.unlink(missing_ok=True)


def _apply_secrets(sec: dict, target_cfg: AppConfig, *,
                   restore_exact: bool = False) -> list[str]:
    """Replace the secret stores. Unknown-instance entries are SKIPPED with a
    warning — never an orphan secret file on disk (ADR-0013 hardening).
    restore_exact (rollback only) disables the skip: a live world may
    legitimately hold a secret whose instance card isn't saved yet, and a
    rollback must restore byte-exact, not editorialize."""
    warnings: list[str] = []
    known = ({f"pmo-{p.name}" for p in target_cfg.pmos}
             | {f"repo-{r.name}" for r in target_cfg.repos})
    wanted: dict[str, dict[str, str]] = {}
    for key, fields in sec["connections"].items():
        if key not in known and not restore_exact:
            warnings.append(f"secrets.connections[{key}]: no such configured "
                            "instance — skipped")
            continue
        wanted[key] = fields
    current = secrets_store.list_connection_secrets()
    for key in set(current) - set(wanted):
        scope, _, instance = key.partition("-")
        secrets_store.delete_connection_instance(scope, instance)
    for key, fields in wanted.items():
        scope, _, instance = key.partition("-")
        for field in set(current.get(key, {})) - set(fields):
            secrets_store.delete_connection_field(scope, instance, field)
        for field, value in fields.items():
            secrets_store.write_connection_secret(scope, instance, field, value)
    cur_h = secrets_store.list_harness_secrets()
    for var in set(cur_h) - set(sec["harness"]):
        secrets_store.delete_harness_secret(var)
    for var, value in sec["harness"].items():
        secrets_store.write_harness_secret(var, value)
    return warnings
