import os
import re
import subprocess
import tarfile
from pathlib import Path


BACKUP_SCRIPT = Path("/srv/repo-scripts/backup_data.sh")
RESTORE_SCRIPT = Path("/srv/repo-scripts/restore_data.sh")
PAYLOADS = Path("/srv/repo-scripts/lib")
PINNED = re.compile(r"@sha256:[0-9a-f]{64}\b")


def _payload(name: str) -> Path:
    p = PAYLOADS / name
    assert p.exists(), (
        "mount missing — the pytest runner must bind scripts → /srv/repo-scripts"
    )
    return p


def _run_backup_payload(src: Path, out_dir: Path, kind: str,
                        out_base: str = "t.tar.gz", extra_env=None):
    env = {**os.environ, "SRC": str(src), "OUT_DIR": str(out_dir),
           "OUT_BASE": out_base, "KIND": kind,
           "OWNER": f"{os.getuid()}:{os.getgid()}", **(extra_env or {})}
    return subprocess.run(["sh", str(_payload("backup_payload.sh"))],
                          capture_output=True, text=True, env=env)


def _run_restore_payload(dst: Path, in_dir: Path, kind: str,
                         tarfile_name: str = "t.tar.gz"):
    env = {**os.environ, "DST": str(dst), "IN_DIR": str(in_dir),
           "TARFILE": tarfile_name, "KIND": kind}
    return subprocess.run(["sh", str(_payload("restore_payload.sh"))],
                          capture_output=True, text=True, env=env)


def _tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text()
            for p in root.rglob("*") if p.is_file()}


