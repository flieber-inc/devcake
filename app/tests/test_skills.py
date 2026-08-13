"""Skill store v1 (docs/16): DevType.skills schema + the SkillService
(domain/skills.py) — builtin scan, frontmatter, payload assembly with
store-first reads, builtin fallback, and size caps."""

import asyncio

import pytest

from devcake.config import DevType
from devcake.domain.orchestrator import dispatch


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _dt(**kw):
    return DevType(name="senior-dev", harness_template="claude-code", **kw)


def test_devtype_skills_default_empty():
    assert _dt().skills == []
    assert _dt().skills_required == []


def test_devtype_skills_accepts_valid_names_and_dedupes():
    dt = _dt(skills=["tdd", "pr-hygiene", "tdd", "write_spec2"])
    assert dt.skills == ["tdd", "pr-hygiene", "write_spec2"]


@pytest.mark.parametrize("bad", [
    "", "UPPER", "has space", "-leading-hyphen", "a" * 65, "dot.name",
    # external `<card>/<skill>` shape violations (ADR-0016 addendum): one
    # slash max, prefix in repo-card shape (starts alpha, no hyphens, ≤12)
    "a/b/c", "SL/ash", "1x/skill", "has-hyphen/skill", "toolongcardxx/skill",
    "/ash", "sl/",
])
def test_devtype_skills_rejects_bad_names(bad):
    with pytest.raises(ValueError):
        _dt(skills=[bad])


def test_devtype_external_skill_names_and_basename_collisions():
    """`<card>/<skill>` selects from an external repo card (ADR-0016
    addendum). Basenames must be unique across the selection — the payload
    flattens external paths to the basename dir."""
    dt = _dt(skills=["sl/ash", "tdd"], skills_required=["sl/ash"])
    assert dt.skills == ["sl/ash", "tdd"]
    with pytest.raises(ValueError, match="share the install dir"):
        _dt(skills=["tdd", "myrepo/tdd"])
    with pytest.raises(ValueError, match="share the install dir"):
        _dt(skills=["repoa/tdd", "repob/tdd"])


def test_devtype_skills_required_subset_of_skills():
    dt = _dt(skills=["tdd", "pr-hygiene"], skills_required=["pr-hygiene", "tdd"])
    assert dt.skills_required == ["pr-hygiene", "tdd"]


def test_devtype_skills_required_rejects_not_in_skills():
    with pytest.raises(ValueError, match="skills_required"):
        _dt(skills=["tdd"], skills_required=["pr-hygiene"])


def test_devtype_skills_required_dedupes_and_validates_names():
    dt = _dt(skills=["tdd", "pr-hygiene"],
             skills_required=["tdd", "tdd", "pr-hygiene"])
    assert dt.skills_required == ["tdd", "pr-hygiene"]
    with pytest.raises(ValueError):
        _dt(skills=["tdd"], skills_required=["UPPER"])


def test_required_skills_block_empty_when_none():
    from devcake.domain.orchestrator.dispatch import append_required_skills
    assert append_required_skills("base prompt", [], []) == "base prompt"
    assert append_required_skills(
        "base prompt", ["tdd"], [{"name": "other", "files": []}]) == "base prompt"


def test_required_skills_block_lists_shipped_required_only():
    from devcake.domain.orchestrator.dispatch import append_required_skills
    out = append_required_skills(
        "base prompt",
        ["tdd", "pr-hygiene", "missing"],
        [{"name": "pr-hygiene", "files": []}, {"name": "tdd", "files": []}],
    )
    assert out.startswith("base prompt\n\n### Required skills\n")
    assert "`tdd`" in out and "`pr-hygiene`" in out
    assert "missing" not in out
    # order follows skills_required, not payload order
    assert out.index("`tdd`") < out.index("`pr-hygiene`")


# ── frontmatter (lenient: broken YAML must never raise) ──────────────────────

def test_parse_frontmatter_good():
    from devcake.domain.skills import parse_frontmatter
    text = "---\nname: tdd\ndescription: Test-first discipline\n---\n\n# TDD\n"
    fm = parse_frontmatter(text)
    assert fm == {"name": "tdd", "description": "Test-first discipline"}


@pytest.mark.parametrize("text", [
    "# No frontmatter at all\n",
    "---\nname: [unclosed\n---\nbody",          # broken YAML
    "---\n- just\n- a list\n---\nbody",          # YAML but not a mapping
    "---\nnever closed",                          # no closing fence
    "",
])
def test_parse_frontmatter_lenient(text):
    from devcake.domain.skills import parse_frontmatter
    assert isinstance(parse_frontmatter(text), dict)


