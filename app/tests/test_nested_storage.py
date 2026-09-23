"""Rootless Podman store: native overlay on the workspace bind.

fuse-overlayfs is what a Dev cgroup pays for when the store sits on the
container's own overlay upper. The image conf and the probe receipt both
name the native-diff contract. A red storage verdict must NOT flip
rig_ok: fuse still runs containers, it just fills the cgroup.
"""
from pathlib import Path

STORAGE_CANDIDATES = [
    Path("/srv/images/common/containers/storage.conf"),
    Path(__file__).parents[2] / "images" / "common" / "containers" / "storage.conf",
    Path(__file__).parents[1] / "images" / "common" / "containers" / "storage.conf",
]
DOCKERFILE_CANDIDATES = [
    Path("/srv/images.Dockerfile"),
    Path(__file__).parents[2] / "images" / "Dockerfile",
    Path(__file__).parents[1] / "images" / "Dockerfile",
]
PROBE_CANDIDATES = [
    Path("/srv/repo-scripts/harness_probe/nested_probe.sh"),
    Path(__file__).parents[2] / "scripts" / "harness_probe" / "nested_probe.sh",
    Path(__file__).parents[1] / "scripts" / "harness_probe" / "nested_probe.sh",
]
VERDICT_CANDIDATES = [
    Path("/srv/repo-scripts/harness_probe/storage_verdict.py"),
    Path(__file__).parents[2] / "scripts" / "harness_probe" / "storage_verdict.py",
    Path(__file__).parents[1] / "scripts" / "harness_probe" / "storage_verdict.py",
]


def _first(paths: list[Path]) -> Path:
    for path in paths:
        if path.exists():
            return path
    raise AssertionError(f"none of {paths} exist")


def test_storage_conf_puts_the_store_on_the_workspace_bind():
    text = _first(STORAGE_CANDIDATES).read_text()
    assert 'graphroot = "/workspace/.podman-storage"' in text
    assert 'driver = "overlay"' in text
    assert 'mount_program = ""' in text
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        assert "fuse-overlayfs" not in line
    dockerfile = _first(DOCKERFILE_CANDIDATES).read_text()
    assert "storage.conf" in dockerfile


def test_native_overlay_verdict_is_independent_of_rig_ok():
    import importlib.util
    path = _first(VERDICT_CANDIDATES)
    spec = importlib.util.spec_from_file_location("storage_verdict", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.storage_ok(native_overlay_diff="true", fuse_count=0) is True
    assert mod.storage_ok(native_overlay_diff="false", fuse_count=0) is False
    assert mod.storage_ok(native_overlay_diff="true", fuse_count=3) is False
    probe = _first(PROBE_CANDIDATES).read_text()
    assert "NP_NATIVE_DIFF" in probe
    assert "NP_FUSE_COUNT" in probe
    # rig_ok stays the "containers can start" verdict
    assert 'e["NP_RIG_OK"] == "true"' in probe