def test_data_backup_defaults_outside_the_checkout(tmp_path):
    """A no-argument secret backup must not land beside tracked files."""
    assert BACKUP_SCRIPT.exists(), (
        "mount missing — the pytest runner must bind scripts → /srv/repo-scripts"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 0\n")
    fake_docker.chmod(0o700)

    operator_home = tmp_path / "operator-home"
    operator_home.mkdir()
    env = {
        **os.environ,
        "HOME": str(operator_home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    env.pop("XDG_DATA_HOME", None)
    env.pop("DEVCAKE_BACKUP_DIR", None)

    result = subprocess.run(
        [str(BACKUP_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    expected_dir = operator_home / ".local/share/devcake/backups"
    assert f"wrote {expected_dir}/devcake-data-" in result.stdout
    assert expected_dir.stat().st_mode & 0o777 == 0o700


# --- restore drill: the container payloads run for real against tmp dirs ---
# (2026-08-12 audit OPS-H1: restores wiped the volume before validating the
# tarball, and no backup was ever verified — these drills pin the fixed
# ordering with real tar archives, no docker needed.)


def test_backup_restore_round_trip(tmp_path):
    src, out, dst = tmp_path / "src", tmp_path / "out", tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    out.mkdir(), dst.mkdir()
    (src / "a.txt").write_text("hello")
    (src / "sub" / "b.txt").write_text("world")
    (src / ".hidden").write_text("dot")
    (dst / "stale.txt").write_text("previous volume contents")

    r = _run_backup_payload(src, out, "data")
    assert r.returncode == 0, r.stderr
    with tarfile.open(out / "t.tar.gz") as tf:
        assert tf.getnames()[0] == "DEVCAKE_BACKUP_KIND"

    r = _run_restore_payload(dst, out, "data")
    assert r.returncode == 0, r.stderr
    assert _tree(dst) == {"a.txt": "hello", "sub/b.txt": "world",
                          ".hidden": "dot"}
    assert not list(dst.glob(".pre-restore-*")), "aside must be cleaned up"


def test_restore_refuses_corrupt_tarball_without_touching_dst(tmp_path):
    src, out, dst = tmp_path / "src", tmp_path / "out", tmp_path / "dst"
    src.mkdir(), out.mkdir(), dst.mkdir()
    (src / "a.txt").write_text("x")
    (dst / "precious.txt").write_text("only copy")
    assert _run_backup_payload(src, out, "data").returncode == 0
    whole = (out / "t.tar.gz").read_bytes()
    (out / "t.tar.gz").write_bytes(whole[: len(whole) // 2])

    r = _run_restore_payload(dst, out, "data")
    assert r.returncode != 0
    assert _tree(dst) == {"precious.txt": "only copy"}, (
        "a corrupt tarball must not mutate the volume at all"
    )
    assert not list(dst.glob(".pre-restore-*"))


def test_restore_refuses_wrong_kind_without_touching_dst(tmp_path):
    src, out, dst = tmp_path / "src", tmp_path / "out", tmp_path / "dst"
    src.mkdir(), out.mkdir(), dst.mkdir()
    (src / "repo.git").write_text("gitea stuff")
    (dst / "data-thing.json").write_text("app data")
    assert _run_backup_payload(src, out, "gitea").returncode == 0

    r = _run_restore_payload(dst, out, "data")
    assert r.returncode == 2
    assert "kind is 'gitea', expected 'data'" in r.stderr
    assert _tree(dst) == {"data-thing.json": "app data"}
    assert not list(dst.glob(".pre-restore-*"))


def test_restore_accepts_legacy_markerless_backup_with_warning(tmp_path):
    out, dst = tmp_path / "out", tmp_path / "dst"
    out.mkdir(), dst.mkdir()
    legacy_src = tmp_path / "legacy"
    legacy_src.mkdir()
    (legacy_src / "old.txt").write_text("pre-marker era")
    with tarfile.open(out / "t.tar.gz", "w:gz") as tf:
        tf.add(legacy_src / "old.txt", arcname="./old.txt")

    r = _run_restore_payload(dst, out, "data")
    assert r.returncode == 0, r.stderr
    assert "legacy backup" in r.stderr and "unverified" in r.stderr
    assert _tree(dst) == {"old.txt": "pre-marker era"}


def test_backup_verifies_the_archive_it_wrote(tmp_path):
    """A tar wrapper corrupts its own czf output — the payload's verify
    pass (full tzf walk of what was just written) must be what fails, so a
    truncated backup dies on backup day, not on restore day."""
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir(), out.mkdir()
    (src / "a.txt").write_text("x" * 4096)
    fake_bin = tmp_path / "tarbin"
    fake_bin.mkdir()
    wrapper = fake_bin / "tar"
    wrapper.write_text(
        "#!/bin/sh\n"
        'command -p tar "$@"\n'
        "rc=$?\n"
        'if [ "$1" = czf ]; then truncate -s 40 "$2"; fi\n'
        "exit $rc\n"
    )
    wrapper.chmod(0o700)
    r = _run_backup_payload(
        src, out, "data",
        extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})
    assert r.returncode != 0, "verify pass must catch the truncated archive"


# --- host-side contract: the scripts drive docker with the pinned image ---


def _fake_docker(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker-args.log"
    fake = fake_bin / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = run ]; then echo "$@" >> {log}; fi\n'
        "exit 0\n"
    )
    fake.chmod(0o700)
    return fake_bin, log


def test_backup_script_uses_pinned_image_and_payload(tmp_path):
    fake_bin, log = _fake_docker(tmp_path)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
           "DEVCAKE_BACKUP_DIR": str(tmp_path / "backups")}
    r = subprocess.run([str(BACKUP_SCRIPT)], check=True, capture_output=True,
                       text=True, env=env)
    args = log.read_text()
    assert PINNED.search(args), "backup container image must be digest-pinned"
    assert "backup_payload.sh" in args
    assert "KIND=data" in args
    assert "(verified readable)" in r.stdout


def test_restore_script_uses_pinned_image_and_payload(tmp_path):
    fake_bin, log = _fake_docker(tmp_path)
    tarball = tmp_path / "devcake-data-x.tar.gz"
    tarball.write_bytes(b"irrelevant - docker is faked")
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    subprocess.run([str(RESTORE_SCRIPT), str(tarball)], check=True,
                   capture_output=True, text=True, env=env)
    args = log.read_text()
    assert PINNED.search(args), "restore container image must be digest-pinned"
    assert "restore_payload.sh" in args
    assert "KIND=data" in args
    assert "find /dst" not in args, "delete-first restore must stay dead"