# ── builtin scan (no forge → bundled skills serve everything) ────────────────

def _builtin_tree(tmp_path):
    d = tmp_path / "skills"
    (d / "tdd").mkdir(parents=True)
    (d / "tdd" / "SKILL.md").write_text(
        "---\nname: tdd\ndescription: Test-first discipline\n---\n\n# TDD\n")
    (d / "tdd" / "reference.md").write_text("extra material\n")
    (d / "pr-hygiene").mkdir()
    (d / "pr-hygiene" / "SKILL.md").write_text(
        "---\nname: pr-hygiene\ndescription: Commit and PR scope discipline\n---\n")
    (d / "README.md").write_text("store readme — not a skill\n")
    (d / "empty-dir").mkdir()                     # no SKILL.md → not a skill
    return d


def test_builtin_seed_lists_every_file_b64(tmp_path):
    import base64
    from devcake.domain.skills import SkillService
    svc = SkillService(builtin_dir=_builtin_tree(tmp_path))
    seed = svc.builtin_seed()
    paths = {f["path"] for f in seed}
    assert paths == {"tdd/SKILL.md", "tdd/reference.md",
                     "pr-hygiene/SKILL.md", "README.md"}
    tdd = next(f for f in seed if f["path"] == "tdd/SKILL.md")
    assert b"Test-first" in base64.b64decode(tdd["content_b64"])


def test_get_skill_builtin_returns_files_and_description(tmp_path):
    from devcake.domain.skills import SkillService
    svc = SkillService(builtin_dir=_builtin_tree(tmp_path))
    detail = _run(svc.get_skill("tdd"))
    assert detail["name"] == "tdd"
    assert detail["description"] == "Test-first discipline"
    assert detail["source"] == "builtin" and detail["builtin"] is True
    paths = {f["path"] for f in detail["files"]}
    assert paths == {"SKILL.md", "reference.md"}
    skill_md = next(f for f in detail["files"] if f["path"] == "SKILL.md")
    assert "# TDD" in skill_md["content"]


def test_get_skill_missing_and_bad_name(tmp_path):
    from devcake.domain.skills import SkillService, SkillStoreError
    svc = SkillService(builtin_dir=_builtin_tree(tmp_path))
    with pytest.raises(SkillStoreError) as e:
        _run(svc.get_skill("nope"))
    assert e.value.status == 404
    with pytest.raises(SkillStoreError) as e:
        _run(svc.get_skill("UPPER"))
    assert e.value.status == 422


def test_list_skills_builtin_fallback_without_forge(tmp_path):
    from devcake.domain.skills import SkillService
    svc = SkillService(builtin_dir=_builtin_tree(tmp_path))
    skills, store = _run(svc.list_skills())
    assert [(s.name, s.source, s.files) for s in skills] == [
        ("pr-hygiene", "builtin", 1), ("tdd", "builtin", 2)]
    assert skills[1].description == "Test-first discipline"
    assert all(s.builtin for s in skills)
    assert store["enabled"] is False and store["ok"] is False


# ── store reads (fake forge implementing the InternalForgePort additions) ────

class _FakeForge:
    def __init__(self, files: dict[str, bytes] | None = None, fail: bool = False):
        self.files = dict(files or {})
        self.fail = fail
        self.tree_calls = 0
        self.file_calls: list[str] = []

    async def skill_store_tree(self):
        if self.fail:
            raise RuntimeError("gitea down")
        self.tree_calls += 1
        return [{"path": p, "size": len(b)} for p, b in self.files.items()]

    async def skill_store_file(self, path):
        if self.fail:
            raise RuntimeError("gitea down")
        self.file_calls.append(path)
        return self.files[path]

    def skill_store_url(self):
        return "http://localhost:3300/devcake-repos/skill-store"


def test_list_skills_from_store(tmp_path):
    from devcake.domain.skills import SkillService
    forge = _FakeForge({
        "README.md": b"store readme",
        "tdd/SKILL.md": b"---\nname: tdd\ndescription: Store copy\n---\n",
        "tdd/reference.md": b"ref",
        "custom/SKILL.md": b"---\nname: custom\ndescription: Operator skill\n---\n",
        "junk/notes.txt": b"dir without SKILL.md is not a skill",
    })
    svc = SkillService(internal_forge=forge, builtin_dir=_builtin_tree(tmp_path))
    skills, store = _run(svc.list_skills())
    assert [(s.name, s.source, s.files) for s in skills] == [
        ("custom", "store", 1), ("tdd", "store", 2)]
    assert skills[1].description == "Store copy"
    # builtin marks skills shipped in the image (they re-seed at boot, so
    # the UI must offer delete only for the others)
    assert [(s.name, s.builtin) for s in skills] == [
        ("custom", False), ("tdd", True)]
    assert store == {"enabled": True, "ok": True, "detail": "",
                     "html_url": forge.skill_store_url()}


