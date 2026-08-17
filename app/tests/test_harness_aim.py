"""Backend adaptor: CLI + base URL → env, argv, files (docs/08 §8).

Public seam: aim(template, base_url, *, api_key, cli_version="") -> Aimed.
Independent expected values are the env-name / argv / path literals — not
a CLI run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_COMMON = [
    Path(__file__).resolve().parents[2] / "images" / "common",
    Path("/srv/images/common"),
]


def _load_aim():
    root = next((p for p in _COMMON if (p / "devcake_dev").is_dir()), None)
    assert root is not None, "images/common missing — bind /srv/images/common"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from devcake_dev.harness import aim as aim_mod
    return aim_mod


def test_unknown_template_is_refused():
    aim_mod = _load_aim()
    with pytest.raises(ValueError, match="nginx"):
        aim_mod.aim("nginx", "http://127.0.0.1:9", api_key="k")


def test_empty_base_url_writes_nothing():
    aim_mod = _load_aim()
    got = aim_mod.aim("grok-build", "", api_key="k", cli_version="0.2.112")
    assert got.env == {}
    assert got.extra_argv == ()
    assert got.files == ()


def test_claude_code_aims_without_v1_suffix():
    aim_mod = _load_aim()
    got = aim_mod.aim("claude-code", "http://127.0.0.1:9", api_key="k")
    assert got.env == {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_AUTH_TOKEN": "k",
    }
    assert got.extra_argv == ()
    assert got.files == ()


def test_grok_house_aims_with_v1_in_the_url():
    aim_mod = _load_aim()
    got = aim_mod.aim(
        "grok-build", "http://127.0.0.1:9/v1", api_key="k",
        cli_version="0.2.112")
    assert got.env == {
        "GROK_MODELS_BASE_URL": "http://127.0.0.1:9/v1",
        "XAI_API_KEY": "k",
    }
    assert got.extra_argv == ()
    assert got.files == ()


def test_grok_1_0_4_aims_with_a_model_block_in_config_toml():
    """1.0.4 ignores GROK_MODELS_BASE_URL for --model stub-model unless
    ~/.grok/config.toml declares that model with base_url + env_key.
    Measured 2026-08-16 on grok 1.0.4 (d846eb93d9)."""
    aim_mod = _load_aim()
    got = aim_mod.aim(
        "grok-build", "http://127.0.0.1:9/v1", api_key="k",
        cli_version="1.0.4", model="stub-model")
    assert got.env == {"XAI_API_KEY": "k"}
    assert got.extra_argv == ()
    assert len(got.files) == 1
    dest = got.files[0]
    assert dest.path_hint == "~/.grok/config.toml"
    assert dest.content == (
        "[model.stub-model]\n"
        'model = "stub-model"\n'
        'base_url = "http://127.0.0.1:9/v1"\n'
        'env_key = "XAI_API_KEY"\n'
    )


def test_pi_aims_with_models_json_and_provider_flag():
    """Pi has no OPENAI_BASE_URL. House 0.84.2 needs ~/.pi/agent/models.json
    plus --provider. Measured 2026-08-16: leftover OPENAI_BASE_URL +
    --provider openai is AUTH; models.json + --provider devcake is 11."""
    aim_mod = _load_aim()
    got = aim_mod.aim(
        "pi", "http://127.0.0.1:9/v1", api_key="k", model="stub-model")
    assert got.env == {"OPENAI_API_KEY": "k"}
    assert got.extra_argv == ("--provider", "devcake")
    assert len(got.files) == 1
    dest = got.files[0]
    assert dest.path_hint == "~/.pi/agent/models.json"
    assert dest.content == (
        "{\n"
        '  "providers": {\n'
        '    "devcake": {\n'
        '      "baseUrl": "http://127.0.0.1:9/v1",\n'
        '      "api": "openai-completions",\n'
        '      "apiKey": "$OPENAI_API_KEY",\n'
        '      "models": [\n'
        '        {\n'
        '          "id": "stub-model"\n'
        '        }\n'
        '      ]\n'
        '    }\n'
        '  }\n'
        '}\n'
    )


def test_opencode_aims_with_xdg_provider_json():
    """OpenCode ignores OPENAI_BASE_URL. House 1.18.18 needs an XDG
    opencode.json custom provider plus --model devcake/<id>.
    Measured 2026-08-16: leftover OPENAI_BASE_URL is FAULT; XDG
    provider.devcake.options.baseURL + $OPENAI_API_KEY classifies 11."""
    aim_mod = _load_aim()
    got = aim_mod.aim(
        "opencode", "http://127.0.0.1:9/v1", api_key="k", model="stub-model")
    assert got.env == {"OPENAI_API_KEY": "k"}
    assert got.extra_argv == ()
    assert len(got.files) == 1
    dest = got.files[0]
    assert dest.path_hint == "~/.config/opencode/opencode.json"
    assert dest.content == (
        "{\n"
        '  "provider": {\n'
        '    "devcake": {\n'
        '      "npm": "@ai-sdk/openai-compatible",\n'
        '      "name": "DevCake",\n'
        '      "options": {\n'
        '        "baseURL": "http://127.0.0.1:9/v1",\n'
        '        "apiKey": "$OPENAI_API_KEY"\n'
        '      },\n'
        '      "models": {\n'
        '        "stub-model": {\n'
        '          "name": "stub-model"\n'
        '        }\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}\n'
    )


def test_qwen_aims_with_settings_model_providers():
    """Qwen house 0.21.12 ignores leftover OPENAI_BASE_URL (FAULT).
    Measured 2026-08-16: ~/.qwen/settings.json security.auth.selectedType
    openai + modelProviders.openai[].baseUrl classifies 11. envKey names
    OPENAI_API_KEY; providers-only without selectedType is FAULT."""
    aim_mod = _load_aim()
    got = aim_mod.aim(
        "qwen-code", "http://127.0.0.1:9/v1", api_key="k", model="stub-model")
    assert got.env == {"OPENAI_API_KEY": "k"}
    assert got.extra_argv == ()
    assert len(got.files) == 1
    dest = got.files[0]
    assert dest.path_hint == "~/.qwen/settings.json"
    assert dest.content == (
        "{\n"
        '  "security": {\n'
        '    "auth": {\n'
        '      "selectedType": "openai"\n'
        '    }\n'
        '  },\n'
        '  "modelProviders": {\n'
        '    "openai": [\n'
        '      {\n'
        '        "id": "stub-model",\n'
        '        "envKey": "OPENAI_API_KEY",\n'
        '        "baseUrl": "http://127.0.0.1:9/v1"\n'
        '      }\n'
        '    ]\n'
        '  }\n'
        '}\n'
    )


def test_opencode_strips_provider_prefix_from_the_model_id():
    """DevType.model / probe pin is provider/id; the file models key is id."""
    aim_mod = _load_aim()
    got = aim_mod.aim(
        "opencode", "http://127.0.0.1:9/v1", api_key="k",
        model="devcake/stub-model")
    assert '"stub-model"' in got.files[0].content
    assert '"devcake/stub-model"' not in got.files[0].content


def test_codex_aims_with_provider_override_block():
    aim_mod = _load_aim()
    got = aim_mod.aim("codex", "http://127.0.0.1:9/v1", api_key="k")
    assert got.env == {"CODEX_API_KEY": "k"}
    assert got.extra_argv == (
        "-c", "model_provider=stub",
        "-c", "model_providers.stub.name=Stub",
        "-c", "model_providers.stub.env_key=CODEX_API_KEY",
        "-c", "model_providers.stub.wire_api=responses",
        "-c", "model_providers.stub.base_url=http://127.0.0.1:9/v1",
        "-c", "model_providers.stub.request_max_retries=0",
        "-c", "model_providers.stub.stream_max_retries=0",
    )
    assert got.files == ()


def test_stub_lane_uses_the_cli_url_shape():
    aim_mod = _load_aim()
    assert aim_mod.stub_lane(
        "claude-code", "http://127.0.0.1:9", "healthy",
    ) == "http://127.0.0.1:9/s/healthy"
    assert aim_mod.stub_lane(
        "grok-build", "http://127.0.0.1:9", "http_401",
    ) == "http://127.0.0.1:9/s/http_401/v1"
    assert aim_mod.stub_lane(
        "codex", "http://127.0.0.1:9", "empty",
    ) == "http://127.0.0.1:9/s/empty/v1"


def test_aim_module_is_the_backend_recipe_chokepoint():
    """Recipes live in aim.py — one function per template, no secret_env-only path."""
    aim = _load_aim()
    for template in (
        "claude-code", "codex", "grok-build", "pi", "opencode", "qwen-code",
    ):
        got = aim.aim(template, "", api_key="k")
        assert got.env == {}
        assert got.files == ()


def test_probe_live_has_no_per_template_aim_ladder():
    """The adaptor is the chokepoint — live.py must not keep the if-ladder."""
    candidates = [
        Path(__file__).resolve().parents[2] / "scripts" / "harness_probe" / "live.py",
        Path("/srv/repo-scripts/harness_probe/live.py"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None
    text = path.read_text()
    assert "from devcake_dev.harness.aim import aim" in text or "aim(" in text
    assert "GROK_MODELS_BASE_URL" not in text
    assert "model_providers.stub.base_url" not in text
