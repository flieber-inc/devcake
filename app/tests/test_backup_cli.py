import os
from pathlib import Path
import subprocess


BACKUP_SCRIPT = Path("/srv/repo-scripts/backup_data.sh")


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