def test_list_skills_store_down_serves_builtin(tmp_path):
    from devcake.domain.skills import SkillService
    svc = SkillService(internal_forge=_FakeForge(fail=True),
                       builtin_dir=_builtin_tree(tmp_path))
    skills, store = _run(svc.list_skills())
    assert {s.source for s in skills} == {"builtin"}
    assert store["enabled"] is True and store["ok"] is False
    assert "gitea down" in store["detail"]


# ── payload_for (dispatch attach) ────────────────────────────────────────────

def _b64(payload, name, path):
    import base64
    entry = next(s for s in payload if s["name"] == name)
    f = next(f for f in entry["files"] if f["path"] == path)
    return base64.b64decode(f["content_b64"])


def test_payload_store_first_over_builtin(tmp_path):
    from devcake.domain.skills import SkillService
    forge = _FakeForge({"tdd/SKILL.md": b"---\nname: tdd\n---\nSTORE EDIT\n"})
    svc = SkillService(internal_forge=forge, builtin_dir=_builtin_tree(tmp_path))
    payload, warnings = _run(svc.payload_for(["tdd"]))
    assert warnings == []
    assert _b64(payload, "tdd", "tdd/SKILL.md") == b"---\nname: tdd\n---\nSTORE EDIT\n"


def test_payload_builtin_fallback_when_store_down(tmp_path):
    from devcake.domain.skills import SkillService
    svc = SkillService(internal_forge=_FakeForge(fail=True),
                       builtin_dir=_builtin_tree(tmp_path))
    payload, warnings = _run(svc.payload_for(["tdd"]))
    assert b"Test-first" in _b64(payload, "tdd", "tdd/SKILL.md")
    assert {f["path"] for f in payload[0]["files"]} == {
        "tdd/SKILL.md", "tdd/reference.md"}
    assert any("unreachable" in w for w in warnings)


def test_payload_missing_everywhere_warns_and_skips(tmp_path):
    from devcake.domain.skills import SkillService
    svc = SkillService(builtin_dir=_builtin_tree(tmp_path))
    payload, warnings = _run(svc.payload_for(["tdd", "nope"]))
    assert [s["name"] for s in payload] == ["tdd"]
    assert any("nope" in w for w in warnings)


def test_payload_oversized_file_skipped_without_fetching(tmp_path):
    from devcake.domain import skills as skills_mod
    forge = _FakeForge({
        "big/SKILL.md": b"---\nname: big\n---\n",
        "big/blob.bin": b"x" * (skills_mod.MAX_FILE_BYTES + 1),
    })
    svc = skills_mod.SkillService(internal_forge=forge,
                                  builtin_dir=tmp_path / "none")
    payload, warnings = _run(svc.payload_for(["big"]))
    assert {f["path"] for f in payload[0]["files"]} == {"big/SKILL.md"}
    assert any("blob.bin" in w for w in warnings)
    # the cap must be enforced from the tree's size field BEFORE download —
    # an oversized store file must never be pulled into app memory
    assert forge.file_calls == ["big/SKILL.md"]


# ── authoring: compose / import-validate / save / delete (admin UI flow) ─────

def _fake_writable_forge(files=None):
    """_FakeForge + the write/delete surface save_skill/delete_skill use."""
    forge = _FakeForge(files)
    forge.writes, forge.deletes = [], []

    async def write_skill_files(fs, message):
        import base64 as b
        forge.writes.append((fs, message))
        for f in fs:
            forge.files[f["path"]] = b.b64decode(f["content_b64"])

    async def delete_skill_paths(paths, message):
        forge.deletes.append((paths, message))
        for p in paths:
            forge.files.pop(p, None)

    forge.write_skill_files = write_skill_files
    forge.delete_skill_paths = delete_skill_paths
    return forge


def test_compose_skill_builds_parseable_frontmatter(tmp_path):
    from devcake.domain.skills import SkillService, parse_frontmatter
    import base64
    svc = SkillService(builtin_dir=tmp_path / "none")
    files = svc.compose_skill("my-skill", "Reviews SQL migrations: use when editing schema files.",
                              "# SQL reviews\n\nAlways check for locks.")
    assert [f["path"] for f in files] == ["SKILL.md"]
    text = base64.b64decode(files[0]["content_b64"]).decode()
    fm = parse_frontmatter(text)
    assert fm["name"] == "my-skill"
    assert fm["description"].startswith("Reviews SQL migrations")
    assert "Always check for locks." in text


