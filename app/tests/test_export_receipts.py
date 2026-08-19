"""Structural verify + path-print contract for scripts/export_receipts.py.

The OO export path needs a live OpenObserve; these drills hit only the
post-write tar verify seam (mirrors backup_payload.sh's tzf walk) without
spinning OO or docker.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path("/srv/repo-scripts/export_receipts.py")


def _load():
    assert SCRIPT.exists(), (
        "mount missing — the pytest runner must bind scripts → /srv/repo-scripts"
    )
    spec = importlib.util.spec_from_file_location("export_receipts", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _good_tar(path: Path) -> None:
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(name="runs/example.json")
        data = b'{"ok": true}\n'
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))


def test_verify_written_tar_rejects_truncated_archive(tmp_path):
    mod = _load()
    archive = tmp_path / "data-sans-secrets.tar.gz"
    _good_tar(archive)
    archive.write_bytes(archive.read_bytes()[:40])

    with pytest.raises(SystemExit) as ei:
        mod.verify_written_tar(archive)
    assert ei.value.code != 0
    assert "verify" in str(ei.value).lower() or "data tar" in str(ei.value).lower()


def test_verify_written_tar_returns_absolute_path_on_success(tmp_path, capsys):
    mod = _load()
    archive = tmp_path / "data-sans-secrets.tar.gz"
    _good_tar(archive)

    verified = mod.verify_written_tar(archive)
    assert verified == archive.resolve()
    assert verified.is_absolute()
    # Listing must succeed independently of the helper's return value.
    assert subprocess.run(
        ["tar", "tzf", str(archive)], capture_output=True
    ).returncode == 0
