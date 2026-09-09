"""ADR-0042 §8 — the golden test: the feed the pre-card build posted and
the feed the card build posts for the same mission unfold to the SAME
Dev folder (ACTIVITY.md, MISSION.md, attachment names), once timestamps
are normalised. Nothing the Devs receive changes."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from devcake.domain.model import Activity, ActivityEntry, AttachmentRef
from devcake.domain.orchestrator.feed import is_devcake_comment

from golden_feeds import record_three_step
from test_steward import MapPMO, m as steward_mission, make_mgr as steward_mgr
from test_steward import run_coro

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
BASE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
_TS = re.compile(r"^### \d{4}-\d{2}-\d{2} \d{2}:\d{2} —", re.MULTILINE)


class GoldenPMO(MapPMO):
    """Serves a recorded feed as the mirror's full read; attachment bytes
    are deterministic (their name), so the folder's file set compares."""
    async def download_asset(self, url):
        return url.rsplit("/", 1)[-1].encode()


def _entries(recorded: dict) -> list[ActivityEntry]:
    out = []
    for i, e in enumerate(recorded["entries"]):
        out.append(ActivityEntry(
            ts=BASE + timedelta(minutes=i), author="cake", kind="comment",
            body=e["body"], entry_id=f"c{i}",
            attachments=[AttachmentRef(url=f"https://fake/{n}", name=n)
                         for n in e["attachments"]]))
    return out


def _mirror(tmp_path, recorded: dict) -> dict:
    mission = steward_mission("p1", "T-1")
    mission.description = recorded.get("description") or ""
    pmo = GoldenPMO([], activity=Activity(mission=mission,
                                          entries=_entries(recorded)))
    mgr = steward_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("p1"))
    return {"activity": _TS.sub("### <ts> —", payload["activity_md"]),
            "mission": payload["mission_md"],
            "files": sorted(a["filename"] for a in payload["attachments"])}


def test_legacy_and_card_feeds_unfold_to_the_same_folder(tmp_path):
    legacy = json.loads((FIXTURES / "legacy_three_step.json").read_text())
    card = record_three_step(tmp_path / "record")
    assert len(card["entries"]) < len(legacy["entries"])   # fewer comments…
    assert all(is_devcake_comment(e["body"]) for e in card["entries"])
    a = _mirror(tmp_path / "legacy", legacy)
    b = _mirror(tmp_path / "card", card)
    assert a["files"] == b["files"] == [
        "1_ONBOARD.md", "2_EXECUTE.md", "3_REVIEW.md", "DISCOVERY_2.md"]
    assert a["mission"] == b["mission"]
    assert a["activity"] == b["activity"]                   # …the same folder


def test_the_card_feed_carries_no_html_marker_and_one_card_per_step(tmp_path):
    card = record_three_step(tmp_path)
    bodies = [e["body"] for e in card["entries"]]
    assert not any("<!--" in b for b in bodies)
    assert [b.split(" ")[1:3] for b in bodies if " Step " in b] == \
        [["Step", "1"], ["Step", "2"], ["Step", "3"]]