def test_validate_import_extracts_name_and_checks(tmp_path):
    from devcake.domain.skills import SkillService, SkillStoreError
    import base64
    import pytest as _pytest
    svc = SkillService(builtin_dir=tmp_path / "none")
    good = [{"path": "SKILL.md", "content_b64": base64.b64encode(
        b"---\nname: imported\ndescription: An imported skill\n---\nbody").decode()},
        {"path": "refs/extra.md", "content_b64": base64.b64encode(b"x").decode()}]
    assert svc.validate_import(good) == "imported"

    def _err(files, fragment):
        with _pytest.raises(SkillStoreError) as e:
            svc.validate_import(files)
        assert fragment in str(e.value)

    _err([{"path": "notes.md", "content_b64": "eA=="}], "SKILL.md")
    _err([{"path": "SKILL.md", "content_b64": base64.b64encode(
        b"---\nname: BAD NAME\ndescription: d\n---\n").decode()}], "name")
    _err([{"path": "SKILL.md", "content_b64": base64.b64encode(
        b"---\nname: ok-name\n---\n").decode()}], "description")
    _err([{"path": "SKILL.md", "content_b64": "!!!"}], "base64")


def test_save_skill_writes_prefixed_and_guards_collisions(tmp_path):
    from devcake.domain.skills import SkillService, SkillStoreError
    import pytest as _pytest
    forge = _fake_writable_forge({"existing/SKILL.md": b"---\nname: existing\n---\n"})
    svc = SkillService(internal_forge=forge, builtin_dir=_builtin_tree(tmp_path))
    files = svc.compose_skill("fresh", "A new skill", "body")
    _run(svc.save_skill("fresh", files))
    written, message = forge.writes[-1]
    assert [f["path"] for f in written] == ["fresh/SKILL.md"]
    assert "fresh" in message
    # collision guards: existing store skill AND builtin names need overwrite
    with _pytest.raises(SkillStoreError) as e:
        _run(svc.save_skill("existing", files))
    assert e.value.status == 409
    with _pytest.raises(SkillStoreError) as e:
        _run(svc.save_skill("tdd", files))          # tdd is builtin
    assert e.value.status == 409
    _run(svc.save_skill("existing", svc.compose_skill("existing", "d", "b"),
                        overwrite=True))            # explicit overwrite is fine
    # unsafe paths refused
    with _pytest.raises(SkillStoreError) as e:
        _run(svc.save_skill("fresh2", [{"path": "../evil.md", "content_b64": "eA=="}]))
    assert e.value.status == 422
    # a skill payload must contain SKILL.md
    with _pytest.raises(SkillStoreError):
        _run(svc.save_skill("fresh3", [{"path": "notes.md", "content_b64": "eA=="}]))


def test_overwrite_removes_dropped_files(tmp_path):
    """A shrinking overwrite must delete files the new version dropped —
    otherwise the orphan keeps being installed into every future run."""
    from devcake.domain.skills import SkillService
    forge = _fake_writable_forge({
        "foo/SKILL.md": b"---\nname: foo\ndescription: v1\n---\n",
        "foo/refs/help.md": b"old help",
        "foo/scripts/run.py": b"old script"})
    svc = SkillService(internal_forge=forge, builtin_dir=tmp_path / "none")
    # v2 keeps SKILL.md + run.py, drops refs/help.md
    v2 = svc.compose_skill("foo", "v2", "body") + [
        {"path": "scripts/run.py", "content_b64": "eA=="}]
    _run(svc.save_skill("foo", v2, overwrite=True))
    remaining = {p for p in forge.files if p.startswith("foo/")}
    assert remaining == {"foo/SKILL.md", "foo/scripts/run.py"}
    assert forge.deletes and "foo/refs/help.md" in forge.deletes[-1][0]
    # a fresh install (SKILL.md-only) leaves no orphans either
    _run(svc.save_skill("foo", svc.compose_skill("foo", "v3", "b"), overwrite=True))
    assert {p for p in forge.files if p.startswith("foo/")} == {"foo/SKILL.md"}


