"""SPA FALLBACK is a pinned mirror of GET /connections/registry (ADR-0034).

The admin SPA keeps a cold-start copy of registry metadata in
admin/spa/src/lib/registry_fallback.json (imported by registry.js). That
copy is physically unavoidable (another language / offline path) — so it
must be field-by-field equal to the live registry projection. Drift turns
this test red.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from devcake.api.connections_service import connections_registry
from devcake.adapters.registry import PMO_SYSTEMS

# Host checkout: repo/app/tests → repo/admin/...
# Container: bind at /srv/admin-registry-fallback.json (pytest_app.sh / ci.yml).
_FALLBACK_CANDIDATES = [
    Path("/srv/admin-registry-fallback.json"),
    Path(__file__).resolve().parents[2]
    / "admin" / "spa" / "src" / "lib" / "registry_fallback.json",
]

# Operator-visible PMO fields the FALLBACK mirror must carry (cold-start SPA).
_PMO_PIN_FIELDS = (
    "id",
    "display_name",
    "needs_api_base",
    "team_key_label",
    "team_key_help",
    "api_base_help",
    "supports_priority",
    "operator_note",
    "attachments_supported",
    "relations_supported",
    "experimental",
)


def _fallback_path() -> Path:
    for p in _FALLBACK_CANDIDATES:
        if p.exists():
            return p
    return _FALLBACK_CANDIDATES[-1]


def _load_fallback() -> dict:
    path = _fallback_path()
    assert path.exists(), (
        f"SPA registry FALLBACK missing at {path} — bind "
        "admin/spa/src/lib/registry_fallback.json → "
        "/srv/admin-registry-fallback.json in the pytest runner, or run "
        "from a full repo checkout (PYTHONPATH=app)."
    )
    return json.loads(path.read_text())


def _live_registry() -> dict:
    return asyncio.new_event_loop().run_until_complete(connections_registry())


def test_spa_fallback_pmo_systems_match_live_registry():
    """Every FALLBACK pmo_systems entry equals the API projection field-by-field."""
    fallback = _load_fallback()
    live = _live_registry()
    live_by_id = {s["id"]: s for s in live["pmo_systems"]}
    fb_by_id = {s["id"]: s for s in fallback["pmo_systems"]}

    assert set(fb_by_id) == set(live_by_id), (
        f"SPA FALLBACK pmo system ids drifted from registry: "
        f"fallback={sorted(fb_by_id)} live={sorted(live_by_id)}"
    )
    for sid in sorted(live_by_id):
        for field in _PMO_PIN_FIELDS:
            assert field in fb_by_id[sid], (
                f"SPA FALLBACK pmo_systems[{sid!r}] missing field {field!r}"
            )
            assert fb_by_id[sid][field] == live_by_id[sid][field], (
                f"SPA FALLBACK drifted from registry for {sid}.{field}:\n"
                f"  fallback={fb_by_id[sid][field]!r}\n"
                f"  live    ={live_by_id[sid][field]!r}"
            )


def test_spa_fallback_forges_and_shapes_match_live_registry():
    fallback = _load_fallback()
    live = _live_registry()

    live_forges = {f["id"]: f["display_name"] for f in live["forges"]}
    fb_forges = {f["id"]: f["display_name"] for f in fallback["forges"]}
    assert fb_forges == live_forges, (
        f"SPA FALLBACK forges drifted: fallback={fb_forges} live={live_forges}"
    )

    assert set(fallback["secret_shape_prefixes"]) == set(
        live["secret_shape_prefixes"]
    ), (
        f"SPA FALLBACK secret_shape_prefixes drifted: "
        f"fallback={fallback['secret_shape_prefixes']!r} "
        f"live={live['secret_shape_prefixes']!r}"
    )
    assert fallback["managed_labels_expected"] == live["managed_labels_expected"], (
        f"SPA FALLBACK managed_labels_expected drifted: "
        f"fallback={fallback['managed_labels_expected']} "
        f"live={live['managed_labels_expected']}"
    )


def test_pmo_experimental_flags_match_launch_roster():
    """Launch-supported vs experimental PMO roster is encoded once on PMOSystemInfo.

    Docs (00, 05, 16) and GET /connections/registry must match this pin —
    do not invent SPA chips; metadata honesty is required. All four current
    systems are launch-supported; the experimental set stays empty until a
    future genuinely-experimental system opts in.
    """
    launch = {"linear", "gitea_issues", "github_issues", "gitlab_issues"}
    experimental: set[str] = set()
    assert set(PMO_SYSTEMS) == launch | experimental

    for sid in launch:
        assert PMO_SYSTEMS[sid].experimental is False, sid
    for sid in experimental:
        assert PMO_SYSTEMS[sid].experimental is True, sid

    live = _live_registry()
    by_id = {s["id"]: s for s in live["pmo_systems"]}
    for sid in launch:
        assert by_id[sid]["experimental"] is False, sid
    for sid in experimental:
        assert by_id[sid]["experimental"] is True, sid
