"""Activity-folder zip expansion (plan slice D): every .zip attachment is
kept as bytes AND extracted under {stem}/ with zip-slip hardening."""

from __future__ import annotations

import base64
import io
import zipfile
from datetime import datetime, timezone

from devcake.domain.model import Activity, ActivityEntry, AttachmentRef
from devcake.domain.orchestrator import dispatch
from test_mapper import MapPMO, run_coro

NOW = datetime.now(timezone.utc)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, data in members.items():
            z.writestr(path, data)
    return buf.getvalue()


def test_safe_activity_relpath_rejects_slip():
    assert dispatch.safe_activity_relpath("a/b.md") == "a/b.md"
    assert dispatch.safe_activity_relpath("../evil") is None
    assert dispatch.safe_activity_relpath("/abs") is None
    assert dispatch.safe_activity_relpath("a/../../x") is None
    assert dispatch.safe_activity_relpath("") is None
    assert dispatch.safe_activity_relpath("..") is None
    assert dispatch.safe_activity_relpath("ok.md") == "ok.md"


def test_expand_zip_attachment_happy_and_caps():
    data = _zip_bytes({"a.md": b"hello", "dir/b.txt": b"world"})
    out = dispatch.expand_zip_attachment("report.zip", data,
                                         max_bytes=10 * 1024 * 1024)
    paths = {p: c for p, c in out}
    assert paths["report/a.md"] == b"hello"
    assert paths["report/dir/b.txt"] == b"world"

    # zip-slip members dropped
    evil = _zip_bytes({"../evil.txt": b"x", "ok.txt": b"y"})
    out2 = dispatch.expand_zip_attachment("pack.zip", evil, max_bytes=1024)
    assert out2 == [("pack/ok.txt", b"y")]

    # corrupt
    assert dispatch.expand_zip_attachment("x.zip", b"not-a-zip",
                                          max_bytes=1024) == []

    # byte cap stops early (small first member fits, large second does not)
    big = _zip_bytes({"small.txt": b"ab", "big.txt": b"x" * 100})
    out3 = dispatch.expand_zip_attachment("c.zip", big, max_bytes=10)
    assert out3 == [("c/small.txt", b"ab")]

    # declared uncompressed size alone can trip the pre-read cap (zip bomb
    # defense: do not decompress a member that cannot fit the remaining budget)
    bomb = _zip_bytes({"huge.txt": b"x" * 50})
    out4 = dispatch.expand_zip_attachment("b.zip", bomb, max_bytes=10)
    assert out4 == []


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
    files = dispatch._activity_snapshot_files(payload)
    paths = {f["path"] for f in files}
    assert "T-1-deliverable/REPORT.md" in paths
    assert "T-1-deliverable.zip" in paths
    # must NOT collapse nested to basename-only
    assert "REPORT.md" not in paths or "T-1-deliverable/REPORT.md" in paths