def test_save_skill_wraps_forge_errors_as_502(tmp_path):
    from devcake.domain.skills import SkillService, SkillStoreError
    import pytest as _pytest
    forge = _fake_writable_forge()

    async def boom(fs, message):
        raise RuntimeError("gitea 5xx")
    forge.write_skill_files = boom
    svc = SkillService(internal_forge=forge, builtin_dir=tmp_path / "none")
    with _pytest.raises(SkillStoreError) as e:
        _run(svc.save_skill("x", svc.compose_skill("x", "d", "b")))
    assert e.value.status == 502 and "gitea 5xx" in str(e.value)


def test_delete_skill_wraps_forge_errors_as_502(tmp_path):
    from devcake.domain.skills import SkillService, SkillStoreError
    import pytest as _pytest
    forge = _fake_writable_forge({"c/SKILL.md": b"---\nname: c\n---\n"})

    async def boom(paths, message):
        raise RuntimeError("gitea 5xx")
    forge.delete_skill_paths = boom
    svc = SkillService(internal_forge=forge, builtin_dir=tmp_path / "none")
    with _pytest.raises(SkillStoreError) as e:
        _run(svc.delete_skill("c"))
    assert e.value.status == 502


def test_save_skill_dedupes_files_by_path(tmp_path):
    from devcake.domain.skills import SkillService
    forge = _fake_writable_forge()
    svc = SkillService(internal_forge=forge, builtin_dir=tmp_path / "none")
    dupes = [{"path": "SKILL.md", "content_b64": "QQ=="},
             {"path": "SKILL.md", "content_b64": "Qg=="}]
    _run(svc.save_skill("d", dupes))
    written = [f["path"] for f in forge.writes[-1][0]]
    assert written == ["d/SKILL.md"]           # last wins, no duplicate op


def test_save_skill_requires_store_and_invalidates_cache(tmp_path):
    from devcake.domain.skills import SkillService, SkillStoreError
    import pytest as _pytest
    svc = SkillService(builtin_dir=tmp_path / "none")   # no forge
    with _pytest.raises(SkillStoreError) as e:
        _run(svc.save_skill("x", svc.compose_skill("x", "d", "b")))
    assert e.value.status == 503

    forge = _fake_writable_forge()
    svc2 = SkillService(internal_forge=forge, builtin_dir=tmp_path / "none")
    assert _run(svc2.list_skills())[0] == []            # warms the cache
    _run(svc2.save_skill("x", svc2.compose_skill("x", "described", "b")))
    names = [s.name for s in _run(svc2.list_skills())[0]]
    assert names == ["x"]                               # cache was invalidated


def test_delete_skill_guards_and_deletes(tmp_path):
    from devcake.domain.skills import SkillService, SkillStoreError
    import pytest as _pytest
    forge = _fake_writable_forge({
        "custom/SKILL.md": b"---\nname: custom\ndescription: d\n---\n",
        "custom/refs/x.md": b"x",
        "tdd/SKILL.md": b"store copy of a builtin"})
    svc = SkillService(internal_forge=forge, builtin_dir=_builtin_tree(tmp_path))
    with _pytest.raises(SkillStoreError) as e:
        _run(svc.delete_skill("tdd"))                   # builtin: re-seeds at boot
    assert e.value.status == 422
    with _pytest.raises(SkillStoreError) as e:
        _run(svc.delete_skill("nope"))
    assert e.value.status == 404
    _run(svc.delete_skill("custom"))
    paths, _msg = forge.deletes[-1]
    assert sorted(paths) == ["custom/SKILL.md", "custom/refs/x.md"]
    assert [s.name for s in _run(svc.list_skills())[0] if s.name == "custom"] == []


# ── dispatch attach (_skill_payload; registry-driven skills_dir) ─────────────

class _FakeSkillService:
    def __init__(self):
        self.calls = []

    async def payload_for(self, names):
        self.calls.append(list(names))
        return ([{"name": n, "files": [{"path": f"{n}/SKILL.md",
                                        "content_b64": "eA=="}]}
                 for n in names],
                [f"warned about {names[0]}"])


def test_skill_payload_attaches_for_claude_harness():
    from fakes import make_mission_manager
    mgr = make_mission_manager(skills=_FakeSkillService())
    dt = DevType(name="senior-dev", harness_template="claude-code",
                 skills=["tdd", "pr-hygiene"])
    payload = _run(dispatch._skill_payload(mgr, dt))
    assert [s["name"] for s in payload] == ["tdd", "pr-hygiene"]
    assert mgr.skills.calls == [["tdd", "pr-hygiene"]]


