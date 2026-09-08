"""``devcake up --release [TAG]`` and ``devcake prune`` (ADR-0038 addendum):
the release checkout with its guards, the implied bake, the post-up image
tidy-up, and the prune verb's two owners (CLI: control plane + dangling;
baker: Dev images through the app's prune request)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_devcake_cli_up_down_status import _ensure_cli_importable, _fake_checkout


# ── a throwaway git repo with release tags ───────────────────────────────────

def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(root), text=True,
                          capture_output=True, check=True).stdout


def _head_tags(root: Path) -> set[str]:
    """Every tag at HEAD (a release commit also carries its cli-v tag)."""
    return set(_git(root, "tag", "--points-at", "HEAD").split())


def _release_repo(tmp_path: Path, *, cli_versions: dict[str, str]) -> Path:
    """Tags v0.1.0 and v0.2.0 (plus a cli-v9.9.9 that must be ignored); each
    release commit carries cli/devcake_cli/__init__.py with the version
    given, and a VERSION file naming the tag."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "cli" / "devcake_cli").mkdir(parents=True)
    for tag in ("v0.1.0", "v0.2.0"):
        (root / "cli" / "devcake_cli" / "__init__.py").write_text(
            f'__version__ = "{cli_versions[tag]}"\n')
        (root / "VERSION").write_text(tag + "\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", tag)
        _git(root, "tag", "-a", tag, "-m", tag)
    _git(root, "tag", "-a", "cli-v9.9.9", "-m", "cli")
    # a local "origin" so `git fetch --tags origin` works offline
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(root), str(origin)],
                   check=True, capture_output=True)
    _git(root, "remote", "add", "origin", str(origin))
    return root


def test_release_tags_are_v_semver_newest_first(tmp_path):
    _ensure_cli_importable()
    from devcake_cli import release
    root = _release_repo(tmp_path, cli_versions={"v0.1.0": "0.1.0", "v0.2.0": "0.1.0"})
    assert release.release_tags(root) == ["v0.2.0", "v0.1.0"]
    assert release.resolve_release_tag(root, "latest") == "v0.2.0"
    assert release.resolve_release_tag(root, "v0.1.0") == "v0.1.0"
    with pytest.raises(release.ReleaseRefused, match="not found"):
        release.resolve_release_tag(root, "v9.9.9")
    with pytest.raises(release.ReleaseRefused, match="not a release tag"):
        release.resolve_release_tag(root, "cli-v9.9.9")


def test_checkout_release_lands_detached_on_the_tag(tmp_path, capsys):
    _ensure_cli_importable()
    from devcake_cli import release
    root = _release_repo(tmp_path, cli_versions={"v0.1.0": "0.1.0", "v0.2.0": "0.1.0"})
    assert release.checkout_release(root, "v0.1.0") == "v0.1.0"
    assert "v0.1.0" in _head_tags(root)
    assert (root / "VERSION").read_text().strip() == "v0.1.0"
    assert "checked out release v0.1.0" in capsys.readouterr().out


def test_dirty_tree_refuses_before_touching_anything(tmp_path):
    _ensure_cli_importable()
    from devcake_cli import release
    root = _release_repo(tmp_path, cli_versions={"v0.1.0": "0.1.0", "v0.2.0": "0.1.0"})
    (root / "VERSION").write_text("edited\n")          # modified tracked file
    (root / "scratch.txt").write_text("untracked is fine\n")
    with pytest.raises(release.ReleaseRefused, match="modified tracked files"):
        release.checkout_release(root, "v0.1.0")
    assert "v0.2.0" in _head_tags(root)
    (root / "VERSION").write_text("v0.2.0\n")
    assert release.checkout_release(root, "v0.1.0") == "v0.1.0"   # untracked never blocks


def test_older_cli_than_the_release_ships_refuses_with_the_upgrade_command(tmp_path):
    _ensure_cli_importable()
    from devcake_cli import release
    root = _release_repo(tmp_path, cli_versions={"v0.1.0": "0.1.0", "v0.2.0": "9.9.9"})
    with pytest.raises(release.ReleaseRefused, match="uv tool upgrade devcake-cli"):
        release.checkout_release(root, "latest")
    assert "v0.2.0" in _head_tags(root)
    # read from the tag, never from the working tree
    assert release.release_cli_version(root, "v0.1.0") == "0.1.0"
    assert release.release_cli_version(root, "v0.2.0") == "9.9.9"
    # an equal or newer CLI passes
    release.check_cli_matches_release(root, "v0.2.0", running="9.9.9")
    release.check_cli_matches_release(root, "v0.2.0", running="10.0.0")


