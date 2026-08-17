"""Slice 3: DevType.cli_version is empty or a concrete semver.

`latest` is a resolve-once gesture, never a stored value. Every
registry template accepts a stored semver the same way. Independent
expected values are the literals `latest`, `2.1.250`, and 422-shaped errors.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from devcake.config import DevType


def test_empty_cli_version_is_the_house_pin():
    dt = DevType(name="implementer", harness_template="grok-build")
    assert dt.cli_version == ""


def test_concrete_semver_round_trips():
    dt = DevType(name="implementer", harness_template="grok-build",
                 cli_version="1.0.4")
    assert dt.cli_version == "1.0.4"
    again = DevType.model_validate(dt.model_dump())
    assert again.cli_version == "1.0.4"


def test_stored_latest_is_rejected():
    with pytest.raises(ValidationError, match="latest"):
        DevType(name="implementer", harness_template="grok-build",
                cli_version="latest")


def test_every_registry_template_accepts_a_stored_semver():
    from devcake.harness import HARNESSES

    for template in HARNESSES:
        dt = DevType(name="pin-dev", harness_template=template,
                     cli_version="0.84.2")
        assert dt.cli_version == "0.84.2"


def test_effective_cli_version_is_house_or_the_stored_pin():
    from devcake.house_pins import effective_cli_version

    house = DevType(name="implementer", harness_template="grok-build")
    assert effective_cli_version(house) == "0.2.112"
    pinned = DevType(name="implementer", harness_template="grok-build",
                     cli_version="1.0.4")
    assert effective_cli_version(pinned) == "1.0.4"


def test_staffing_looks_up_the_stored_pin_not_the_house_arg():
    from devcake.staffing import HarnessNotStaffed, require_staffed

    class Store:
        def __init__(self):
            self.asked = None

        def get(self, *, digest, template, cli_version):
            self.asked = (digest, template, cli_version)
            return None

    dt = DevType(name="implementer", harness_template="grok-build",
                 cli_version="1.0.4")
    store = Store()
    with pytest.raises(HarnessNotStaffed, match="1.0.4"):
        require_staffed(dt, digest="sha256:abc", store=store)
    assert store.asked == ("sha256:abc", "grok-build", "1.0.4")


def test_keep_set_records_concrete_pins(tmp_path):
    from devcake.keep_set import publish_keep_set

    publish_keep_set(
        {
            "implementer": DevType(
                name="implementer", harness_template="grok-build",
                cli_version="1.0.4"),
            "also": DevType(name="also", harness_template="grok-build"),
            "judgment": DevType(name="judgment",
                                harness_template="claude-code"),
        },
        root=tmp_path,
    )
    import json
    body = json.loads((tmp_path / "harness_keep_set.json").read_text())
    assert body["templates"] == ["claude-code", "grok-build"]
    assert body["pins"] == [
        {"template": "claude-code", "cli_version": "2.1.229"},
        {"template": "grok-build", "cli_version": "0.2.112"},
        {"template": "grok-build", "cli_version": "1.0.4"},
    ]


def test_latest_gesture_returns_a_planted_semver():
    from devcake.versions import resolve_latest

    class Planted:
        def latest(self, template: str) -> str:
            assert template == "claude-code"
            return "2.1.250"

    assert resolve_latest("claude-code", source=Planted()) == "2.1.250"


def test_latest_gesture_rejects_a_non_semver():
    from devcake.versions import resolve_latest

    class Junk:
        def latest(self, template: str) -> str:
            return "not-a-version"

    with pytest.raises(ValueError, match="semver"):
        resolve_latest("claude-code", source=Junk())


def test_latest_resolves_for_every_registry_template():
    from devcake.harness import HARNESSES
    from devcake.versions import resolve_latest

    class Planted:
        def latest(self, template: str) -> str:
            return "9.9.9"

    for template in HARNESSES:
        assert resolve_latest(template, source=Planted()) == "9.9.9"


def test_latest_cli_service_returns_the_planted_number():
    import asyncio
    from devcake.api import devtypes_service

    class Planted:
        def latest(self, template: str) -> str:
            return "2.1.250"

    loop = asyncio.new_event_loop()
    body = loop.run_until_complete(
        devtypes_service.latest_cli_version("claude-code", source=Planted()))
    assert body == {"cli_version": "2.1.250"}
    pi = loop.run_until_complete(
        devtypes_service.latest_cli_version("pi", source=Planted()))
    assert pi == {"cli_version": "2.1.250"}
    loop.close()
