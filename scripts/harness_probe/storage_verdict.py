"""Whether a nested-engine probe used a native overlay diff.

Independent of rig_ok. fuse-overlayfs still starts containers; it fills
the Dev cgroup with page cache. The probe records this. It does not tell
a Dev that containers are unavailable.
"""


def storage_ok(*, native_overlay_diff: str, fuse_count: int) -> bool:
    return native_overlay_diff == "true" and int(fuse_count) == 0