def test_editable_cli_inside_the_checkout_refuses(tmp_path):
    _ensure_cli_importable()
    from devcake_cli import release
    root = _release_repo(tmp_path, cli_versions={"v0.1.0": "0.1.0", "v0.2.0": "0.1.0"})
    with pytest.raises(release.ReleaseRefused, match="editable"):
        release.check_cli_not_editable_from(root, root / "cli" / "devcake_cli" / "release.py")
    release.check_cli_not_editable_from(root, tmp_path / "elsewhere" / "release.py")


def test_dry_run_resolves_a_named_tag_without_fetching_or_checking_out(tmp_path, capsys):
    _ensure_cli_importable()
    from devcake_cli import release
    root = _release_repo(tmp_path, cli_versions={"v0.1.0": "0.1.0", "v0.2.0": "0.1.0"})
    _git(root, "remote", "remove", "origin")            # a fetch would fail
    assert release.checkout_release(root, "v0.1.0", dry_run=True) == "v0.1.0"
    assert release.checkout_release(root, "latest", dry_run=True) == "latest"
    out = capsys.readouterr().out
    assert "would: git fetch --tags origin && git checkout v0.1.0" in out
    assert "v0.2.0" in _head_tags(root)


# ── up --release: order, implied bake all, tidy-up last ──────────────────────

def test_up_release_checks_out_first_bakes_the_control_plane_and_prunes_last(monkeypatch, tmp_path):
    _ensure_cli_importable()
    import devcake_cli.up as up_mod
    from devcake_cli import release as release_mod
    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))
    order = []
    monkeypatch.setattr(release_mod, "checkout_release",
                        lambda root, wanted, **kw: order.append(("release", wanted)) or "v0.2.0")
    for name in ("_bake", "_compose_up", "_health_gate", "_hello_smoke",
                 "_start_baker", "_prune_after_release"):
        monkeypatch.setattr(up_mod, name,
                            lambda *a, _n=name, **k: order.append((_n, getattr(a[1], "bake_targets", None) if len(a) > 1 else None)))
    opts = up_mod.UpOptions(release="latest")
    assert up_mod.run_up(opts, repo=tmp_path) == 0
    assert order[0] == ("release", "latest")
    assert [n for n, _ in order] == ["release", "_bake", "_start_baker", "_compose_up",
                                     "_health_gate", "_hello_smoke", "_prune_after_release"]
    # implied bake = the control plane (the default target list), never `all`
    assert dict(order)["_bake"] == []
    # an explicit control-plane --bake wins
    order.clear()
    assert up_mod.run_up(up_mod.UpOptions(release="v0.2.0", bake=True,
                                          bake_targets=["app"]), repo=tmp_path) == 0
    assert dict(order)["_bake"] == ["app"]


def test_up_refuses_to_bake_dev_images(monkeypatch, tmp_path, capsys):
    """ADR-0038 addendum: Dev images are the host baker's alone; `up` bakes
    the control plane only and refuses a harness target or `all` before
    touching anything — through the CLI and through UpOptions alike."""
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.up as up_mod
    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))
    for argv in (["up", "--dry-run", "--bake", "all"],
                 ["up", "--dry-run", "--bake", "grok-build"],
                 ["up", "--dry-run", "--bake", "app", "images"],
                 ["up", "--dry-run", "--release", "--bake", "claude-code"]):
        assert cli_main.main(argv) == 2, argv
        err = capsys.readouterr().err
        assert "control plane only" in err and "host baker" in err
    assert not (tmp_path / ".env").exists()
    # the control plane is fine, with or without the test image
    assert cli_main.main(["up", "--dry-run", "--bake", "app", "admin", "hello"]) == 0
    assert cli_main.main(["up", "--dry-run", "--bake", "app-test"]) == 0
    assert up_mod.refused_bake_targets([]) is None
    assert up_mod.refused_bake_targets(["hello"]) is None
    assert "codex" in (up_mod.refused_bake_targets(["app", "codex"]) or "")


def test_up_release_refusal_is_exit_6_and_touches_nothing(monkeypatch, tmp_path):
    _ensure_cli_importable()
    import devcake_cli.up as up_mod
    from devcake_cli import release as release_mod
    sock = _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKER_SOCK", str(sock))
    called = []
    monkeypatch.setattr(up_mod, "_bake", lambda *a, **k: called.append("bake"))

    def refuse(root, wanted, **kw):
        raise release_mod.ReleaseRefused("the checkout has modified tracked files")
    monkeypatch.setattr(release_mod, "checkout_release", refuse)
    assert up_mod.run_up(up_mod.UpOptions(release="latest"), repo=tmp_path) == 6
    assert called == []
    assert not (tmp_path / ".env").exists()


