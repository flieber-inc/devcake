"""House CLI pins: Grok's installer must receive ARG GROK_VERSION.

Public seam (PLAN_CLI_PINS Slice 0a): `ARG GROK_VERSION` and the RUN that
passes it to the vendored installer. Independent expected values are the
ARG literal and the RUN line — not "the installer happens to run."
"""

from __future__ import annotations

import re
from pathlib import Path

# images/Dockerfile sits outside the app-test COPY. Runners bind it
# (scripts/pytest_app.sh / ci.yml). Host PYTHONPATH=app finds the tree.
_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "images" / "Dockerfile",
    Path("/srv/images.Dockerfile"),
]

HOUSE_GROK = "0.2.112"

MOUNT_HINT = (
    "images/Dockerfile missing — bind it at /srv/images.Dockerfile "
    "(see scripts/pytest_app.sh / ci.yml)"
)


def _dockerfile() -> str:
    path = next((p for p in _CANDIDATES if p.is_file()), None)
    assert path is not None, MOUNT_HINT
    return path.read_text()


def test_grok_house_pin_is_the_arg_literal():
    match = re.search(r"^ARG GROK_VERSION=(\S+)\s*$", _dockerfile(), re.M)
    assert match is not None, "ARG GROK_VERSION=<semver> must exist"
    assert match.group(1) == HOUSE_GROK


def test_app_dockerfile_digest_arg_defaults_to_the_sentinel():
    from devcake.house_pins import SENTINEL_DIGEST

    candidates = [
        Path(__file__).resolve().parents[2] / "app" / "Dockerfile",
        Path("/srv/app.Dockerfile"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    assert path is not None, "app/Dockerfile missing — bind /srv/app.Dockerfile"
    text = path.read_text()
    assert f"ARG DEVCAKE_APP_DIGEST={SENTINEL_DIGEST}" in text


def test_house_pins_module_matches_dockerfile_arg_defaults():
    """Q1 start: app literals stay equal to the bake ARGs."""
    from devcake.house_pins import DOCKERFILE_ARG, HOUSE_PINS

    text = _dockerfile()
    for template, version in HOUSE_PINS.items():
        arg = DOCKERFILE_ARG[template]
        match = re.search(rf"^ARG {re.escape(arg)}=(\S+)\s*$", text, re.M)
        assert match is not None, arg
        assert match.group(1) == version, template


def test_grok_installer_run_passes_the_arg():
    """Ungated `bash /tmp/grok-install.sh` is the hole. The ARG must be argv."""
    assert re.search(
        r'^RUN bash /tmp/grok-install.sh "\$\{GROK_VERSION\}" && rm /tmp/grok-install.sh\s*$',
        _dockerfile(),
        re.M,
    ), "installer RUN must pass ${GROK_VERSION} as positional argv"
