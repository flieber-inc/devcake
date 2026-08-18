"""ADR-0030 Decision 3: POST /api/v1/missions — operator mission
transcription. Resurrects the PR-#14-deleted seam's coverage and adds the
v1 extensions: required instance, boundary validation for attachments,
redaction before the port call, adoption-label plumbing (never the
family-gate label), partial-failure honesty, and the Linear-visibility
feed post."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from devcake.api import mission_actions
from devcake.api.mission_actions import create_mission
from devcake.domain.model import MissionRef, PRIORITY_RANK



def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


class CreatePMO:
    """Records the full create/upload/feed surface; fails on demand."""

    def __init__(self, *, upload_fails: set[str] | None = None,
                 get_raises: bool = False, feed_raises: bool = False):
        self.created: list[tuple] = []
        self.uploads: list[tuple[str, str, bytes]] = []
        self.feeds: list[tuple[MissionRef, str]] = []
        self._upload_fails = upload_fails or set()
        self._get_raises = get_raises
        self._feed_raises = feed_raises

    def capabilities(self):
        return SimpleNamespace(
            attachment_max_bytes=getattr(self, "attachment_max_bytes", 1024),
            attachments_supported=getattr(self, "attachments_supported", True),
        )

    async def create_mission(self, team_ref, title, description, priority,
                             label_names, parent_ref=None):
        if priority not in PRIORITY_RANK:
            raise ValueError(f"illegal priority {priority!r}")
        self.created.append((team_ref, title, description, priority,
                             set(label_names)))
        return "DEV-9", "pmo-new"

    async def get(self, ref):
        if self._get_raises:
            raise RuntimeError("gone")
        return SimpleNamespace(url="https://pmo/DEV-9")

    async def upload_attachment(self, pmo_id, name, data):
        if name in self._upload_fails:
            raise RuntimeError(f"upload of {name} exploded")
        self.uploads.append((pmo_id, name, data))
        return f"https://assets/{name}"

    async def post_feed(self, ref, markdown):
        if self._feed_raises:
            raise RuntimeError("feed down")
        self.feeds.append((ref, markdown))


def _mgr(pmo=None, team_key="devcake-pmo/missions"):
    m = SimpleNamespace(instance=SimpleNamespace(name="board",
                                                 team_key=team_key),
                        instance_name="board",
                        pmo=pmo if pmo is not None else CreatePMO(),
                        audits=[])
    m._audit = lambda pmo_id, action, detail="": m.audits.append(
        (pmo_id, action, detail))
    return m


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _create(**over):
    kw = dict(instance="board", title="Ship it", description="body",
              priority="medium", adopt=True, attachments=None,
              managers=over.pop("managers", {"board": _mgr()}),
              adoption_mode="opt_in")
    kw.update(over)
    return run_coro(create_mission(**kw))


# ── boundary validation ──────────────────────────────────────────────────────

def test_blank_title_422():
    with pytest.raises(HTTPException) as e:
        _create(title="   ")
    assert e.value.status_code == 422


def test_unknown_priority_422():
    with pytest.raises(HTTPException) as e:
        _create(priority="asap")
    assert e.value.status_code == 422


def test_unknown_instance_404():
    with pytest.raises(HTTPException) as e:
        _create(instance="nope")
    assert e.value.status_code == 404


def test_unconfigured_instance_409():
    with pytest.raises(HTTPException) as e:
        _create(managers={"board": _mgr(team_key="")})
    assert e.value.status_code == 409


def test_attachment_name_and_b64_and_size_and_count_422():
    for atts in (
        [{"name": "../evil.md", "content_b64": _b64(b"x")}],
        [{"name": "ok.md", "content_b64": "not-base64!!"}],
        [{"name": "big.bin", "content_b64": _b64(b"x" * 2048)}],  # cap 1024
        [{"name": f"f{i}.md", "content_b64": _b64(b"x")} for i in range(11)],
    ):
        with pytest.raises(HTTPException) as e:
            _create(attachments=atts)
        assert e.value.status_code == 422, atts[0]["name"]


def test_attachments_unsupported_422s_before_zero_byte_cap():
    pmo = CreatePMO()
    pmo.attachment_max_bytes = 0
    pmo.attachments_supported = False
    with pytest.raises(HTTPException) as e:
        _create(managers={"board": _mgr(pmo)},
                attachments=[{"name": "brief.md", "content_b64": _b64(b"hi")}])
    assert e.value.status_code == 422
    assert "does not support" in e.value.detail
    assert pmo.created == []


# ── the write-through ────────────────────────────────────────────────────────

def test_create_passes_devcake_label_only_when_adopting(monkeypatch):
    pmo = CreatePMO()
    out = _create(managers={"board": _mgr(pmo)})
    team, title, desc, prio, labels = pmo.created[0]
    assert team == "devcake-pmo/missions"
    assert labels == {"DEVCAKE"}          # never DEVCAKE-CREATED (family gate)
    assert out["adopted"] is True and out["key"] == "DEV-9"
    assert out["url"] == "https://pmo/DEV-9"


def test_opt_in_without_adopt_sends_no_label_and_says_so():
    pmo = CreatePMO()
    out = _create(adopt=False, managers={"board": _mgr(pmo)})
    assert pmo.created[0][4] == set()
    assert out["adopted"] is False        # the dialog copy owns the truth


def test_opt_out_ignores_the_toggle():
    pmo = CreatePMO()
    out = _create(adopt=False, adoption_mode="opt_out",
                  managers={"board": _mgr(pmo)})
    assert pmo.created[0][4] == set()
    assert out["adopted"] is True         # everything is adopted in opt_out


def test_title_and_description_are_redacted_before_the_port(monkeypatch):
    seen = []

    def marker(text):
        seen.append(text)
        return f"[R]{text}"
    monkeypatch.setattr(mission_actions, "redact", marker)
    pmo = CreatePMO()
    _create(title="secret title", description="secret body",
            managers={"board": _mgr(pmo)})
    _, title, desc, _, _ = pmo.created[0]
    assert title == "[R]secret title" and desc == "[R]secret body"
    assert "secret title" in seen and "secret body" in seen


def test_url_lookup_failure_never_fails_the_create():
    pmo = CreatePMO(get_raises=True)
    out = _create(managers={"board": _mgr(pmo)})
    assert out["url"] == "" and out["key"] == "DEV-9"


# ── attachments: uploads, index post, partial-failure honesty ───────────────

def test_attachments_upload_and_index_post():
    pmo = CreatePMO()
    out = _create(attachments=[
        {"name": "spec.md", "content_b64": _b64(b"# spec")},
        {"name": "notes.txt", "content_b64": _b64(b"n")},
    ], managers={"board": _mgr(pmo)})
    assert [(n, d) for _, n, d in pmo.uploads] == [
        ("spec.md", b"# spec"), ("notes.txt", b"n")]
    # Linear-visibility rule: one sentinel-free feed post links every upload
    assert len(pmo.feeds) == 1
    ref, body = pmo.feeds[0]
    assert ref == MissionRef("pmo-new", "issue")
    assert "[spec.md](https://assets/spec.md)" in body
    assert "devcake:v1" not in body       # operator-authored = HUMAN
    assert out["feed_posted"] is True and out["attachment_failures"] == []


def test_partial_attachment_failure_is_disclosed_not_rolled_back():
    pmo = CreatePMO(upload_fails={"bad.pdf"})
    out = _create(attachments=[
        {"name": "ok.md", "content_b64": _b64(b"ok")},
        {"name": "bad.pdf", "content_b64": _b64(b"x")},
    ], managers={"board": _mgr(pmo)})
    # 200-with-warnings: the mission EXISTS; the failure is named
    assert [f["name"] for f in out["attachment_failures"]] == ["bad.pdf"]
    assert [n for _, n, _ in pmo.uploads] == ["ok.md"]
    assert out["feed_posted"] is True     # the successful upload is indexed
    assert out["key"] == "DEV-9"


def test_failed_index_post_is_a_warning_not_an_error():
    pmo = CreatePMO(feed_raises=True)
    out = _create(attachments=[{"name": "a.md", "content_b64": _b64(b"a")}],
                  managers={"board": _mgr(pmo)})
    assert out["feed_posted"] is False and out["attachment_failures"] == []


def test_no_uploads_means_no_index_post():
    pmo = CreatePMO()
    _create(managers={"board": _mgr(pmo)})
    assert pmo.feeds == []
