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