def test_up_flags_parse_release(monkeypatch):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    opts = cli_main.parse_up_flags(["--release"])
    assert opts.release == "latest" and opts.bake is False
    opts = cli_main.parse_up_flags(["--release", "v0.5.11", "--no-hello-smoke"])
    assert opts.release == "v0.5.11" and opts.no_hello_smoke
    opts = cli_main.parse_up_flags(["--release", "--bake", "app", "admin"])
    assert opts.release == "latest" and opts.bake_targets == ["app", "admin"]
    opts = cli_main.parse_up_flags(["--bake"])
    assert opts.release is None


# ── prune: the planner and the executor ─────────────────────────────────────

IMAGES = [
    ("devcake/app", "v0.5.11", "aaa111"), ("devcake/app", "v0.5.10", "aaa110"),
    ("devcake/app", "latest", "aaa000"), ("devcake/admin", "v0.5.10", "bbb110"),
    ("devcake/app-test", "latest", "ccc000"), ("devcake/dev-hello", "v0.5.9", "ddd009"),
    ("devcake/dev-hello", "v0.5.11", "ddd011"),
    ("devcake/dev-grok-build", "v0.5.9-1.0.13", "eee009"),
    ("devcake/dev-claude-code", "v0.5.10-2.1.258", "fff010"),
    ("gitea/gitea", "1.27.1-rootless", "ggg000"), ("<none>", "<none>", "hhh000"),
    ("<none>", "<none>", "iii000"),
]


def test_plan_removes_stale_control_plane_and_dangling_never_devs():
    _ensure_cli_importable()
    from devcake_cli.prune import plan_image_prune
    plan = plan_image_prune(IMAGES, tag="v0.5.11", in_use={"devcake/app:v0.5.10", "iii000"})
    assert sorted(plan.remove) == sorted([
        "devcake/app:latest", "devcake/admin:v0.5.10", "devcake/app-test:latest",
        "devcake/dev-hello:v0.5.9", "hhh000"])
    kept = dict(plan.keep)
    assert kept["devcake/app:v0.5.11"] == "current release"
    assert kept["devcake/app:v0.5.10"] == "in use"
    assert kept["iii000"] == "in use"
    assert "the baker" in kept["devcake/dev-grok-build:v0.5.9-1.0.13"]
    assert "the baker" in kept["devcake/dev-claude-code:v0.5.10-2.1.258"]
    assert kept["gitea/gitea:1.27.1-rootless"] == "not a DevCake image"
    assert not any(r.startswith("devcake/dev-grok") or r.startswith("devcake/dev-claude")
                   for r in plan.remove)


def test_prune_verb_rmis_only_the_plan_and_asks_the_app_for_devs(monkeypatch, tmp_path, capsys):
    _ensure_cli_importable()
    import devcake_cli.main as cli_main
    import devcake_cli.prune as prune_mod
    _fake_checkout(tmp_path)
    (tmp_path / ".env").write_text("DEVCAKE_TAG=v0.5.11\n")
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["docker", "images"]:
            return subprocess.CompletedProcess(argv, 0, stdout="\n".join(
                f"{r} {t} {i}" for r, t, i in IMAGES) + "\n", stderr="")
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(argv, 0, stdout="devcake/app:v0.5.11 sha256:aaa111\n", stderr="")
        if argv[:2] == ["docker", "rmi"]:
            if argv[2] == "hhh000":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="conflict: image is being used")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(argv)
    monkeypatch.setattr(prune_mod.subprocess, "run", fake_run)
    posted = []
    monkeypatch.setattr(prune_mod.admin_api, "request",
                        lambda root, method, path, **kw: posted.append((method, path)) or (200, {"ok": True}))
    rc = cli_main.main(["prune", "--devs"])
    out = capsys.readouterr().out
    assert rc == 0
    rmis = sorted(a[2] for a in calls if a[:2] == ["docker", "rmi"])
    # the fake `ps` shows only the v0.5.11 app in use, so v0.5.10 goes too
    assert rmis == sorted(["devcake/app:latest", "devcake/app:v0.5.10",
                           "devcake/admin:v0.5.10", "devcake/app-test:latest",
                           "devcake/dev-hello:v0.5.9", "hhh000", "iii000"])
    assert not any(a[:2] == ["docker", "rmi"] and "-f" in a for a in calls)
    assert posted == [("POST", "/api/v1/harness/prune")]
    assert "removed 6 of 7 stale image(s)" in out
    assert "kept hhh000: conflict" in out
    assert "Dev-image prune requested" in out
    # dry run: nothing removed, nothing posted
    calls.clear(); posted.clear()
    rc = cli_main.main(["--json", "prune", "--devs", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and payload["images"]["removed"] == [] and posted == []
    assert len(payload["images"]["planned"]) == 7
    assert cli_main.main(["prune", "--help"]) == 0
    assert cli_main.main(["prune", "--bogus"]) == 2
