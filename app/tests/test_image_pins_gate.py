"""The docker-run scanner in check_image_pins.py (2026-08-12 audit OPS-M1)
must catch unpinned images in ops scripts and pass the legitimate shapes the
repo actually uses — variable defaults, array splices, multi-line payloads,
python subprocess lists. Test the gate, not just the gated."""

import importlib.util
from pathlib import Path

GATE = Path("/srv/repo-scripts/check_image_pins.py")

DIGEST = "a" * 64


def _load(tmp_path):
    assert GATE.exists(), (
        "mount missing — the pytest runner must bind scripts → /srv/repo-scripts"
    )
    spec = importlib.util.spec_from_file_location("pins_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = tmp_path  # rel-path anchor for offender messages
    return mod


def _sh(mod, tmp_path, body: str) -> list[str]:
    f = tmp_path / "case.sh"
    f.write_text("#!/bin/sh\n" + body)
    offenders: list[str] = []
    mod.check_shell_script(f, offenders)
    return offenders


def _py(mod, tmp_path, body: str) -> list[str]:
    f = tmp_path / "case.py"
    f.write_text(body)
    offenders: list[str] = []
    mod.check_python_script(f, offenders)
    return offenders


def test_unpinned_shell_image_is_an_offender(tmp_path):
    mod = _load(tmp_path)
    off = _sh(mod, tmp_path,
              'docker run --rm -v "$V":/x alpine sh -c "true"\n')
    assert off and "alpine" in off[0]


def test_pinned_variable_default_passes(tmp_path):
    mod = _load(tmp_path)
    off = _sh(mod, tmp_path,
              f'IMG="${{OVERRIDE:-alpine:3.22@sha256:{DIGEST}}}"\n'
              'docker run --rm "$IMG" true\n')
    assert not off, off


def test_variable_without_in_file_default_is_an_offender(tmp_path):
    mod = _load(tmp_path)
    off = _sh(mod, tmp_path, 'docker run --rm "$MYSTERY" true\n')
    assert off and "MYSTERY" in off[0]


def test_devcake_local_image_with_splice_and_entrypoint_passes(tmp_path):
    mod = _load(tmp_path)
    off = _sh(mod, tmp_path,
              'docker run --rm "${ARGS[@]}" --entrypoint sh '
              'devcake/foo:latest -c "x"\n')
    assert not off, off


def test_multiline_run_with_unterminated_payload_resolves_variable(tmp_path):
    mod = _load(tmp_path)
    off = _sh(mod, tmp_path,
              'OUT="$(docker run --rm "${N[@]}" \\\n'
              '  -e A="$B" \\\n'
              "  --entrypoint bash \"$IMAGE\" -c '\n"
              "set -eu\n"
              "true\n"
              "')\"\n"
              'IMAGE="${X:-devcake/dev:latest}"\n')
    assert not off, off


def test_comment_lines_are_ignored(tmp_path):
    mod = _load(tmp_path)
    off = _sh(mod, tmp_path, "# e.g. docker run --rm alpine sh\ntrue\n")
    assert not off, off


def test_python_unpinned_list_is_an_offender(tmp_path):
    mod = _load(tmp_path)
    off = _py(mod, tmp_path,
              'import subprocess\n'
              'subprocess.run(["docker", "run", "--rm",\n'
              '                "-v", f"{v}:/x", "alpine", "sh"])\n')
    assert off and "alpine" in off[0]


def test_python_module_constant_pin_passes(tmp_path):
    mod = _load(tmp_path)
    off = _py(mod, tmp_path,
              f'IMG = ("alpine:3.22@sha256:" "{DIGEST}")\n'
              'import subprocess\n'
              'subprocess.run(["docker", "run", "--rm",\n'
              '                "-v", f"{v}:/x", IMG, "sh"])\n')
    assert not off, off


def test_python_fstring_image_is_an_offender(tmp_path):
    mod = _load(tmp_path)
    off = _py(mod, tmp_path,
              'import subprocess\n'
              'subprocess.run(["docker", "run", f"{img}", "sh"])\n')
    assert off and "not a resolvable literal" in off[0]


def _df(mod, tmp_path, body: str) -> list[str]:
    f = tmp_path / "Dockerfile"
    f.write_text(body)
    offenders: list[str] = []
    mod.check_dockerfile(f, offenders)
    return offenders


def test_unpinned_syntax_frontend_is_an_offender(tmp_path):
    """# syntax= is an external BuildKit image — floating tags must fail."""
    mod = _load(tmp_path)
    off = _df(mod, tmp_path,
              "# syntax=docker/dockerfile:1\n"
              f"FROM alpine:3.22@sha256:{DIGEST}\n")
    assert off and "syntax" in off[0].lower()


def test_pinned_syntax_frontend_passes(tmp_path):
    mod = _load(tmp_path)
    off = _df(mod, tmp_path,
              f"# syntax=docker/dockerfile:1@sha256:{DIGEST}\n"
              f"FROM alpine:3.22@sha256:{DIGEST}\n")
    assert not off, off


def test_ordinary_hash_comment_is_not_a_syntax_line(tmp_path):
    mod = _load(tmp_path)
    off = _df(mod, tmp_path,
              "# syntax note: keep digests pinned\n"
              f"FROM alpine:3.22@sha256:{DIGEST}\n")
    assert not off, off