@pytest.mark.parametrize("harness", ["grok-build", "codex"])
def test_skill_payload_attaches_for_grok_and_codex(harness):
    """grok 0.2.103 and codex 0.144.4 read ~/.agents/skills natively — the
    registry declares a skills_dir for them, so delivery is no longer
    claude-code-only."""
    from fakes import make_mission_manager
    mgr = make_mission_manager(skills=_FakeSkillService())
    dt = DevType(name="main-dev", harness_template=harness, skills=["tdd"])
    payload = _run(dispatch._skill_payload(mgr, dt))
    assert [s["name"] for s in payload] == ["tdd"]
    assert mgr.skills.calls == [["tdd"]]


def test_skill_payload_skipped_when_harness_has_no_skills_dir(monkeypatch):
    """The warn+skip gate is registry-driven: a harness whose entry declares
    no skills_dir never asks the service — never a refused run.

    Patch the HARNESSES reference the gate actually reads (dispatch's module
    global): test_harness.py reloads devcake.harness, after which
    devcake.harness.HARNESSES is a different object from dispatch's."""
    from devcake.domain.orchestrator import dispatch as dispatch_mod
    from fakes import make_mission_manager
    monkeypatch.setattr(dispatch_mod.HARNESSES["grok-build"],
                        "skills_dir", None)
    mgr = make_mission_manager(skills=_FakeSkillService())
    dt = DevType(name="main-dev", harness_template="grok-build", skills=["tdd"])
    assert _run(dispatch._skill_payload(mgr, dt)) == []
    assert mgr.skills.calls == []          # never even asked


def test_skill_payload_empty_without_service_or_selection():
    from fakes import make_mission_manager
    mgr = make_mission_manager()           # skills service not wired
    dt = DevType(name="senior-dev", harness_template="claude-code",
                 skills=["tdd"])
    assert _run(dispatch._skill_payload(mgr, dt)) == []
    mgr2 = make_mission_manager(skills=_FakeSkillService())
    dt2 = DevType(name="senior-dev", harness_template="claude-code")
    assert _run(dispatch._skill_payload(mgr2, dt2)) == []


def test_runspec_reply_carries_skills(tmp_path):
    from devcake.adapters.files import RunStore
    from devcake.domain.run import Run
    from devcake.domain.runs import RunManager

    replies = []

    class FakeMessaging:
        async def reply(self, run_id, kind, payload):
            replies.append((kind, payload))

        async def delete_runspec_result(self, rid):
            pass

    store = RunStore(tmp_path / "runs")
    manager = RunManager(store, FakeMessaging(), executor=None)
    run = Run(run_id="HELLO-1-1-HELLO-AAAAAA", mission_key="HELLO",
              mission_type="HELLO", dev_type="hello-stub", seq=1)
    run.spec_skills = [{"name": "tdd", "files": [
        {"path": "tdd/SKILL.md", "content_b64": "eA=="}]}]
    run.spec_skills_dir = ".agents/skills"
    run.state = "dispatched"
    store.save(run)
    _run(manager.handle(run.run_id, "runspec.get", {}))
    kind, payload = replies[-1]
    assert kind == "runspec.result"
    assert payload["skills"] == run.spec_skills
    assert payload["skills_dir"] == ".agents/skills"

    # legacy Run persisted before the field existed → "" → the entrypoint
    # falls back to its .claude/skills default (old-behavior compat)
    run2 = Run(run_id="HELLO-1-2-HELLO-AAAAAA", mission_key="HELLO",
               mission_type="HELLO", dev_type="hello-stub", seq=2)
    run2.state = "dispatched"
    store.save(run2)
    _run(manager.handle(run2.run_id, "runspec.get", {}))
    assert replies[-1][1]["skills_dir"] == ""


def test_payload_total_cap_drops_later_files(tmp_path):
    from devcake.domain import skills as skills_mod
    chunk = skills_mod.MAX_FILE_BYTES - 1024      # under the per-file cap
    files = {f"a/{i:02d}.md": b"y" * chunk for i in range(5)}   # ~950 KB
    files["a/SKILL.md"] = b"---\nname: a\n---\n"
    files["b/SKILL.md"] = b"z" * chunk            # would cross MAX_TOTAL_BYTES
    files["c/SKILL.md"] = b"w" * chunk            # dropped without any fetch
    forge = _FakeForge(files)
    svc = skills_mod.SkillService(internal_forge=forge,
                                  builtin_dir=tmp_path / "none")
    payload, warnings = _run(svc.payload_for(["a", "b", "c"]))
    assert [s["name"] for s in payload] == ["a"]
    assert any("cap" in w for w in warnings)
    # once the budget is exhausted, later skills must not be fetched at all
    assert all(p.startswith("a/") for p in forge.file_calls)


