"""``devcake setup`` — configure a reachable control plane (ADR-0038 Decision 1).

Does **not** bake, compose-up, wait healthy, or hello-smoke (Decision 8).
Slices: Dev Type first-setup, PMO/repo connections + secrets, settings-bundle
import→profile→apply (+ host ``.env`` for section C), doctor subset + receipt.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import doctor, envfile
from .paths import require_checkout_root

_ROLES: tuple[str, ...] = ("judge", "executor", "steward")
_DEFAULT_BASE_URL = "http://localhost:8000"

_SETUP_HELP = """\
usage: devcake setup [flags…] [--json]

Configure an already-reachable DevCake control plane (ADR-0038).
Does not start, stop, bake, or wait on the compose stack — run
`devcake up --bake` first. Clean-host chain:

  devcake up --bake && devcake setup … --json

Dev Type first-setup (create-once; HTTP 409 → exit 5):
  --role-harness <role>=<template>   repeatable; role ∈ judge,executor,steward
  --role-model <role>=<model>        repeatable; empty model = harness default
  --same-harness <template>          apply one harness to all three roles
  --same-model <model>               with --same-harness (optional)

PMO connection (upsert by name):
  --pmo-name <name>                  required when any --pmo-* is set
  --pmo-system <system>              default: linear
  --pmo-team-key <key>               team / board key (not a secret)
  --pmo-api-base <url>               optional API base override
  --pmo-api-key-env <VAR>            secret from env (never argv value)
  --pmo-api-key-file <path>          secret from file
  --pmo-api-key-stdin                secret from stdin

Repo connection (upsert by name):
  --repo-name <name>                 required when any --repo-* is set
  --repo-forge <forge>               default: github
  --repo-url <url>                   repository URL
  --repo-api-base <url>              optional API base override
  --repo-token-env <VAR>             write token from env
  --repo-token-file <path>           write token from file
  --repo-token-stdin                 write token from stdin

Settings-bundle import (ADR-0013 import→profile→apply):
  --import <bundle.yaml>             kind: devcake-settings-bundle
  --import-passphrase-env <VAR>      passphrase from env
  --import-passphrase-file <path>    passphrase from file
  --import-passphrase-stdin          passphrase from stdin
  --import-overwrite                 overwrite existing profile name
  --import-profile <name>            profile save-as (default: imported-<stem>)

