"""Focused static contract gate, run by the ordinary fresh-image pytest path."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def check(*paths: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--follow-imports=silent",
         "--ignore-missing-imports", "--check-untyped-defs",
         "--disallow-incomplete-defs", "--warn-unused-ignores",
         "--cache-dir=/tmp/devcake-mypy", *(str(p) for p in paths)],
        cwd=ROOT, text=True, capture_output=True, timeout=120)


def test_repository_context_port_and_implementations_typecheck():
    result = check(
        ROOT / "devcake/ports/repository_context.py",
        ROOT / "devcake/domain/repository_context.py",
        ROOT / "tests/typechecks/repository_context.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_type_gate_rejects_an_incompatible_cache(tmp_path):
    bad = tmp_path / "incompatible_cache.py"
    bad.write_text('''from devcake.domain.repo_mirror import NullRepoCache
from devcake.ports.repository_context import RepositoryContextCache

class BrokenCache(NullRepoCache):
    async def ensure_fresh(self, names: list[str]) -> tuple[bool, list[str]]:
        return False, ["missing failure keys"]

cache: RepositoryContextCache = BrokenCache()
''')
    result = check(bad)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "RepositoryContextCache" in result.stdout
    assert "ensure_fresh" in result.stdout
