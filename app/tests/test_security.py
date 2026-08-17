"""docs/14 §7 — transcript redaction (hygiene) and §8 security_warnings copy."""
import os

from devcake.security import MASK, redact, redact_value


def test_env_value_redacted(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 36)
    out = redact(f"leaked: {os.environ['GITHUB_TOKEN']} end")
    assert "ghp_" not in out and MASK in out


def test_patterns_redacted():
    for tok in ("ghp_" + "x" * 30, "glpat-" + "y" * 20, "sk-" + "z" * 30,
                "xai-" + "w" * 24, "lin_api_" + "k" * 24):
        assert tok not in redact(f"body {tok} body")


def test_extra_values_and_short_safety():
    assert "runpw" in redact("runpw", ["runpw"])           # <8 chars: never masked
    secret = "per-run-redis-password-123"
    assert secret not in redact(f"x {secret} y", [secret])


def test_plain_text_untouched():
    text = "нормальный transcript with code `git push` and no secrets"
    assert redact(text) == text


def test_structured_values_are_redacted_before_persistence(monkeypatch):
    token = "ghp_" + "q" * 36
    monkeypatch.setenv("GITHUB_TOKEN", token)
    value = {"summary": f"leaked {token}", "nested": [token, 7, None]}
    scrubbed = redact_value(value)
    assert token not in str(scrubbed)
    assert scrubbed["nested"][1:] == [7, None]


# ── registry-driven redaction: the superset tripwire (docs/14 §7) ────────────
# The v0 lists frozen as literals: the generated SECRET_ENV_VARS/TOKEN_PATTERNS
# must always contain AT LEAST these. A dropped entry means a secret could hit
# the PMO unredacted — unrecoverable — so this test must exist BEFORE and pass
# AFTER any registry rework.

V0_SECRET_ENV_VARS = [
    "LINEAR_API_KEY", "GITHUB_TOKEN", "GITHUB_REVIEWER_TOKEN", "GITLAB_TOKEN",
    "GITLAB_REVIEWER_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "XAI_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "REDIS_PASSWORD",
    "DAGU_PASSWORD", "ADMIN_PASSWORD", "OO_ROOT_PASSWORD",
    # added with the ISSUES #13/#15 opt-in credentials — the mitigations must
    # not themselves open redaction gaps
    "OO_INGEST_PASSWORD", "GITHUB_TOKEN_RO", "GITLAB_TOKEN_RO",
]
V0_TOKEN_PATTERNS = [
    r"\bghp_[A-Za-z0-9]{20,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    r"\bglpat-[A-Za-z0-9_-]{15,}\b",
    r"\bsk-[A-Za-z0-9_-]{20,}\b",
    r"\bxai-[A-Za-z0-9_-]{20,}\b",
    r"\blin_api_[A-Za-z0-9]{20,}\b",
    r"\blin_oauth_[A-Za-z0-9]{20,}\b",
]


def test_redaction_lists_are_superset_of_v0():
    import devcake.security as sec
    assert set(V0_SECRET_ENV_VARS) <= set(sec.secret_env_vars())
    assert set(V0_TOKEN_PATTERNS) <= {p.pattern for p in sec.token_patterns()}


def test_all_registered_adapters_contribute_even_unconfigured():
    """Switching forge must never open a redaction gap: EVERY registered
    adapter's shapes are scrubbed, not just the configured one's."""
    import devcake.security as sec
    from devcake.adapters.registry import PMO_SYSTEMS, forges
    envs, pats = set(sec.secret_env_vars()), {p.pattern for p in sec.token_patterns()}
    for s in PMO_SYSTEMS.values():
        assert set(s.secret_env_vars) <= envs
        assert set(s.token_patterns) <= pats
    for d in forges().values():
        assert set(d.secret_env_vars) <= envs
        assert set(d.token_patterns) <= pats


def test_redact_still_scrubs_all_v0_shapes():
    from devcake.security import MASK, redact
    samples = ["ghp_" + "a" * 30, "github_pat_" + "b" * 30, "glpat-" + "c" * 20,
               "sk-" + "d" * 25, "xai-" + "e" * 25, "lin_api_" + "f" * 25,
               "lin_oauth_" + "g" * 25]
    out = redact("tokens: " + " ".join(samples))
    for s in samples:
        assert s not in out
    assert MASK in out