def test_payload_and_listing_reuse_cached_store_reads(tmp_path):
    """Dispatch sweeps call payload_for per mission — within the cache TTL
    the store tree and file bytes must be fetched once, not per dispatch."""
    forge = _FakeForge({"tdd/SKILL.md": b"---\nname: tdd\n---\nbody\n"})
    from devcake.domain.skills import SkillService
    svc = SkillService(internal_forge=forge, builtin_dir=tmp_path / "none")
    _run(svc.payload_for(["tdd"]))
    _run(svc.payload_for(["tdd"]))
    _run(svc.list_skills())
    assert forge.tree_calls == 1
    assert forge.file_calls == ["tdd/SKILL.md"]


def test_skill_payload_never_raises_out_of_dispatch():
    """Skills are additive: even a service that blows up (e.g. a bundled
    copy unreadable in a broken image — store errors are already swallowed
    inside payload_for) must not refuse the run; dispatch gets [] and a
    warning instead."""
    from fakes import make_mission_manager

    class _ExplodingService:
        async def payload_for(self, names):
            raise OSError("bundled skill unreadable")

    mgr = make_mission_manager(skills=_ExplodingService())
    dt = DevType(name="senior-dev", harness_template="claude-code",
                 skills=["tdd"])
    assert _run(dispatch._skill_payload(mgr, dt)) == []


def test_payload_ships_skill_md_first_under_mid_skill_cap(tmp_path):
    """When the total cap lands inside a skill, SKILL.md must ship and the
    supporting files drop — plain sorted() path order could ship an
    uppercase-named helper while dropping the manifest, leaving a dead
    skill dir the harness ignores."""
    from devcake.domain import skills as skills_mod
    chunk = skills_mod.MAX_FILE_BYTES - 1024
    files = {f"a/{i:02d}.md": b"y" * chunk for i in range(5)}    # ~995 KiB
    files["a/SKILL.md"] = b"---\nname: a\n---\n"
    files["b/AAA-REFERENCE.md"] = b"r" * chunk   # sorts before SKILL.md
    files["b/SKILL.md"] = b"---\nname: b\n---\n"
    forge = _FakeForge(files)
    svc = skills_mod.SkillService(internal_forge=forge,
                                  builtin_dir=tmp_path / "none")
    payload, warnings = _run(svc.payload_for(["a", "b"]))
    b_entry = next((s for s in payload if s["name"] == "b"), None)
    assert b_entry is not None, "skill b lost its manifest to the cap"
    assert [f["path"] for f in b_entry["files"]] == ["b/SKILL.md"]
    assert any("cap" in w for w in warnings)


# ── external skills: `<card>/<skill>` from the repo-card mirror ──────────────
# (ADR-0016 addendum: no second cache — the ADR-0024 mirror's read-side)

class _FakeMirror:
    """RepoCache read-side stub: fixed head, one tree per (card, subdir)."""

    def __init__(self, trees=None, head="abc123", files=None):
        self.trees = trees or {}     # (card, subdir) -> {skill: {rel: size}}
        self.head = head             # None = unreadable mirror
        self.files = files or {}     # (card, skill, rel) -> bytes
        self.reads: list[tuple] = []

    async def tree_head(self, name):
        return self.head

    async def read_skill_tree(self, name, subdir, sha):
        return self.trees.get((name, subdir), {})

    async def read_skill_file(self, name, subdir, sha, skill, rel):
        self.reads.append((name, skill, rel))
        return self.files.get((name, skill, rel))


def _ext_svc(tmp_path, *, subdir="", head="abc123"):
    from devcake.config import AppConfig, RepoInstance
    from devcake.domain.skills import SkillService
    card = RepoInstance(name="myrepo", url="https://gh.example/o/skills",
                        skills_subdir=subdir)
    cfg = AppConfig(pmos=[], repos=[card])
    mirror = _FakeMirror(head=head)
    return SkillService(builtin_dir=_builtin_tree(tmp_path),
                        repo_cache=mirror, config=cfg), mirror


def test_payload_external_flattens_to_the_basename_dir(tmp_path):
    svc, mirror = _ext_svc(tmp_path)
    mirror.trees[("myrepo", "")] = {"tdd": {"SKILL.md": 20, "notes.md": 10}}
    mirror.files[("myrepo", "tdd", "SKILL.md")] = b"---\nname: tdd\n---\nEXT\n"
    mirror.files[("myrepo", "tdd", "notes.md")] = b"notes"
    payload, warnings = _run(svc.payload_for(["myrepo/tdd"]))
    assert warnings == []
    assert [s["name"] for s in payload] == ["myrepo/tdd"]
    # paths flattened to the BASENAME dir — install_skills and harness
    # discovery stay byte-identical to a store skill named `tdd`
    assert {f["path"] for f in payload[0]["files"]} == {
        "tdd/SKILL.md", "tdd/notes.md"}
    assert _b64(payload, "myrepo/tdd", "tdd/SKILL.md").endswith(b"EXT\n")
    # SKILL.md is read FIRST (the _collect anchor rule)
    assert mirror.reads[0] == ("myrepo", "tdd", "SKILL.md")


