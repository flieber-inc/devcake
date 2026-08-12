"""docs/contracts/spa-contracts.json is generated from the Python source of
truth (gen_spa_contracts.build) and replayed by the SPA's pure-node suite —
the cross-language pin the 2026-08-12 audit found missing for the three
hand-mirrors (board derive precedence, instance-name rule, card scaffolds).
This side turns red when the BACKEND changes without regenerating."""

import json
from pathlib import Path

from tests.gen_spa_contracts import build

CONTRACTS = Path("/srv/docs/contracts/spa-contracts.json")


def test_contracts_file_matches_the_source_of_truth():
    assert CONTRACTS.exists(), (
        "mount missing — the pytest runner must bind docs → /srv/docs"
    )
    committed = json.loads(CONTRACTS.read_text())
    fresh = build()
    assert committed == fresh, (
        "docs/contracts/spa-contracts.json is stale vs the backend — "
        "regenerate: docker run --rm -v $(pwd)/app:/srv-rw -w /srv-rw "
        "devcake/app-test:latest python -m tests.gen_spa_contracts "
        "> docs/contracts/spa-contracts.json  (then re-run the SPA "
        "test:helpers suite, which replays the same file)"
    )


def test_vector_sweep_is_the_full_powerset():
    """The drift guarantee rests on coverage: 4 statuses × 2 adoption modes
    × 2^8 label subsets — ANY precedence reordering in derive() must land
    in some vector."""
    data = build()
    assert len(data["board"]["vectors"]) == 4 * 2 * 256
    assert len(data["board"]["reasons"]) >= 8