Universal: --help, --json
Exit codes: 0 ok · 2 usage · 3 doctor hard-fail · 5 first-setup conflict · 1 other
"""


@dataclass
class SetupOptions:
    as_json: bool = False
    role_harness: dict[str, str] = field(default_factory=dict)
    role_model: dict[str, str] = field(default_factory=dict)
    same_harness: str | None = None
    same_model: str | None = None
    pmo_name: str | None = None
    pmo_system: str | None = None
    pmo_team_key: str | None = None
    pmo_api_base: str | None = None
    pmo_api_key_source: tuple[str, str] | None = None  # kind, ref
    repo_name: str | None = None
    repo_forge: str | None = None
    repo_url: str | None = None
    repo_api_base: str | None = None
    repo_token_source: tuple[str, str] | None = None
    import_path: Path | None = None
    import_passphrase_source: tuple[str, str] | None = None
    import_overwrite: bool = False
    import_profile: str | None = None
    base_url: str = _DEFAULT_BASE_URL


HttpFn = Callable[[str, str, dict | None, dict[str, str]], tuple[int, Any]]


class UsageError(Exception):
    """Bad flags / missing required input → exit 2."""


def parse_setup_flags(argv: Sequence[str]) -> SetupOptions | int:
    """Parse setup argv. Returns SetupOptions or exit code (0 help / 2 usage)."""
    opts = SetupOptions()
    tokens = list(argv)
    i = 0

    def need_value(flag: str) -> str:
        nonlocal i
        i += 1
        if i >= len(tokens):
            raise UsageError(f"{flag} requires a value")
        return tokens[i]

    def set_secret_source(
        current: tuple[str, str] | None,
        kind: str,
        ref: str,
        label: str,
    ) -> tuple[str, str]:
        if current is not None:
            raise UsageError(
                f"{label}: mutually exclusive env/file/stdin — pick one"
            )
        return (kind, ref)

    try:
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("-h", "--help"):
                sys.stdout.write(_SETUP_HELP)
                return 0
            if tok == "--role-harness":
                raw = need_value(tok)
                role, _, template = raw.partition("=")
                if not role or not template or role not in _ROLES:
                    raise UsageError(
                        f"--role-harness expects <role>=<template> "
                        f"with role ∈ {','.join(_ROLES)}"
                    )
                opts.role_harness[role] = template
            elif tok == "--role-model":
                raw = need_value(tok)
                role, _, model = raw.partition("=")
                if not role or "=" not in raw or role not in _ROLES:
                    raise UsageError(
                        f"--role-model expects <role>=<model> "
                        f"with role ∈ {','.join(_ROLES)}"
                    )
                opts.role_model[role] = model
            elif tok == "--same-harness":
                opts.same_harness = need_value(tok)
            elif tok == "--same-model":
                opts.same_model = need_value(tok)
            elif tok == "--pmo-name":
                opts.pmo_name = need_value(tok)
            elif tok == "--pmo-system":
                opts.pmo_system = need_value(tok)
            elif tok == "--pmo-team-key":
                opts.pmo_team_key = need_value(tok)
            elif tok == "--pmo-api-base":
                opts.pmo_api_base = need_value(tok)
            elif tok == "--pmo-api-key-env":
                opts.pmo_api_key_source = set_secret_source(
                    opts.pmo_api_key_source, "env", need_value(tok), "pmo-api-key"
                )
            elif tok == "--pmo-api-key-file":
                opts.pmo_api_key_source = set_secret_source(
                    opts.pmo_api_key_source, "file", need_value(tok), "pmo-api-key"
                )
            elif tok == "--pmo-api-key-stdin":
                opts.pmo_api_key_source = set_secret_source(
                    opts.pmo_api_key_source, "stdin", "", "pmo-api-key"
                )
            elif tok == "--repo-name":
                opts.repo_name = need_value(tok)
            elif tok == "--repo-forge":
                opts.repo_forge = need_value(tok)
            elif tok == "--repo-url":
                opts.repo_url = need_value(tok)
            elif tok == "--repo-api-base":
                opts.repo_api_base = need_value(tok)
            elif tok == "--repo-token-env":
                opts.repo_token_source = set_secret_source(
                    opts.repo_token_source, "env", need_value(tok), "repo-token"
                )
            elif tok == "--repo-token-file":
                opts.repo_token_source = set_secret_source(
                    opts.repo_token_source, "file", need_value(tok), "repo-token"
                )
            elif tok == "--repo-token-stdin":
                opts.repo_token_source = set_secret_source(
                    opts.repo_token_source, "stdin", "", "repo-token"
                )
            elif tok == "--import":
                opts.import_path = Path(need_value(tok))
            elif tok == "--import-passphrase-env":
                opts.import_passphrase_source = set_secret_source(
                    opts.import_passphrase_source,
                    "env",
                    need_value(tok),
                    "import-passphrase",
                )
            elif tok == "--import-passphrase-file":
                opts.import_passphrase_source = set_secret_source(
                    opts.import_passphrase_source,
                    "file",
                    need_value(tok),
                    "import-passphrase",
                )
            elif tok == "--import-passphrase-stdin":
                opts.import_passphrase_source = set_secret_source(
                    opts.import_passphrase_source,
                    "stdin",
                    "",
                    "import-passphrase",
                )
            elif tok == "--import-overwrite":
                opts.import_overwrite = True
            elif tok == "--import-profile":
                opts.import_profile = need_value(tok)
            elif tok.startswith("-"):
                raise UsageError(f"unknown option: {tok} (try --help)")
            else:
                raise UsageError(f"unexpected argument: {tok!r}")
            i += 1
    except UsageError as e:
        sys.stderr.write(f"devcake setup: {e}\n")
        return 2

    try:
        _validate_options(opts)
    except UsageError as e:
        sys.stderr.write(f"devcake setup: {e}\n")
        return 2
    return opts


def _validate_options(opts: SetupOptions) -> None:
    if opts.same_model is not None and not opts.same_harness:
        raise UsageError("--same-model requires --same-harness")

    # Expand same-harness / same-model into per-role maps; reject disagreements.
    if opts.same_harness:
        for role in _ROLES:
            existing = opts.role_harness.get(role)
            if existing is not None and existing != opts.same_harness:
                raise UsageError(
                    f"--same-harness {opts.same_harness!r} disagrees with "
                    f"--role-harness {role}={existing}"
                )
            opts.role_harness[role] = opts.same_harness
        if opts.same_model is not None:
            for role in _ROLES:
                existing = opts.role_model.get(role)
                if existing is not None and existing != opts.same_model:
                    raise UsageError(
                        f"--same-model {opts.same_model!r} disagrees with "
                        f"--role-model {role}={existing}"
                    )
                opts.role_model[role] = opts.same_model

    wants_roster = bool(opts.role_harness) or bool(opts.same_harness)
    if wants_roster:
        missing = [r for r in _ROLES if r not in opts.role_harness]
        if missing:
            raise UsageError(
                "first-setup requires harness for all roles "
                f"(missing: {', '.join(missing)}); use --same-harness "
                "or --role-harness for each"
            )

    pmo_set = any(
        v is not None
        for v in (
            opts.pmo_name,
            opts.pmo_system,
            opts.pmo_team_key,
            opts.pmo_api_base,
            opts.pmo_api_key_source,
        )
    )
    if pmo_set and not opts.pmo_name:
        raise UsageError("--pmo-name is required when configuring a PMO")

    repo_set = any(
        v is not None
        for v in (
            opts.repo_name,
            opts.repo_forge,
            opts.repo_url,
            opts.repo_api_base,
            opts.repo_token_source,
        )
    )
    if repo_set and not opts.repo_name:
        raise UsageError("--repo-name is required when configuring a repo")

    if opts.import_passphrase_source and not opts.import_path:
        raise UsageError("--import is required when a passphrase source is set")


def _read_secret(source: tuple[str, str], *, label: str) -> str:
    kind, ref = source
    if kind == "env":
        val = os.environ.get(ref)
        if val is None or val == "":
            raise UsageError(f"{label}: env var {ref!r} is unset or empty")
        return val
    if kind == "file":
        path = Path(ref)
        if not path.is_file():
            raise UsageError(f"{label}: file not found: {ref}")
        val = path.read_text(encoding="utf-8").rstrip("\n")
        if not val:
            raise UsageError(f"{label}: file {ref} is empty")
        return val
    if kind == "stdin":
        val = sys.stdin.read().rstrip("\n")
        if not val:
            raise UsageError(f"{label}: stdin is empty")
        return val
    raise UsageError(f"{label}: unknown secret source {kind!r}")


def _basic_auth_header(user: str, password: str) -> str:
    tok = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {tok}"


def _load_admin_auth(repo: Path) -> tuple[str, str]:
    data = envfile.parse_env_file(repo / ".env")
    user = (data.get("ADMIN_USER") or "").strip()
    password = (data.get("ADMIN_PASSWORD") or "").strip()
    if not user or not password:
        raise RuntimeError(
            "ADMIN_USER / ADMIN_PASSWORD missing from checkout .env — "
            "run `devcake up` first (auto-init) or set them manually"
        )
    return user, password


def default_http(
    method: str,
    url: str,
    body: dict | None,
    headers: dict[str, str],
) -> tuple[int, Any]:
    """stdlib HTTP JSON helper (no httpx dependency in the CLI package)."""
    data = None
    req_headers = dict(headers)
    if body is not None:
        data = json.dumps(body).encode()
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200) or 200
            if not raw:
                return status, {}
            try:
                return status, json.loads(raw.decode())
            except json.JSONDecodeError:
                return status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload: Any = json.loads(raw.decode()) if raw else {"detail": str(e)}
        except json.JSONDecodeError:
            payload = raw.decode(errors="replace") if raw else str(e)
        return e.code, payload
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach control plane at {url}: {e.reason}") from e


def _api(
    http: HttpFn,
    method: str,
    base: str,
    path: str,
    body: dict | None,
    auth_header: str,
) -> tuple[int, Any]:
    url = base.rstrip("/") + path
    # Control-plane auth requires X-DevCake-Request: 1 on every mutating
    # method (POST/PUT/PATCH/DELETE); without it the app returns 403
    # "missing request intent header". Send on all setup calls (SPA does too).
    return http(
        method,
        url,
        body,
        {
            "Authorization": auth_header,
            "X-DevCake-Request": "1",
        },
    )


def _build_roles_body(opts: SetupOptions) -> dict[str, dict[str, str]]:
    roles: dict[str, dict[str, str]] = {}
    for role in _ROLES:
        roles[role] = {
            "harness_template": opts.role_harness[role],
            "model": opts.role_model.get(role, ""),
        }
    return roles


def _wants_roster(opts: SetupOptions) -> bool:
    return bool(opts.role_harness)


def _wants_pmo(opts: SetupOptions) -> bool:
    return opts.pmo_name is not None


def _wants_repo(opts: SetupOptions) -> bool:
    return opts.repo_name is not None


def _run_first_setup(
    opts: SetupOptions,
    *,
    http: HttpFn,
    auth_header: str,
    receipt: dict[str, Any],
) -> int | None:
    """Returns an exit code on terminal failure, else None."""
    if not _wants_roster(opts):
        return None
    roles = _build_roles_body(opts)
    status, payload = _api(
        http,
        "POST",
        opts.base_url,
        "/api/v1/dev-types/first-setup",
        {"roles": roles},
        auth_header,
    )
    if status == 409:
        receipt["ok"] = False
        receipt["roles_created"] = []
        receipt["roles"] = {
            role: {
                "harness_template": roles[role]["harness_template"],
                "model": roles[role]["model"],
                "created": False,
            }
            for role in _ROLES
        }
        detail = _detail(payload)
        receipt["next_steps"].append(
            f"first-setup conflict (roster non-empty): {detail}"
        )
        sys.stderr.write(f"devcake setup: first-setup conflict: {detail}\n")
        return 5
    if status >= 400:
        receipt["ok"] = False
        detail = _detail(payload)
        sys.stderr.write(f"devcake setup: first-setup failed ({status}): {detail}\n")
        return 1
    created = list(payload.get("created") or _ROLES) if isinstance(payload, dict) else list(_ROLES)
    receipt["roles_created"] = created
    receipt["roles"] = {
        role: {
            "harness_template": roles[role]["harness_template"],
            "model": roles[role]["model"],
            "created": True,
        }
        for role in _ROLES
    }
    return None


def _detail(payload: Any) -> str:
    if isinstance(payload, dict):
        d = payload.get("detail", payload)
        return str(d)
    return str(payload)


def _upsert_pmo(
    opts: SetupOptions,
    *,
    http: HttpFn,
    auth_header: str,
    receipt: dict[str, Any],
) -> int | None:
    if not _wants_pmo(opts):
        return None
    status, cfg = _api(http, "GET", opts.base_url, "/api/v1/config", None, auth_header)
    if status >= 400 or not isinstance(cfg, dict):
        sys.stderr.write(f"devcake setup: GET /config failed ({status}): {_detail(cfg)}\n")
        receipt["ok"] = False
        return 1
    pmos = list(cfg.get("pmos") or [])
    name = opts.pmo_name or ""
    idx = next((i for i, p in enumerate(pmos) if p.get("name") == name), None)
    card: dict[str, Any] = dict(pmos[idx]) if idx is not None else {"name": name}
    if opts.pmo_system is not None:
        card["system"] = opts.pmo_system
    elif "system" not in card:
        card["system"] = "linear"
    if opts.pmo_team_key is not None:
        card["team_key"] = opts.pmo_team_key
    if opts.pmo_api_base is not None:
        card["api_base"] = opts.pmo_api_base
    if idx is None:
        pmos.append(card)
    else:
        pmos[idx] = card
    put_body = {**cfg, "pmos": pmos}
    status, put_out = _api(
        http, "PUT", opts.base_url, "/api/v1/config", put_body, auth_header
    )
    if status >= 400:
        sys.stderr.write(
            f"devcake setup: PUT /config (pmo) failed ({status}): {_detail(put_out)}\n"
        )
        receipt["ok"] = False
        return 1

    configured = True
    if opts.pmo_api_key_source:
        try:
            secret = _read_secret(opts.pmo_api_key_source, label="pmo-api-key")
        except UsageError as e:
            sys.stderr.write(f"devcake setup: {e}\n")
            receipt["ok"] = False
            return 2
        status, sec_out = _api(
            http,
            "PUT",
            opts.base_url,
            f"/api/v1/secrets/pmo/{name}/api_key",
            {"value": secret},
            auth_header,
        )
        if status >= 400:
            sys.stderr.write(
                f"devcake setup: put pmo secret failed ({status}): {_detail(sec_out)}\n"
            )
            receipt["ok"] = False
            return 1
        receipt.setdefault("secrets_received", {})["pmo_api_key"] = True
    else:
        receipt.setdefault("secrets_received", {}).setdefault("pmo_api_key", False)

    receipt.setdefault("connections", {}).setdefault("pmo", []).append(
        {"name": name, "configured": configured, "tested": False}
    )
    return None


def _upsert_repo(
    opts: SetupOptions,
    *,
    http: HttpFn,
    auth_header: str,
    receipt: dict[str, Any],
) -> int | None:
    if not _wants_repo(opts):
        return None
    status, cfg = _api(http, "GET", opts.base_url, "/api/v1/config", None, auth_header)
    if status >= 400 or not isinstance(cfg, dict):
        sys.stderr.write(f"devcake setup: GET /config failed ({status}): {_detail(cfg)}\n")
        receipt["ok"] = False
        return 1
    repos = list(cfg.get("repos") or [])
    name = opts.repo_name or ""
    idx = next((i for i, r in enumerate(repos) if r.get("name") == name), None)
    card: dict[str, Any] = dict(repos[idx]) if idx is not None else {"name": name}
    if opts.repo_forge is not None:
        card["forge"] = opts.repo_forge
    elif "forge" not in card:
        card["forge"] = "github"
    if opts.repo_url is not None:
        card["url"] = opts.repo_url
    if opts.repo_api_base is not None:
        card["api_base"] = opts.repo_api_base
    if idx is None:
        repos.append(card)
    else:
        repos[idx] = card
    put_body = {**cfg, "repos": repos}
    status, put_out = _api(
        http, "PUT", opts.base_url, "/api/v1/config", put_body, auth_header
    )
    if status >= 400:
        sys.stderr.write(
            f"devcake setup: PUT /config (repo) failed ({status}): {_detail(put_out)}\n"
        )
        receipt["ok"] = False
        return 1

    if opts.repo_token_source:
        try:
            secret = _read_secret(opts.repo_token_source, label="repo-token")
        except UsageError as e:
            sys.stderr.write(f"devcake setup: {e}\n")
            receipt["ok"] = False
            return 2
        status, sec_out = _api(
            http,
            "PUT",
            opts.base_url,
            f"/api/v1/secrets/repo/{name}/token",
            {"value": secret},
            auth_header,
        )
        if status >= 400:
            sys.stderr.write(
                f"devcake setup: put repo secret failed ({status}): {_detail(sec_out)}\n"
            )
            receipt["ok"] = False
            return 1
        sr = receipt.setdefault("secrets_received", {})
        sr["repo_token_count"] = int(sr.get("repo_token_count") or 0) + 1
    else:
        receipt.setdefault("secrets_received", {}).setdefault("repo_token_count", 0)

    receipt.setdefault("connections", {}).setdefault("repos", []).append(
        {"name": name, "configured": True, "tested": False}
    )
    return None


def _apply_setup_env_to_host(repo: Path, values: dict[str, str]) -> list[str]:
    """Write section-C key names into checkout .env (mode 600). Never log values."""
    env_path = repo / ".env"
    written: list[str] = []
    for key, value in values.items():
        if not isinstance(value, str):
            continue
        envfile.upsert_env_var(key, value, env_path)
        written.append(key)
    if written:
        envfile.ensure_permission_floor(env_path)
    return written


def _run_bundle_import(
    opts: SetupOptions,
    *,
    http: HttpFn,
    auth_header: str,
    repo: Path,
    receipt: dict[str, Any],
) -> int | None:
    if opts.import_path is None:
        return None
    path = opts.import_path
    if not path.is_file():
        sys.stderr.write(f"devcake setup: --import file not found: {path}\n")
        receipt["ok"] = False
        return 2
    raw = path.read_bytes()
    content_b64 = base64.b64encode(raw).decode()
    body: dict[str, Any] = {
        "content_b64": content_b64,
        "save_as": opts.import_profile
        or f"imported-{path.stem}".replace(" ", "-")[:64],
        "overwrite": opts.import_overwrite,
    }
    if opts.import_passphrase_source:
        try:
            body["passphrase"] = _read_secret(
                opts.import_passphrase_source, label="import-passphrase"
            )
        except UsageError as e:
            sys.stderr.write(f"devcake setup: {e}\n")
            receipt["ok"] = False
            return 2

    status, imp = _api(
        http, "POST", opts.base_url, "/api/v1/settings/import", body, auth_header
    )
    if status >= 400:
        sys.stderr.write(
            f"devcake setup: settings import failed ({status}): {_detail(imp)}\n"
        )
        receipt["ok"] = False
        return 1
    if not isinstance(imp, dict):
        receipt["ok"] = False
        return 1
    profile = str(imp.get("saved_as") or body["save_as"])
    sections = list(imp.get("sections") or [])

    # Apply world-swap.
    status, apply_out = _api(
        http,
        "POST",
        opts.base_url,
        f"/api/v1/profiles/{profile}/apply",
        None,
        auth_header,
    )
    if status >= 400:
        sys.stderr.write(
            f"devcake setup: profile apply failed ({status}): {_detail(apply_out)}\n"
        )
        receipt["ok"] = False
        receipt["bundle_import"] = {
            "applied": False,
            "path": str(path),
            "sections": sections,
            "profile": profile,
            "setup_env_keys": [],
            "secret_key_counts": {},
        }
        return 1

    setup_env_keys: list[str] = []
    secret_key_counts: dict[str, int] = {}
    # Host-side section C via /settings/import/env when present.
    if imp.get("has_setup_env"):
        status, env_body = _api(
            http,
            "POST",
            opts.base_url,
            "/api/v1/settings/import/env",
            {
                "content_b64": content_b64,
                **(
                    {"passphrase": body["passphrase"]}
                    if "passphrase" in body
                    else {}
                ),
            },
            auth_header,
        )
        # import/env returns plaintext .env — parse KEY=VALUE and upsert.
        if status < 400 and isinstance(env_body, str):
            values: dict[str, str] = {}
            for line in env_body.splitlines():
                raw_line = line.strip()
                if not raw_line or raw_line.startswith("#"):
                    continue
                if "=" not in raw_line:
                    continue
                k, _, v = raw_line.partition("=")
                if k:
                    values[k] = v
            setup_env_keys = _apply_setup_env_to_host(repo, values)
            if setup_env_keys:
                sections = list(dict.fromkeys([*sections, "setup_env"]))
                receipt["next_steps"].append(
                    "devcake up   # bundle setup_env changed host .env — compose must reload"
                )
        elif status >= 400:
            sys.stderr.write(
                f"devcake setup: warning: setup_env download failed "
                f"({status}): {_detail(env_body)}\n"
            )

    # Secret counts from import response / inventory — names only.
    # inventory() returns {harness: […], connections: […], …} (lists).
    status_inv, inv = _api(
        http, "GET", opts.base_url, "/api/v1/secrets/inventory", None, auth_header
    )
    if status_inv < 400 and isinstance(inv, dict):
        conn = inv.get("connections") or []
        harness = inv.get("harness") or []
        if isinstance(conn, (list, dict)):
            secret_key_counts["connections"] = len(conn)
        if isinstance(harness, (list, dict)):
            secret_key_counts["harness"] = len(harness)

    receipt["bundle_import"] = {
        "applied": True,
        "path": str(path),
        "sections": sections,
        "profile": profile,
        "setup_env_keys": setup_env_keys,
        "secret_key_counts": secret_key_counts,
    }
    return None


def _doctor_into_receipt(
    receipt: dict[str, Any],
    *,
    repo: Path | None,
) -> bool:
    """Run doctor catalog into receipt. Returns True when hard-ok."""
    checks = doctor.run_checks(repo_root=repo)
    hard_ok = not any((not c.ok) and c.hard for c in checks)
    receipt["doctor"] = {
        "ok": hard_ok,
        "checks": [{"id": c.id, "ok": c.ok, "detail": c.detail} for c in checks],
    }
    for c in checks:
        if not c.ok:
            # Remedies live in detail; never run them.
            receipt["next_steps"].append(c.detail)
    if not hard_ok:
        receipt["ok"] = False
    return hard_ok


def _empty_receipt() -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": 1,
        "roles_created": [],
        "roles": {},
        "connections": {"pmo": [], "repos": []},
        "secrets_received": {
            "pmo_api_key": False,
            "repo_token_count": 0,
            "harness_key_count": 0,
        },
        "bundle_import": {},
        "doctor": {},
        "next_steps": [],
    }


def _needs_api(opts: SetupOptions) -> bool:
    return (
        _wants_roster(opts)
        or _wants_pmo(opts)
        or _wants_repo(opts)
        or opts.import_path is not None
    )


def run_setup(
    opts: SetupOptions,
    *,
    http: HttpFn | None = None,
    repo_root: Path | None = None,
) -> int:
    """Execute setup slices. Returns ADR-0038 exit code."""
    http_fn = http or default_http
    try:
        repo = repo_root or require_checkout_root()
    except FileNotFoundError as e:
        sys.stderr.write(f"devcake setup: {e}\n")
        return 2

    receipt = _empty_receipt()
    exit_code = 0
    auth = ""

    if _needs_api(opts):
        try:
            user, password = _load_admin_auth(repo)
        except RuntimeError as e:
            sys.stderr.write(f"devcake setup: {e}\n")
            receipt["ok"] = False
            _emit(receipt, opts.as_json)
            return 1
        auth = _basic_auth_header(user, password)

        # Slice order: roster → connections → import → doctor.
        # First-setup 409 locks exit 5 but other upsert/import slices still run.
        for step in (
            lambda: _run_first_setup(
                opts, http=http_fn, auth_header=auth, receipt=receipt
            ),
            lambda: _upsert_pmo(
                opts, http=http_fn, auth_header=auth, receipt=receipt
            ),
            lambda: _upsert_repo(
                opts, http=http_fn, auth_header=auth, receipt=receipt
            ),
            lambda: _run_bundle_import(
                opts, http=http_fn, auth_header=auth, repo=repo, receipt=receipt
            ),
        ):
            try:
                rc = step()
            except RuntimeError as e:
                sys.stderr.write(f"devcake setup: {e}\n")
                receipt["ok"] = False
                _emit(receipt, opts.as_json)
                return 1
            if rc is not None and exit_code == 0:
                exit_code = rc

    hard_ok = _doctor_into_receipt(receipt, repo=repo)
    if exit_code == 0 and not hard_ok:
        exit_code = 3
    if exit_code != 0:
        receipt["ok"] = False

    if not receipt.get("bundle_import"):
        receipt.pop("bundle_import", None)

    _emit(receipt, opts.as_json)
    return exit_code


def _emit(receipt: dict[str, Any], as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(receipt, indent=2) + "\n")
        return
    # Human summary — never secret values.
    lines = ["devcake setup"]
    if receipt.get("roles_created"):
        lines.append(f"  roles_created: {', '.join(receipt['roles_created'])}")
    elif receipt.get("roles"):
        lines.append("  roles: (not created — see conflict / next_steps)")
    conns = receipt.get("connections") or {}
    for p in conns.get("pmo") or []:
        lines.append(f"  pmo: {p.get('name')} configured={p.get('configured')}")
    for r in conns.get("repos") or []:
        lines.append(f"  repo: {r.get('name')} configured={r.get('configured')}")
    bi = receipt.get("bundle_import") or {}
    if bi:
        lines.append(
            f"  bundle_import: applied={bi.get('applied')} profile={bi.get('profile')}"
        )
    doc = receipt.get("doctor") or {}
    if doc:
        lines.append(f"  doctor: ok={doc.get('ok')}")
    for step in receipt.get("next_steps") or []:
        lines.append(f"  next: {step}")
    lines.append("  ok" if receipt.get("ok") else "  FAILED")
    sys.stdout.write("\n".join(lines) + "\n")
