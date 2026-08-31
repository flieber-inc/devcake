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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from opentelemetry import trace
from pydantic import ValidationError

from . import secrets as secrets_store
from .config import (AppConfig, DevType, HARNESS_VAR_PATTERN,
                     _INSTANCE_NAME_RE, delete_dev_type,
                     reconcile_managed_pmos, reconcile_reserved_crons,
                     reject_stale_patch, save_config, save_dev_type,
                     validate_memory_bindings)
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
    # ADR-0025: host-absolute workspace base — devcake up re-derives it on the
    # target host, so an exported value is verification-only like DOCKER_GID
    ("DEVCAKE_WS_HOST", True),
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
                      include_orphan_secrets: bool = False,
                      skill_payloads: list[dict] | None = None) -> dict:
    """Snapshot the live world into a bundle dict. dismissed_alerts is
    STRIPPED (importing someone else's dismissals could hide active
    advisories); credential files are export-only opt-in — host-migration
    material that "save current as profile" deliberately excludes (no
    plaintext OAuth copy per casual snapshot) but apply now HONORS wherever
    the section appears (SEC-2: the old one-way door validated them on
    import and then silently never wrote them).

    ``include_orphan_secrets`` (default False): user-facing snapshots
    (profiles, export) drop connection secrets for instances not in the
    config — they are not part of the reproducible world (audit D5 #15/#16).
    The apply/rollback path sets it True so the pre-apply snapshot is
    BYTE-EXACT — a rollback must restore secrets stored for an instance the
    incoming bundle was about to add (re-audit #3)."""
    # /data/harness_receipts/, harness_bake_status.json and
    # harness_baker.jsonl are host-local factory output — never a bundle
    # section. Importing someone else's receipts would lie about this
    # tree's digest.
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
        # Drop orphan active_devtype_prompts keys for deleted Dev Types so
        # export/profile snapshots stay applyable (do not mutate live config).
        live_dts = set(dev_types)
        app["active_devtype_prompts"] = {
            k: v for k, v in (app.get("active_devtype_prompts") or {}).items()
            if k in live_dts}
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
        # Exclude ORPHAN connection secrets — stored for an instance no longer
        # in the config (audit D5 #15/#16): a lingering repo-bare.token from a
        # deleted card would otherwise ride the snapshot, show as "replaced" in
        # the apply preview yet be dropped by the apply write, and flag the
        # profile permanently diverged. A snapshot reproduces the CONFIG's
        # world; an instance-less secret is not part of it. The live orphan
        # itself is untouched (it has its own lifecycle — docs/18).
        # EXCEPTION: the rollback path (include_orphan_secrets=True) keeps them
        # so the pre-apply snapshot is byte-exact and a rollback loses nothing
        # (re-audit #3).
        all_conns = secrets_store.list_connection_secrets()
        if include_orphan_secrets:
            conns = all_conns
        else:
            live_keys = secrets_store.connection_instance_keys(config)
            conns = {k: v for k, v in all_conns.items() if k in live_keys}
        sec: dict = {
            "connections": conns,
            "harness": secrets_store.list_harness_secrets(),
        }
        if include_credential_files:
            sec["credential_files"] = _read_credential_files(
                dev_types, include_orphans=include_orphan_secrets)
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