def test_payload_external_never_falls_back_to_store_or_builtin(tmp_path):
    # builtin has `tdd`; the external namespace is DISJOINT — a missing
    # external skill warns + skips, it never serves someone else's `tdd`
    svc, mirror = _ext_svc(tmp_path)
    payload, warnings = _run(svc.payload_for(["myrepo/tdd"]))
    assert payload == []
    assert any("no tdd/SKILL.md" in w for w in warnings)


def test_payload_external_unconfigured_card_and_dead_mirror_warn(tmp_path):
    svc, mirror = _ext_svc(tmp_path)
    payload, warnings = _run(svc.payload_for(["ghost/tdd"]))
    assert payload == [] and any("not configured" in w for w in warnings)
    svc2, _m = _ext_svc(tmp_path / "b", head=None)
    payload, warnings = _run(svc2.payload_for(["myrepo/tdd"]))
    assert payload == [] and any("no readable head" in w for w in warnings)


def test_payload_external_caps_apply_before_reading(tmp_path):
    from devcake.domain import skills as skills_mod
    svc, mirror = _ext_svc(tmp_path)
    mirror.trees[("myrepo", "")] = {"tdd": {
        "SKILL.md": 20, "huge.bin": skills_mod.MAX_FILE_BYTES + 1}}
    mirror.files[("myrepo", "tdd", "SKILL.md")] = b"---\nname: tdd\n---\n"
    payload, warnings = _run(svc.payload_for(["myrepo/tdd"]))
    assert {f["path"] for f in payload[0]["files"]} == {"tdd/SKILL.md"}
    assert any("huge.bin" in w for w in warnings)
    assert ("myrepo", "tdd", "huge.bin") not in mirror.reads


def test_payload_external_respects_skills_subdir(tmp_path):
    svc, mirror = _ext_svc(tmp_path, subdir="skills")
    mirror.trees[("myrepo", "skills")] = {"tdd": {"SKILL.md": 5}}
    mirror.files[("myrepo", "tdd", "SKILL.md")] = b"x"
    payload, _w = _run(svc.payload_for(["myrepo/tdd"]))
    assert [s["name"] for s in payload] == ["myrepo/tdd"]


def test_get_skill_external_reads_the_mirror_and_stays_read_only(tmp_path):
    from devcake.domain.skills import SkillStoreError
    svc, mirror = _ext_svc(tmp_path)
    mirror.trees[("myrepo", "")] = {"tdd": {"SKILL.md": 30}}
    mirror.files[("myrepo", "tdd", "SKILL.md")] = (
        b"---\nname: tdd\ndescription: External copy\n---\nbody")
    got = _run(svc.get_skill("myrepo/tdd"))
    assert (got["source"], got["origin"], got["builtin"]) == (
        "external", "myrepo", False)
    assert got["description"] == "External copy"
    assert [f["path"] for f in got["files"]] == ["SKILL.md"]
    with pytest.raises(SkillStoreError) as e:
        _run(svc.get_skill("myrepo/none"))
    assert e.value.status == 404
    # read-only BY CONSTRUCTION: authoring names refuse the slash
    with pytest.raises(SkillStoreError):
        _run(svc.delete_skill("myrepo/tdd"))


def test_external_infos_lists_every_skill_in_referenced_cards(tmp_path):
    svc, mirror = _ext_svc(tmp_path)
    mirror.trees[("myrepo", "")] = {
        "tdd": {"SKILL.md": 5}, "review": {"SKILL.md": 5, "extra.md": 3}}
    mirror.files[("myrepo", "tdd", "SKILL.md")] = (
        b"---\ndescription: A\n---\n")
    mirror.files[("myrepo", "review", "SKILL.md")] = (
        b"---\ndescription: B\n---\n")
    infos = _run(svc.external_infos(["myrepo/tdd"]))
    assert [(i.name, i.source, i.origin, i.files) for i in infos] == [
        ("myrepo/review", "external", "myrepo", 2),
        ("myrepo/tdd", "external", "myrepo", 1)]
    assert _run(svc.external_infos(["flat-name"])) == []