def test_runtime_secret_registry():
    """Ephemeral per-run credentials (the Redis relay password) are registered
    at ACL creation and masked until teardown; empty values are ignored."""
    from devcake.security import register_runtime_secret, unregister_runtime_secret

    secret = "relay-password-0123456789abcdef"
    register_runtime_secret("run-a", secret)
    try:
        assert secret not in redact(f"leak {secret} end")
        assert MASK in redact(f"leak {secret} end")
    finally:
        unregister_runtime_secret("run-a")
    assert secret in redact(f"leak {secret} end")

    register_runtime_secret("run-b", "")          # ignored, not registered
    assert redact("nothing to mask") == "nothing to mask"


def test_runtime_secret_overwrite_keeps_prior_redacted():
    """Credential rotation must not unmask the previous value until restart."""
    from devcake import security as sec
    from devcake.security import register_runtime_secret, unregister_runtime_secret

    old = "rotated-away-secret-value-aaaa"
    new = "rotated-in-secret-value-bbbb"
    register_runtime_secret("cred:coder:auth.txt", old)
    try:
        register_runtime_secret("cred:coder:auth.txt", new)
        assert old not in redact(f"leak {old} end")
        assert new not in redact(f"leak {new} end")
        assert MASK in redact(f"leak {old} and {new}")
    finally:
        unregister_runtime_secret("cred:coder:auth.txt")
        # priors are process-local until restart; drop test values if present
        if old in sec._runtime_secret_priors:
            sec._runtime_secret_priors.remove(old)
        if new in sec._runtime_secret_priors:
            sec._runtime_secret_priors.remove(new)