def _read_credential_files(dev_types: dict, *,
                           include_orphans: bool = False) -> dict[str, list[dict]]:
    """include_orphans (the rollback snapshot, re-audit #3 precedent):
    enumerate via inventory() rather than the dev_types dict, so a rollback
    restores byte-exact including dirs whose dev type isn't in config."""
    import base64
    root = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets"
    if include_orphans:
        names = sorted({cf["dev_type"] for cf in
                        secrets_store.inventory()["credential_files"]})
    else:
        names = list(dev_types)
    out: dict[str, list[dict]] = {}
    for name in names:
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
_HARNESS_RE = re.compile(f"^{HARNESS_VAR_PATTERN}$")   # one definition: config.py


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
        incoming_dts = dict(cfg_section.get("dev_types") or {})
        # MAPPER→STEWARD (2026-08-06): bundles exported before the rename
        # carry a "mapper" dev type — import it under its new name (a bundle
        # that somehow carries both keeps the explicit "steward")
        if "mapper" in incoming_dts:
            incoming_dts.setdefault("steward", incoming_dts["mapper"])
            del incoming_dts["mapper"]
        for name, data in incoming_dts.items():
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
                warnings=warnings, dev_types=dts)
        else:
            # rollback path: still drop impossible active_devtype_prompts
            # keys so a restored world does not re-poison PUT /config
            prune_stale_active_devtype_prompts(cfg, set(dts))
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
                                   "{pmo|repo|skill}-{instance}")
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
                                   f"^{HARNESS_VAR_PATTERN}$")
        if not isinstance(value, str) or not value:
            raise BundleError(422, f"secrets.harness[{var}]: value must be a "
                                   "non-empty string")
        harness[var] = value
    out: dict = {"connections": conns, "harness": harness}
    cred = sec.get("credential_files")
    if cred is not None:
        if not isinstance(cred, dict):
            raise BundleError(422, "secrets.credential_files must be a mapping")
        import base64
        out_cred: dict[str, list[dict]] = {}
        for dev, files in cred.items():
            rows: list[dict] = []
            for f in files or []:
                fname = str((f or {}).get("filename") or "")
                try:
                    # THE path-safety gate — same one the upload endpoint and
                    # the OAuth writer use (reserved dirs, traversal, shape);
                    # the old bespoke basename check is gone (SEC-2)
                    secrets_store.require_credential_ref(str(dev), fname)
                except ValueError as e:
                    raise BundleError(
                        422, f"secrets.credential_files[{dev!r}]: {e}")
                b64 = (f or {}).get("content_b64")
                if not isinstance(b64, str):
                    raise BundleError(
                        422, f"secrets.credential_files[{dev}/{fname}]: "
                             "content_b64 must be a string")
                try:
                    raw = base64.b64decode(b64, validate=True)
                    content = raw.decode("utf-8")
                except Exception:  # noqa: BLE001 — any decode failure maps to one typed 422; never echoes the payload
                    raise BundleError(
                        422, f"secrets.credential_files[{dev}/{fname}]: "
                             "content_b64 must decode to UTF-8 text")
                if len(raw) > secrets_store.MAX_CREDENTIAL_FILE_BYTES:
                    raise BundleError(
                        422, f"secrets.credential_files[{dev}/{fname}]: "
                             f"exceeds {secrets_store.MAX_CREDENTIAL_FILE_BYTES}"
                             f" bytes")
                rows.append({"filename": fname, "content": content})
            out_cred[str(dev)] = rows
        out["credential_files"] = out_cred
    if cfg is not None:
        known = secrets_store.connection_instance_keys(cfg)
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
    wrong passphrase or tampering (one indistinguishable message).

    The envelope may carry only B and C (`secrets`, `setup_env`). Any other
    key (notably `config`) is refused so a crafted ciphertext cannot rewrite
    the operator-visible plaintext section A after passphrase entry. Outer
    YAML `secrets`/`setup_env`/`plaintext_secrets` are discarded whenever
    `protected` is present — the envelope is the sole source of B/C.
    """
    from . import settings_crypto
    plaintext = settings_crypto.decrypt_blob(passphrase, bundle["protected"])
    try:
        sections = json.loads(plaintext)
        if not isinstance(sections, dict):
            raise ValueError("not a mapping")
    except Exception:  # noqa: BLE001 — any parse/shape failure of decrypted bytes maps to one typed 422; never leaks internals
        raise BundleError(422, "decrypted payload is not a bundle section map")
    allowed = ("secrets", "setup_env")
    unknown = sorted(set(sections) - set(allowed))
    if unknown:
        raise BundleError(
            422, "protected payload may only carry secrets and setup_env — "
                 f"unexpected keys: {', '.join(unknown)}")
    # Drop outer B/C and the plaintext flag: envelope is authoritative.
    out = {k: v for k, v in bundle.items()
           if k not in ("protected", "secrets", "setup_env", "plaintext_secrets")}
    for key in allowed:
        if key in sections:
            out[key] = sections[key]
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
        "DEVCAKE_WS_HOST": "# HOST-SPECIFIC — absolute path; devcake up re-derives it",
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

def prune_stale_active_devtype_prompts(cfg: AppConfig,
                                       dev_type_names: set[str]) -> list[str]:
    """Drop active_devtype_prompts keys for Dev Types that no longer exist.

    Missing key ⇒ "Development" at resolve time, so pruning is safe. deep_merge
    cannot delete dict keys on a partial PUT, and remove_dev_type historically
    left orphans — callers heal here instead of 422. Mutates cfg in place;
    returns human-readable warnings for any dropped key.
    """
    active = cfg.active_devtype_prompts or {}
    stale = sorted(n for n in list(active) if n not in dev_type_names)
    for name in stale:
        active.pop(name, None)
    cfg.active_devtype_prompts = active
    return [f"active_devtype_prompts: dropped unknown Dev Type {n!r}"
            for n in stale]


def assert_assignment_dev_types(assignments, dev_type_names: set[str],
                                *, prefix: str = "assignments") -> None:
    """THE unknown-Dev-Type check (PUT /assignments + boot/bundle)."""
    for mt, a in assignments.items():
        name = a.dev_type if hasattr(a, "dev_type") else a.get("dev_type")
        if name not in dev_type_names:
            raise BundleError(422, f"{prefix}[{mt}]: unknown Dev Type "
                                   f"{name!r}")


def validate_config_semantics(cfg: AppConfig, dev_type_names: set[str],
                              template_exists: Callable[[str, str], bool],
                              *, warnings: list[str] | None = None,
                              dev_types: dict | None = None) -> None:
    """Cross-store checks shared by boot, PUT /config, and bundle apply —
    ONE path, no caller-specific carve-outs (2026-08-12 audit SEC-3; the old
    `check_assignments` flag made the bundle path strict and the PUT path
    blind, and the two had already drifted on empty dev_type). The SPA saves
    Dev Types before the config PUT so a draft that creates-and-references a
    Dev Type in one Save still validates (DraftChrome ordering note).
    template_exists decides against the caller's template universe — disk
    for the PUT/boot, bundle ∪ builtins for an apply.

    Stale active_devtype_prompts keys are pruned (with optional warnings),
    never hard-422 — delete/rename/export paths must stay healable after an
    incomplete Dev Type removal.
    """
    rm = cfg.steward
    if rm.enabled and (not rm.dev_type or rm.dev_type not in dev_type_names):
        raise BundleError(422, "steward.dev_type must name an "
                               "existing Dev Type when the steward is enabled")
    for mt, name in (cfg.active_prompt_templates or {}).items():
        if mt not in PLAYBOOK_VARS:
            raise BundleError(422, f"active_prompt_templates: unknown "
                                   f"mission type {mt!r}")
        if not template_exists(mt, name):
            raise BundleError(422, f"active_prompt_templates: no stored "
                                   f"template {mt}/{name}")
    for inst in cfg.pmos:                     # CAKE-150 / ADR-0037 overrides
        for mt, name in (inst.active_prompt_templates or {}).items():
            if mt not in PLAYBOOK_VARS:
                raise BundleError(
                    422, f"pmos[{inst.name}].active_prompt_templates: "
                         f"unknown mission type {mt!r}")
            if not template_exists(mt, name):
                raise BundleError(
                    422, f"pmos[{inst.name}].active_prompt_templates: "
                         f"no stored template {mt}/{name}")
    dropped = prune_stale_active_devtype_prompts(cfg, dev_type_names)
    if warnings is not None:
        warnings.extend(dropped)
    elif dropped:
        for msg in dropped:
            log.warning(msg)
    # Empty assignments = unstaffed (CAKE-164); non-empty maps must name
    # existing Dev Types. Shape completeness is the model validator's job.
    assert_assignment_dev_types(cfg.assignments or {}, dev_type_names)
    for inst in cfg.pmos:                     # ADR-0019 override maps
        assert_assignment_dev_types(
            inst.assignments, dev_type_names,
            prefix=f"pmos[{inst.name}].assignments")
    # I2 with Dev Type memory_repos — AppConfig validation only sees
    # instance lists; this is the cross-store chokepoint.
    validate_memory_bindings(cfg, dev_types=dev_types)


def dry_run_adapters(cfg: AppConfig) -> None:
    """Construct every adapter before anything persists: a bad URL or forge
    shape must never land on disk. Unconfigured instances are valid-but-idle."""
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
        # per-PMO intake (same ADR-0013 "flag intake state" spirit as the master)
        cur_pmo = {p["name"]: bool(p.get("intake_paused"))
                   for p in (cur.get("pmos") or []) if p.get("name")}
        new_pmo = {p["name"]: bool(p.get("intake_paused"))
                   for p in (new.get("pmos") or []) if p.get("name")}
        for name in sorted(set(cur_pmo) | set(new_pmo)):
            if cur_pmo.get(name) != new_pmo.get(name) and name in new_pmo:
                state = "paused" if new_pmo[name] else "UNPAUSED"
                warnings.append(f"applying changes intake for PMO '{name}' to {state}")

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
        cred = parsed["secrets"].get("credential_files")
        if cred is not None:
            # names only ("dev/file"), never content — same ADR-0011 rule
            cur_cred = {f'{cf["dev_type"]}/{cf["filename"]}' for cf in
                        secrets_store.inventory()["credential_files"]}
            new_cred = {f"{dev}/{f['filename']}"
                        for dev, files in cred.items() for f in files}
            out["sections"]["secrets"]["credential_files"] = {
                "added": sorted(new_cred - cur_cred),
                "replaced": sorted(new_cred & cur_cred),
                "removed": sorted(cur_cred - new_cred),
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
    cred = parsed["secrets"].get("credential_files")
    if cred:
        inv = {(cf["dev_type"], cf["filename"]): cf["updated_at"]
               for cf in secrets_store.inventory()["credential_files"]}
        for dev, files in cred.items():
            for f in files:
                ts = inv.get((dev, f["filename"]))
                if ts and ts > created:
                    warns.append(
                        f"credential file {dev}/{f['filename']} was updated "
                        "after this snapshot was captured — applying "
                        "restores the older version (a re-OAuth since the "
                        "export would be overwritten)")
    return warns


# ── apply ────────────────────────────────────────────────────────────────────

def apply_bundle(bundle: dict, *, config: AppConfig,
                 dev_types: dict[str, DevType],
                 reload: Callable[[], None],
                 managers: dict | None = None,
                 _is_rollback: bool = False) -> dict:
    """REPLACE the live world with the bundle's sections. Synchronous end to
    end — no awaits between validation and the in-memory swap, so the asyncio
    poll loop can never observe a half-applied world. The caller holds the
    runs-active guard.

    Ordering (crash honesty, ADR-0013; made true by the 2026-08-12 audit
    SEC-1 fix — deletions used to run before the commit): three phases —
      1. ADDITIVE writes: config files, then secret keys the old world
         never stored (_apply_secret_adds);
      2. the config.yaml COMMIT POINT (in-memory swap + save_config +
         config-file prunes);
      3. DESTRUCTIVE secret ops (_apply_secret_destructive): overwrites,
         then deletions.
    Invariant: a crash BEFORE the commit never modifies or removes any value
    the old world stored — the old config stays authoritative over a store
    with, at worst, inert extra keys *and extra config files for names that
    did not exist*, pruned by a re-apply. Existing ``dev_types/`` and
    template files are overwritten only AFTER ``save_config``. After the
    commit the new world is authoritative and a re-apply converges the
    stragglers.
    A secrets-only apply has no config.yaml commit; its commit point is the
    ADD→MUT boundary, with the correspondingly weaker (but honest)
    guarantee: every file replacement is individually atomic, no stored
    value is modified before all additions are durable, no deletion happens
    before all writes, and a crash leaves a well-formed mixed key set that
    re-applying either bundle converges. One residual, named honestly: an
    ADD-phase secret for an instance the old config has but never stored a
    value for becomes visible to the old world after a crash — additive,
    never a modification.
    """
    parsed = validate_bundle(bundle, _strict=not _is_rollback)
    warnings = list(parsed["warnings"])
    new_cfg: AppConfig | None = parsed["config"]
    if new_cfg is not None and not _is_rollback:
        # ADR-0030: profiles saved BEFORE the managed-board feature carry no
        # board row — applying one must not delete the instance (and, at the
        # next boot's provisioning, resurrect it with a fresh identity while
        # its PAT still exists). Rollback re-applies serialize_current output,
        # which already carries the row — skip there.
        incoming = [p.model_dump() for p in new_cfg.pmos]
        reconciled = reconcile_managed_pmos(
            [p.model_dump() for p in config.pmos], incoming,
            internal_forge_present=bool(
                os.environ.get("GITEA_ADMIN_PASSWORD")))
        if reconciled != incoming:
            new_cfg = AppConfig.model_validate(
                {**new_cfg.model_dump(), "pmos": reconciled})
            parsed["config"] = new_cfg
        incoming_crons = [c.model_dump() for c in new_cfg.crons]
        reconciled_crons = reconcile_reserved_crons(
            [c.model_dump() for c in config.crons], incoming_crons)
        if reconciled_crons != incoming_crons:
            new_cfg = AppConfig.model_validate(
                {**new_cfg.model_dump(), "crons": reconciled_crons})
            parsed["config"] = new_cfg
        dry_run_adapters(new_cfg)

    cred_in_bundle = (parsed["secrets"] is not None
                      and parsed["secrets"].get("credential_files") is not None)
    previous = serialize_current(
        config, dev_types,
        include_config=new_cfg is not None,
        include_secrets=parsed["secrets"] is not None,
        # a failed apply that already overwrote grok-auth.json must be able
        # to roll it back — snapshot the group iff the bundle touches it
        include_credential_files=cred_in_bundle,
        include_orphan_secrets=True)   # byte-exact rollback (re-audit #3)
    # snapshot auto_merge before the in-memory swap so OFF→ON re-arm works
    # for profile/import apply the same way as PUT /config (ADR-0020)
    prev_repos = list(config.repos) if new_cfg is not None else []

    applied: list[str] = []
    # the plan is computed inside the same synchronous, lock-held window as
    # the phases that execute it — it cannot go stale
    ops = (_plan_secret_ops(parsed["secrets"],
                            new_cfg if new_cfg is not None else config,
                            target_dev_types=set(
                                parsed["dev_types"] if new_cfg is not None
                                else dev_types),
                            restore_exact=_is_rollback)
           if parsed["secrets"] is not None else None)
    if ops is not None:
        warnings.extend(ops.warnings)
    # snapshot which config files already exist BEFORE any write, so the
    # ADD pass and the overwrite pass agree on what was pre-existing — else
    # the ADD pass creates a file the overwrite pass then re-writes (a
    # harmless but wasteful double write). One snapshot = each file written
    # exactly once, in exactly its phase.
    pre_existing = _config_files_on_disk(parsed) if new_cfg is not None else set()
    try:
        if new_cfg is not None:
            _apply_config_files(parsed, pre_existing, existing_only=False)
        if ops is not None:
            _apply_secret_adds(ops)          # phase ADD — pre-commit safe
        if new_cfg is not None:
            # commit point: config.yaml swaps the authoritative world
            live_alerts = list(config.dismissed_alerts)
            for field in type(new_cfg).model_fields:
                setattr(config, field, getattr(new_cfg, field))
            config.dismissed_alerts = live_alerts
            save_config(config)
            _apply_config_files(parsed, pre_existing, existing_only=True)
            _prune_config_files(parsed, dev_types)
            dev_types.clear()               # shared by reference with managers
            dev_types.update(parsed["dev_types"])
            from .keep_set import publish_keep_set
            publish_keep_set(dev_types)
            prompt_templates.seed_devtype_prompts(dev_types)
            applied.append("config")
        if ops is not None:
            _apply_secret_destructive(ops)   # phase MUT — post-commit only
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
            if new_cfg is not None and not _is_rollback:
                # same OFF→ON re-arm as PUT /config — ONE implementation in
                # config.py (ADR-0020); no api import (module stays import-light)
                from .config import apply_auto_merge_rearm
                apply_auto_merge_rearm(prev_repos, config.repos, managers)
    except BundleError:
        raise
    except Exception as e:
        if _is_rollback:
            raise
        log.exception("bundle apply failed — restoring previous settings")
        try:
            apply_bundle(previous, config=config, dev_types=dev_types,
                         reload=reload, managers=None, _is_rollback=True)
        except Exception:
            log.exception("rollback also failed")
            raise BundleError(
                500, "apply failed AND the rollback failed — the deployment "
                     "may hold a mix of old and new settings; re-apply "
                     "either the profile or your last export to converge "
                     f"(apply error: {e})")
        raise BundleError(500, f"apply failed; previous settings restored: {e}")
    untouched = ["runs", "internal forge", "profiles"]
    if not (ops is not None and ops.cred_section_present):
        untouched.insert(1, "dev-type credential files")
    return {"applied": applied, "warnings": warnings, "untouched": untouched}


def _config_files_on_disk(parsed: dict) -> set:
    """The (kind, name) config-file keys that exist on disk RIGHT NOW — the
    pre-apply snapshot both _apply_config_files passes key on (see there)."""
    from .config import CONFIG_PATH
    dt_dir = CONFIG_PATH.parent / "dev_types"
    out: set = set()
    for dt in parsed["dev_types"].values():
        if (dt_dir / f"{dt.name}.yaml").exists():
            out.add(("dt", dt.name))
    for mt, entries in parsed["prompt_templates"].items():
        tdir = prompt_templates._dir(mt)
        for name in entries:
            if (tdir / f"{name}.yaml").exists():
                out.add(("tpl", mt, name))
    for dev, entries in parsed["devtype_prompts"].items():
        ddir = prompt_templates._dev_dir(dev)
        for name in entries:
            if (ddir / f"{name}.yaml").exists():
                out.add(("dtp", dev, name))
    return out


def _apply_config_files(parsed: dict, pre_existing: set, *,
                        existing_only: bool) -> None:
    """Write config files through the existing per-store choke points, keyed
    on the pre-apply on-disk snapshot ``pre_existing`` (never re-``exists()``,
    so a file the ADD pass just created is not re-written by the MUT pass).

    ``existing_only=False`` (ADD): names not on disk before the apply — safe
    before the config.yaml commit (inert extras if we crash).
    ``existing_only=True`` (MUT): overwrite files the old world already
    stored — only after the commit, so a SIGKILL cannot mix old
    config.yaml with new Dev Type / template bytes.
    """
    for dt in parsed["dev_types"].values():
        if (("dt", dt.name) in pre_existing) != existing_only:
            continue
        save_dev_type(dt)
    for mt, entries in parsed["prompt_templates"].items():
        for name, text in entries.items():
            if (("tpl", mt, name) in pre_existing) != existing_only:
                continue
            prompt_templates.save_template(mt, name, text)
    for dev, entries in parsed["devtype_prompts"].items():
        for name, text in entries.items():
            if (("dtp", dev, name) in pre_existing) != existing_only:
                continue
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


@dataclass(frozen=True)
class _SecretOps:
    """Pure plan of secret-store mutations, partitioned by crash class
    (2026-08-12 audit SEC-1): ADDS create keys the old world never stored —
    safe before the commit point; overwrites and deletions are DESTRUCTIVE
    and run only after it."""
    warnings: tuple[str, ...]
    conn_adds: tuple[tuple[str, str, str, str], ...]       # scope, inst, field, value
    conn_overwrites: tuple[tuple[str, str, str, str], ...]
    conn_field_deletes: tuple[tuple[str, str, str], ...]
    conn_instance_deletes: tuple[tuple[str, str], ...]
    harness_adds: tuple[tuple[str, str], ...]
    harness_overwrites: tuple[tuple[str, str], ...]
    harness_deletes: tuple[str, ...]
    # credential files (SEC-2): section-presence-keyed replace-the-world —
    # absent key = untouched (every pre-existing profile keeps its meaning),
    # present key = listed files written, unlisted host files deleted (the
    # same replace-the-world rule as connections/harness; one rule, not three)
    cred_section_present: bool
    cred_adds: tuple[tuple[str, str, str], ...]            # dev, filename, content
    cred_overwrites: tuple[tuple[str, str, str], ...]
    cred_deletes: tuple[tuple[str, str], ...]


def _plan_secret_ops(sec: dict, target_cfg: AppConfig, *,
                     target_dev_types: set[str],
                     restore_exact: bool = False) -> _SecretOps:
    """Partition the bundle's secret section against the live store. Pure
    read (store listings only). Unknown-instance entries are SKIPPED with a
    warning — never an orphan secret file on disk (ADR-0013 hardening);
    restore_exact (rollback only) disables the skip: a live world may
    legitimately hold a secret whose instance card isn't saved yet, and a
    rollback must restore byte-exact, not editorialize."""
    warnings: list[str] = []
    known = secrets_store.connection_instance_keys(target_cfg)
    wanted: dict[str, dict[str, str]] = {}
    for key, fields in sec["connections"].items():
        if key not in known and not restore_exact:
            warnings.append(f"secrets.connections[{key}]: no such configured "
                            "instance — skipped")
            continue
        wanted[key] = fields
    current = secrets_store.list_connection_secrets()
    conn_adds: list[tuple[str, str, str, str]] = []
    conn_overwrites: list[tuple[str, str, str, str]] = []
    conn_field_deletes: list[tuple[str, str, str]] = []
    for key, fields in wanted.items():
        scope, _, instance = key.partition("-")
        have = current.get(key, {})
        for field, value in fields.items():
            row = (scope, instance, field, value)
            (conn_overwrites if field in have else conn_adds).append(row)
        for field in set(have) - set(fields):
            conn_field_deletes.append((scope, instance, field))
    conn_instance_deletes = []
    for key in sorted(set(current) - set(wanted)):
        scope, _, instance = key.partition("-")
        conn_instance_deletes.append((scope, instance))
    cur_h = secrets_store.list_harness_secrets()
    cred = sec.get("credential_files")
    cred_adds: list[tuple[str, str, str]] = []
    cred_overwrites: list[tuple[str, str, str]] = []
    cred_deletes: list[tuple[str, str]] = []
    if cred is not None:
        current_files = {(cf["dev_type"], cf["filename"])
                         for cf in secrets_store.inventory()["credential_files"]}
        wanted_files: dict[tuple[str, str], str] = {}
        for dev, files in cred.items():
            if dev not in target_dev_types and not restore_exact:
                warnings.append(f"secrets.credential_files[{dev}]: no such "
                                "Dev Type in the target world — skipped")
                continue
            for f in files:
                wanted_files[(dev, f["filename"])] = f["content"]
        for (dev, fname), content in sorted(wanted_files.items()):
            row = (dev, fname, content)
            (cred_overwrites if (dev, fname) in current_files
             else cred_adds).append(row)
        cred_deletes = sorted(current_files - set(wanted_files))
    return _SecretOps(
        warnings=tuple(warnings),
        conn_adds=tuple(conn_adds),
        conn_overwrites=tuple(conn_overwrites),
        conn_field_deletes=tuple(conn_field_deletes),
        conn_instance_deletes=tuple(conn_instance_deletes),
        harness_adds=tuple((v, val) for v, val in sec["harness"].items()
                           if v not in cur_h),
        harness_overwrites=tuple((v, val) for v, val in sec["harness"].items()
                                 if v in cur_h),
        harness_deletes=tuple(sorted(set(cur_h) - set(sec["harness"]))),
        cred_section_present=cred is not None,
        cred_adds=tuple(cred_adds),
        cred_overwrites=tuple(cred_overwrites),
        cred_deletes=tuple(cred_deletes),
    )


def _apply_secret_adds(ops: _SecretOps) -> None:
    """Phase ADD (pre-commit): only writes that create keys the old world
    never stored — through the store choke points, so 0600/atomicity/
    redaction registration come free. A crash after any prefix leaves the
    old world authoritative; nothing it stored was modified or removed."""
    for scope, instance, field, value in ops.conn_adds:
        secrets_store.write_connection_secret(scope, instance, field, value)
    for var, value in ops.harness_adds:
        secrets_store.write_harness_secret(var, value)
    for dev, fname, content in ops.cred_adds:
        # the ONE writer seam (same call the OAuth flow and the upload
        # endpoint use) — 0600, atomic write, size cap, redaction registration
        secrets_store.write_credential_file(dev, fname, content)


def _apply_secret_destructive(ops: _SecretOps) -> None:
    """Phase MUT (post-commit): overwrites first, then deletions — once the
    new world is authoritative, converging to it wins over preserving the
    old one."""
    for scope, instance, field, value in ops.conn_overwrites:
        secrets_store.write_connection_secret(scope, instance, field, value)
    for var, value in ops.harness_overwrites:
        secrets_store.write_harness_secret(var, value)
    for dev, fname, content in ops.cred_overwrites:
        secrets_store.write_credential_file(dev, fname, content)
    for scope, instance, field in ops.conn_field_deletes:
        secrets_store.delete_connection_field(scope, instance, field)
    for scope, instance in ops.conn_instance_deletes:
        secrets_store.delete_connection_instance(scope, instance)
    for var in ops.harness_deletes:
        secrets_store.delete_harness_secret(var)
    for dev, fname in ops.cred_deletes:
        secrets_store.delete_credential_file(dev, fname)
