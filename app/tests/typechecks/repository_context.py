"""Static witnesses: concrete caches must satisfy the preparation port."""
from devcake.domain.repo_mirror import NullRepoCache, RepoCache
from devcake.ports.repository_context import RepositoryContextCache


def implementations(real: RepoCache, null: NullRepoCache) -> None:
    production: RepositoryContextCache = real
    fallback: RepositoryContextCache = null
    del production, fallback