def test_raw_credential_file_does_not_alarm_redaction_gap(
        tmp_path, monkeypatch, caplog):
    """Non-JSON Dev-type credential files are not a redaction_gap (runtime
    register_all owns them); corrupt connection JSON still alarms."""
    import logging

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    # Point security scan at the same tree
    import devcake.security as sec
    monkeypatch.setattr(sec, "_SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(sec, "_reported_unreadable", set())
    monkeypatch.setattr(sec, "_scan_cache", {})

    raw_dir = tmp_path / "secrets" / "coder"
    raw_dir.mkdir(parents=True)
    (raw_dir / "auth.txt").write_text("not-json-raw-oauth-blob!!")

    with caplog.at_level(logging.ERROR, logger="devcake.security"):
        # force disk scan
        sec.redact("hello")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "unreadable secrets file" not in joined
    assert "auth.txt" not in joined or "redaction" not in joined.lower()


def test_oo_ingest_and_ro_forge_tokens_redacted(monkeypatch):
    """ISSUES #13/#15 follow-up: the opt-in credentials (ingest OO user, RO
    forge PATs) must be masked like every other platform secret — they are
    arbitrary strings with no token-shape pattern to fall back on."""
    monkeypatch.setenv("OO_INGEST_PASSWORD", "ingest-pw-0123456789abcdef")
    monkeypatch.setenv("GITHUB_TOKEN_RO", "read-only-pat-0123456789abcdef")
    for leak in ("ingest-pw-0123456789abcdef", "read-only-pat-0123456789abcdef"):
        out = redact(f"env dump: {leak} end")
        assert leak not in out and MASK in out


def test_stored_forge_token_registered_at_construction(tmp_path, monkeypatch):
    """Forge token VALUES are GUI-stored (schema v4); make_forge must register
    the resolved values for redaction (an unusual token shape has no pattern
    fallback — e.g. a GitHub App ghs_ token)."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.adapters.registry import make_forge
    from devcake.config import RepoInstance
    from devcake import secrets as secrets_store
    from devcake.security import unregister_runtime_secret

    secrets_store.write_connection_secret("repo", "main", "token",
                                          "ghs_apptoken0123456789abcdef")
    secrets_store.write_connection_secret("repo", "main", "token_ro",
                                          "custom-ro-value-0123456789abcdef")
    inst = RepoInstance(name="main", url="https://github.com/o/r", forge="github")
    try:
        make_forge(inst)
        out = redact("leak ghs_apptoken0123456789abcdef and "
                     "custom-ro-value-0123456789abcdef end")
        assert "ghs_apptoken" not in out and "custom-ro-value" not in out
        assert MASK in out
    finally:
        for key in ("forge_token:main", "forge_token_ro:main",
                    "forge_reviewer:main"):
            unregister_runtime_secret(key)


def test_dev_type_secret_env_values_redacted(tmp_path, monkeypatch):
    """DevType.secret_env values (e.g. a Datadog API key) are ordinary
    harness-namespace secrets — registered on write and re-registered at
    boot (register_all) — so one leaking into PMO-bound text must mask like
    any platform credential. Tripwire for the secret-env delivery path."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.security import unregister_runtime_secret

    key = "dd-app-key-0123456789abcdef0123456789"
    secrets_store.write_harness_secret("DD_APP_KEY", key)
    try:
        out = redact(f"transcript: DD_APP_KEY={key} end")
        assert key not in out and MASK in out
    finally:
        unregister_runtime_secret("harness:DD_APP_KEY")


def test_known_values_cache_invalidates_on_change(tmp_path, monkeypatch):
    """Audit A28 + 2026-08 F17: the per-file mtime/size cache still sees
    changed, added, and removed files — but the RESULT cache in front of it
    (the glob+stat per redact() call is gone) rescans only on
    invalidate_secret_scan(), which the store's atomic writer calls. Direct
    file writes (this test) must invalidate explicitly — outside the app's
    single-writer contract, that is the documented deal."""
    import json as _json
    from devcake import security
    monkeypatch.setattr(security, "_SECRETS_DIR", tmp_path / "secrets")
    d = tmp_path / "secrets" / "connections"
    d.mkdir(parents=True)
    f = d / "repo-main.json"
    f.write_text(_json.dumps({"token": "first-secret-value-123"}))
    security.invalidate_secret_scan()
    assert "first-secret-value-123" not in security.redact("x first-secret-value-123")
    f.write_text(_json.dumps({"token": "second-secret-value-98765"}))
    security.invalidate_secret_scan()
    assert "second-secret-value-98765" not in security.redact("x second-secret-value-98765")
    f.unlink()
    # a removed file's values persist in the RESULT cache until the next
    # write-triggered rescan — deliberately the SAFE direction (stale-extra
    # masking; "unregistering a just-revoked value is the risky direction")
    assert "second-secret-value-98765" not in security.redact("x second-secret-value-98765")
    security.invalidate_secret_scan()
    # after a rescan the removed file's values drop out of the scan
    assert "first-secret-value-123" in security.redact("x first-secret-value-123")


def test_store_writes_invalidate_the_scan_cache(tmp_path, monkeypatch):
    """The load-bearing half of F17: a value stored through secrets.py must
    be redacted IMMEDIATELY — no manual invalidation, no TTL window. The
    atomic write choke point owns the cache flush."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as s, security
    monkeypatch.setattr(security, "_SECRETS_DIR", tmp_path / "secrets")
    security.redact("warm the cache")                       # populate
    s.write_connection_secret("repo", "hot", "token",
                              "just-stored-value-45678901")
    assert "just-stored-value-45678901" not in security.redact(
        "x just-stored-value-45678901")


def test_register_all_boot_coverage_and_key_scheme(tmp_path, monkeypatch):
    """register_all is the boot path that guarantees exact-match redaction
    even BELOW security's 16-char scan floor, and it must register under the
    same keys as write_/delete_ or unregister strands the boot-registered
    copy (stale over-redaction)."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    import json
    from devcake import secrets as secrets_store

    conn_dir = tmp_path / "secrets" / "connections"
    conn_dir.mkdir(parents=True)
    # written directly (simulating a prior boot) — 11 chars, under the floor
    (conn_dir / "pmo-linear.json").write_text(json.dumps({"api_key": "short-key-1"}))
    harness_dir = tmp_path / "secrets" / "harness"
    harness_dir.mkdir(parents=True)
    (harness_dir / "XAI_API_KEY.json").write_text(json.dumps({"value": "tiny-value9"}))

    secrets_store.register_all()
    try:
        out = redact("boot leak short-key-1 and tiny-value9 end")
        assert "short-key-1" not in out and "tiny-value9" not in out
        # Instance delete unlinks the file but KEEPS redaction registrations
        # until restart (safe direction — same as field/harness delete).
        secrets_store.delete_connection_instance("pmo", "linear")
        assert not (conn_dir / "pmo-linear.json").exists()
        still = redact("after-delete short-key-1 end")
        assert "short-key-1" not in still
    finally:
        # key-scheme contract: explicit unregister drops boot-registered keys
        from devcake.security import unregister_runtime_secret
        unregister_runtime_secret("conn:pmo:linear:api_key")
        unregister_runtime_secret("harness:XAI_API_KEY")
    out = redact("short-key-1 tiny-value9")
    assert "short-key-1" in out and "tiny-value9" in out


def test_read_only_repo_in_work_set_warns(tmp_path, monkeypatch):
    """Founder request 2026-07-15: RO-only repos are valid but reference-
    only — warn when one sits in a PMO's WORK set."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as s
    from devcake import security
    from devcake.config import AppConfig, PMOInstance, RepoInstance
    s.write_connection_secret("repo", "docs", "token_ro", "ro-only-token-1")
    cfg = AppConfig(repos=[RepoInstance(name="docs",
                                        url="https://gitlab.com/o/docs")],
                    pmos=[PMOInstance(name="linear", team_key="DEV",
                                      repos=["docs"])])
    ids = {w["id"] for w in security.security_warnings(cfg)}
    assert "repo-read-only:docs" in ids
    # as a REFERENCE repo (the intended use) there is no warning
    cfg2 = AppConfig(repos=cfg.repos,
                     pmos=[PMOInstance(name="linear", team_key="DEV",
                                       reference_repos=["docs"])])
    ids2 = {w["id"] for w in security.security_warnings(cfg2)}
    assert "repo-read-only:docs" not in ids2


def test_security_warnings_bodies_match_product_contract(tmp_path, monkeypatch):
    """CAKE-30 claim honesty: security_warnings copy must not outrun
    docs/14 (wrong § pointers, wrong admin surface, understated residual)."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as s
    from devcake import security
    from devcake.config import AppConfig, RepoInstance
    s.write_connection_secret("repo", "main", "token", "write-tok-for-warn-1")
    cfg = AppConfig(repos=[RepoInstance(name="main",
                                        url="https://host.example/o/r")])
    by_id = {w["id"]: w for w in security.security_warnings(cfg)}

    gui = by_id["gui-secrets-basic-auth"]
    body = gui["body"]
    # OIDC/SSO is §11 backlog; dedicated-host / basic-auth posture is §0/§4.
    # §7 is transcript redaction — must not be the posture pointer.
    assert "docs/14 §7" not in body
    assert "§11" in body or "docs/14 §0" in body or "docs/14 §4" in body
    assert "OIDC" in body

    write = by_id["forge-write-token:main"]
    wbody = write["body"]
    # Tokens live on Repositories (#/repos), not a generic "Config page".
    assert "Repositories" in wbody
    assert "Config page" not in wbody
    # Residual matches docs/14 §2/§3: write-capable non-EXECUTE can push
    # and, without forge branch protection, may merge.
    assert "push" in wbody.lower()
    assert "merge" in wbody.lower()


def test_profile_secret_snapshots_are_covered_by_the_redaction_glob(tmp_path, monkeypatch):
    """ADR-0013 glob tripwire: /data/secrets/profiles/{name}.json sits at
    scan level two, so a DORMANT profile's values must mask with ZERO
    changes to security.py. If the profiles store ever moves deeper than
    glob("*/*") reaches, this fails."""
    import json as _json
    from devcake import security
    monkeypatch.setattr(security, "_SECRETS_DIR", tmp_path / "secrets")
    d = tmp_path / "secrets" / "profiles"
    d.mkdir(parents=True)
    value = "profile-dormant-secret-abcdef123456"
    (d / "staging.json").write_text(_json.dumps(
        {"connections": {"repo-main": {"token": value}},
         "harness": {"ANTHROPIC_API_KEY": value + "-h"}}))
    out = security.redact(f"transcript {value} and {value}-h end")
    assert value not in out and MASK in out
