"""Host factory: compile + probe + receipt from the app-published keep-set.

The app never talks to Docker. This package is the host verb. It reads only
the keep-set file (never Dev Type YAML) and independently validates every
pin before it will name an image.
"""

from .liveness import (
    SENTINEL,
    UNHEALTHY_NEED,
    append_baker_event,
    classify_app,
    tick_decision,
    unhealthy_verdict,
)
from .core import (
    ARG_NAMES,
    KNOWN_TEMPLATES,
    LAUNCH_SUPPORTED,
    BakeJob,
    InvalidKeepSet,
    KeepSet,
    Pin,
    bake_argv,
    house_from_dockerfile,
    image_ref,
    load_keep_set,
    plan_bakes,
    reconcile,
    run_bake,
    write_status,
)

__all__ = [
    "ARG_NAMES",
    "KNOWN_TEMPLATES",
    "LAUNCH_SUPPORTED",
    "BakeJob",
    "InvalidKeepSet",
    "KeepSet",
    "Pin",
    "bake_argv",
    "house_from_dockerfile",
    "image_ref",
    "load_keep_set",
    "plan_bakes",
    "SENTINEL",
    "UNHEALTHY_NEED",
    "append_baker_event",
    "classify_app",
    "unhealthy_verdict",
    "reconcile",
    "run_bake",
    "tick_decision",
    "write_status",
]
