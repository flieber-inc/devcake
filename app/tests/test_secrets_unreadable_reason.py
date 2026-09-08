"""secrets._read names WHY a file was unreadable: a descriptor shortage
(EMFILE) reads exactly like a corrupt file otherwise (2026-09 incident:
7,993 "unreadable secret file" lines that were all "too many open files")."""
import logging
from pathlib import Path

from devcake import secrets


def test_unreadable_secret_log_names_the_exception(tmp_path, caplog):
    bad = tmp_path / "repo-x.json"
    bad.write_text("{not json")
    with caplog.at_level(logging.ERROR, logger="devcake.secrets"):
        assert secrets._read(bad) == {}
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "unreadable secret file" in msg and "JSONDecodeError" in msg


def test_unreadable_secret_log_names_an_os_error(tmp_path, caplog, monkeypatch):
    p = tmp_path / "repo-y.json"
    p.write_text("{}")
    real = Path.read_text

    def boom(self, *a, **k):
        if self == p:
            raise OSError(24, "Too many open files")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", boom)
    with caplog.at_level(logging.ERROR, logger="devcake.secrets"):
        assert secrets._read(p) == {}
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "OSError" in msg and "Too many open files" in msg
