"""docs/14 §5 — the redaction filter (M7 exit criterion)."""
import os

from devcake.security import MASK, redact


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


# ── registry-driven redaction: the superset tripwire (docs/14 §5) ────────────
# The v0 lists frozen as literals: the generated SECRET_ENV_VARS/TOKEN_PATTERNS
# must always contain AT LEAST these. A dropped entry means a secret could hit
# the PMO unredacted — unrecoverable — so this test must exist BEFORE and pass
# AFTER any registry rework.

V0_SECRET_ENV_VARS = [
    "LINEAR_API_KEY", "GITHUB_TOKEN", "GITHUB_REVIEWER_TOKEN", "GITLAB_TOKEN",
    "GITLAB_REVIEWER_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "XAI_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "REDIS_PASSWORD",
    "DAGU_PASSWORD", "ADMIN_PASSWORD", "OO_ROOT_PASSWORD",
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
