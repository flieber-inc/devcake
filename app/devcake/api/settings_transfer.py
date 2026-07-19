"""Settings export/import application service (ADR-0013 part 2, ADR-0015
Decision 3). The ONE sanctioned secret-value egress: encrypted by default,
plaintext behind an explicit acknowledgment, every export audited. Import
LANDS AS A PROFILE — applying stays the profiles endpoint's job, so there is
exactly one world-swap path (and one runs-active guard) in the product."""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone

import yaml
from fastapi import HTTPException
from fastapi.responses import PlainTextResponse

from .. import profiles as profiles_store
from .. import secrets as secrets_store
from ..domain.skills import SkillStoreError
from ..settings_bundle import (BundleError, MAX_BUNDLE_BYTES, SETUP_ENV_VARS,
                               audit_event, diff_bundle, generate_env_file,
                               protect_bundle, serialize_current,
                               unprotect_bundle, validate_bundle)
from ..settings_crypto import MIN_PASSPHRASE_LEN, DecryptError


class _PassphraseRequired(Exception):
    pass


def open_uploaded_bundle(body: dict) -> dict:
    """content_b64 → dict, hardened: base64 + size cap + yaml.safe_load only,
    protected-envelope unwrap when a passphrase rides along."""
    try:
        raw = base64.b64decode(str(body.get("content_b64") or ""), validate=True)
    except Exception:  # noqa: BLE001 — upload-parsing contract: any decode failure is the client's 422, never a 500
        raise HTTPException(422, "content_b64 must be valid base64")
    if len(raw) > MAX_BUNDLE_BYTES:
        raise HTTPException(422, f"bundle exceeds "
                                 f"{MAX_BUNDLE_BYTES // (1024 * 1024)} MB")
    try:
        doc = yaml.safe_load(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 — upload-parsing contract: any decode/parse failure is the client's 422, never a 500
        raise HTTPException(422, "not a readable YAML bundle")
    if not isinstance(doc, dict):
        raise HTTPException(422, "not a settings bundle")
    if "protected" in doc:
        passphrase = body.get("passphrase")
        if not passphrase:
            raise _PassphraseRequired()
        try:
            doc = unprotect_bundle(doc, str(passphrase))
        except DecryptError as e:
            raise HTTPException(422, str(e))
        except BundleError as e:
            raise HTTPException(e.status, str(e))
    return doc


def bundle_filename() -> str:
    return datetime.now(timezone.utc).strftime("devcake-settings-%Y%m%d-%H%M.yaml")


async def export_settings_body(body: dict, *, config, dev_types,
                               skill_service) -> PlainTextResponse:
    sections = body.get("sections") or {}
    inc_cfg = bool(sections.get("config"))
    inc_sec = bool(sections.get("secrets"))
    inc_env = bool(sections.get("setup_env"))
    if not (inc_cfg or inc_sec or inc_env):
        raise HTTPException(422, "select at least one section to export")
    enc = body.get("encryption") or {}
    passphrase = None
    if inc_sec or inc_env:
        mode = enc.get("mode")
        if mode == "passphrase":
            passphrase = str(enc.get("passphrase") or "")
            if len(passphrase) < MIN_PASSPHRASE_LEN:
                raise HTTPException(422, f"passphrase must be at least "
                                         f"{MIN_PASSPHRASE_LEN} characters")
        elif mode == "plaintext":
            if not enc.get("acknowledge_plaintext"):
                raise HTTPException(422, "plaintext secret export must be "
                                         "explicitly acknowledged")
        else:
            raise HTTPException(422, "exports containing secrets or setup "
                                     "values require an encryption choice")
    source = body.get("source") or "current"
    if source == "current":
        skill_payloads = None
        if inc_cfg and body.get("include_skills"):
            names = [s.name for s in (await skill_service.list_skills())[0]]
            skill_payloads, _warns = await skill_service.payload_for(names)
        bundle = serialize_current(
            config, dev_types,
            include_config=inc_cfg, include_secrets=inc_sec,
            include_credential_files=inc_sec and bool(body.get("include_credential_files")),
            include_setup_env=inc_env, skill_payloads=skill_payloads)
    else:
        name = str((source or {}).get("profile") or "")
        if inc_env:
            raise HTTPException(422, "profiles never hold setup values — "
                                     "export setup_env from current settings")
        try:
            stored = profiles_store.read_profile(name)
        except BundleError as e:
            raise HTTPException(e.status, str(e))
        for want, key in ((inc_cfg, "config"), (inc_sec, "secrets")):
            if want and key not in stored:
                raise HTTPException(422, f"profile {name!r} has no {key} section")
        bundle = {k: v for k, v in stored.items() if k != "name"}
        bundle["sections"] = [s for s in ("config", "secrets")
                              if (s == "config" and inc_cfg) or (s == "secrets" and inc_sec)]
        if not inc_cfg:
            bundle.pop("config", None)
        if not inc_sec:
            bundle.pop("secrets", None)
    if (inc_sec or inc_env) and passphrase is None:
        bundle["plaintext_secrets"] = True
    if passphrase is not None:
        bundle = protect_bundle(bundle, passphrase)
    audit_event("settings_exported",
                f"source={'current' if source == 'current' else source.get('profile')} "
                f"sections={'+'.join(bundle.get('sections') or [])} "
                f"encrypted={passphrase is not None}")
    return PlainTextResponse(
        yaml.safe_dump(bundle, sort_keys=False),
        media_type="application/yaml",
        headers={"Content-Disposition":
                 f'attachment; filename="{bundle_filename()}"'})


async def export_summary_body(*, skill_service) -> dict:
    """Counts for the export dialog — never values, never names of values'
    contents."""
    conns = secrets_store.list_connection_secrets()
    by_scope: dict[str, int] = {"pmo": 0, "repo": 0}
    for key, fields in conns.items():
        scope = key.partition("-")[0]
        by_scope[scope] = by_scope.get(scope, 0) + len(fields)
    harness_n = len(secrets_store.list_harness_secrets())
    try:
        skills_n = len((await skill_service.list_skills())[0])
    except Exception:  # noqa: BLE001 — advisory count: a skill-store outage shows 0, the export dialog must still render
        skills_n = 0
    return {"secrets": {"total": sum(by_scope.values()) + harness_n,
                        "by_scope": by_scope, "harness": harness_n,
                        "connections": len(conns)},
            "env_keys": [n for n, _ in SETUP_ENV_VARS
                         if os.environ.get(n) is not None],
            "skills": skills_n}


def import_preview_body(body: dict, *, config, dev_types) -> dict:
    """Stateless preview: parse, decrypt if needed, diff vs current. No
    values in the response — presence, names, counts, warnings only."""
    try:
        bundle = open_uploaded_bundle(body)
    except _PassphraseRequired:
        return {"needs_passphrase": True}
    try:
        diff = diff_bundle(bundle, config, dev_types)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    return {"sections_present": bundle.get("sections")
            or [k for k in ("config", "secrets", "setup_env") if k in bundle],
            "created_at": bundle.get("created_at"),
            "devcake_tag": bundle.get("devcake_tag", ""),
            "plaintext_secrets": bool(bundle.get("plaintext_secrets")),
            "has_skills": bool((bundle.get("skills") or {}).get("embedded")),
            "summary": diff, "warnings": diff["warnings"]}


async def import_settings_body(body: dict, *, skill_service) -> dict:
    """Import LANDS AS A PROFILE (never applies): the risky transfer is
    decoupled from the risky world-swap. No runs guard here — nothing about
    the live world changes except (opt-in) additive skill-store writes."""
    try:
        bundle = open_uploaded_bundle(body)
    except _PassphraseRequired:
        raise HTTPException(422, "bundle is encrypted — supply the passphrase")
    name = str(body.get("save_as") or "")
    try:
        parsed = validate_bundle(bundle)
        stored = {k: v for k, v in bundle.items()
                  if k not in ("setup_env", "skills", "plaintext_secrets")}
        stored["sections"] = [s for s in ("config", "secrets") if s in stored]
        if not stored["sections"]:
            raise BundleError(422, "bundle carries no config or secrets "
                                   "section to save as a profile")
        profiles_store.save_profile(name, stored,
                                    overwrite=bool(body.get("overwrite")))
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    warnings = list(parsed["warnings"])
    skills_imported: list[str] = []
    if body.get("import_skills"):
        for skill in (bundle.get("skills") or {}).get("embedded") or []:
            try:
                skill_name = skill_service.validate_import(skill.get("files") or [])
                await skill_service.save_skill(skill_name,
                                               skill.get("files") or [],
                                               overwrite=True)
                skills_imported.append(skill_name)
            except SkillStoreError as e:
                warnings.append(f"skill {skill.get('name')!r}: {e}")
    audit_event("settings_imported",
                f"save_as={name} sections={'+'.join(stored['sections'])} "
                f"skills={len(skills_imported)}")
    return {"saved_as": name, "sections": stored["sections"],
            "has_setup_env": "setup_env" in bundle,
            "skills_imported": skills_imported, "warnings": warnings}


def import_env_body(body: dict) -> PlainTextResponse:
    """Section C → a generated .env download. No server state changes and no
    runs guard — the app cannot write the host's .env; the operator places
    the file and restarts the stack."""
    try:
        bundle = open_uploaded_bundle(body)
    except _PassphraseRequired:
        raise HTTPException(422, "bundle is encrypted — supply the passphrase")
    try:
        parsed = validate_bundle(bundle)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    if parsed["setup_env"] is None:
        raise HTTPException(422, "bundle has no setup_env section")
    return PlainTextResponse(
        generate_env_file(parsed["setup_env"]),
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="devcake.env"'})
