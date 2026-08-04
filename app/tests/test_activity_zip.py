"""Activity-folder zip expansion (plan slice D): every .zip attachment is
kept as bytes AND extracted under {stem}/ with zip-slip hardening."""

from __future__ import annotations

import base64
import io
import zipfile
from datetime import datetime, timezone

from devcake.domain.model import Activity, ActivityEntry, AttachmentRef
from devcake.domain.orchestrator import activity_payload as activity
from test_mapper import MapPMO, run_coro

NOW = datetime.now(timezone.utc)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, data in members.items():
            z.writestr(path, data)
    return buf.getvalue()


def test_safe_activity_relpath_rejects_slip():
    assert activity.safe_activity_relpath("a/b.md") == "a/b.md"
    assert activity.safe_activity_relpath("../evil") is None
    assert activity.safe_activity_relpath("/abs") is None
    assert activity.safe_activity_relpath("a/../../x") is None
    assert activity.safe_activity_relpath("") is None
    assert activity.safe_activity_relpath("..") is None
    assert activity.safe_activity_relpath("ok.md") == "ok.md"


def test_expand_zip_attachment_happy_and_caps():
    data = _zip_bytes({"a.md": b"hello", "dir/b.txt": b"world"})
    out = activity.expand_zip_attachment("report.zip", data,
                                         max_bytes=10 * 1024 * 1024)
    paths = {p: c for p, c in out}
    assert paths["report/a.md"] == b"hello"
    assert paths["report/dir/b.txt"] == b"world"

    # zip-slip members dropped
    evil = _zip_bytes({"../evil.txt": b"x", "ok.txt": b"y"})
    out2 = activity.expand_zip_attachment("pack.zip", evil, max_bytes=1024)
    assert out2 == [("pack/ok.txt", b"y")]

    # corrupt
    assert activity.expand_zip_attachment("x.zip", b"not-a-zip",
                                          max_bytes=1024) == []

    # byte cap stops early (small first member fits, large second does not)
    big = _zip_bytes({"small.txt": b"ab", "big.txt": b"x" * 100})
    out3 = activity.expand_zip_attachment("c.zip", big, max_bytes=10)
    assert out3 == [("c/small.txt", b"ab")]

    # declared uncompressed size alone can trip the pre-read cap (zip bomb
    # defense: do not decompress a member that cannot fit the remaining budget)
    bomb = _zip_bytes({"huge.txt": b"x" * 50})
    out4 = activity.expand_zip_attachment("b.zip", bomb, max_bytes=10)
    assert out4 == []


def test_expand_zip_attachment_drops_tree_conflicts():
    """A crafted zip holding both `x` and `x/y` (or duplicate names) must
    yield one valid file tree — a file and a directory sharing a name is
    unrepresentable in git and crashed the entrypoint's mkdir/write."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x", b"file")
        z.writestr("x/y", b"nested")      # conflicts with the file `x`
        z.writestr("dup.txt", b"one")
        z.writestr("dup.txt", b"two")     # duplicate member name
    out = activity.expand_zip_attachment("p.zip", buf.getvalue(),
                                         max_bytes=1024)
    assert [p for p, _ in out] == ["p/x", "p/dup.txt"]


def test_unique_name_respects_extraction_dirs():
    """A flat attachment named like an existing extraction DIRECTORY gets
    the suffix rule — file-vs-dir is a tree conflict, not a coexistence."""
    used = {"ACTIVITY.md", "report/a.md"}
    assert activity._unique_name("report", used) == "report-2"
    assert activity._tree_conflict("report", {"report/a.md"})
    assert activity._tree_conflict("report/a.md/x", {"report/a.md"})
    assert not activity._tree_conflict("report-3", {"report/a.md"})


def test_activity_payload_expands_zip(tmp_path):
    from test_mapper import m as mission_m, make_mgr, _returns

    mission = mission_m("i1", "T-1")
    url = "https://uploads.linear.app/deliverable.zip"
    zdata = _zip_bytes({"REPORT.md": b"# r", "nested/x.txt": b"x"})
    entries = [
        ActivityEntry(ts=NOW, author="bot", kind="comment",
                      body=f"[T-1-deliverable.zip]({url})",
                      attachments=[AttachmentRef(
                          url=url, name="T-1-deliverable.zip")]),
    ]
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    pmo.download_asset = _returns(zdata)
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    names = [a["filename"] for a in payload["attachments"]]
    assert "T-1-deliverable.zip" in names
    assert "T-1-deliverable/REPORT.md" in names
    assert "T-1-deliverable/nested/x.txt" in names
    assert "[attachment: T-1-deliverable.zip]" in payload["activity_md"]
    by_name = {a["filename"]: base64.b64decode(a["content_b64"])
               for a in payload["attachments"]}
    assert by_name["T-1-deliverable/REPORT.md"] == b"# r"


def test_activity_snapshot_keeps_nested_paths():
    payload = {
        "mission_md": "# m",
        "activity_md": "# a",
        "attachments": [
            {"filename": "T-1-deliverable.zip",
             "content_b64": base64.b64encode(b"ZIP").decode()},
            {"filename": "T-1-deliverable/REPORT.md",
             "content_b64": base64.b64encode(b"# r").decode()},
        ],
    }
    files = activity._activity_snapshot_files(payload)
    paths = {f["path"] for f in files}
    assert "T-1-deliverable/REPORT.md" in paths
    assert "T-1-deliverable.zip" in paths
    assert "REPORT.md" not in paths       # never collapsed to basename


def _no_tree_conflicts(names: list[str]) -> bool:
    seen: set[str] = set()
    for n in names:
        if activity._tree_conflict(n, seen):
            return False
        seen.add(n)
    return True


def test_zip_stem_never_collides_with_flat_attachment(tmp_path):
    """A feed attachment named exactly like a zip's stem (either order) must
    not produce a file-vs-dir pair in the payload — the extraction remaps
    to `{stem}-2/…` (zip first) or the flat name gets `-2` (zip second)."""
    from test_mapper import m as mission_m, make_mgr

    zdata = _zip_bytes({"a.md": b"z"})
    flat_url = "https://uploads.linear.app/flat"
    zip_url = "https://uploads.linear.app/report.zip"

    async def dl(url):
        return {flat_url: b"flat-bytes", zip_url: zdata}[url]

    for order in (("report", "report.zip"), ("report.zip", "report")):
        atts = [AttachmentRef(url=flat_url if n == "report" else zip_url,
                              name=n) for n in order]
        entries = [ActivityEntry(ts=NOW, author="h", kind="comment",
                                 body="files", attachments=atts)]
        mission = mission_m("i1", "T-1")
        pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
        pmo.download_asset = dl
        mgr = make_mgr(tmp_path, pmo)
        payload = run_coro(mgr.activity_payload("i1"))
        names = [a["filename"] for a in payload["attachments"]]
        assert _no_tree_conflicts(names), f"conflict in {names} ({order})"
        if order[0] == "report":          # flat won the stem → dir remapped
            assert "report-2/a.md" in names
        else:                             # dir won the stem → flat suffixed
            assert "report/a.md" in names and "report-2" in names
