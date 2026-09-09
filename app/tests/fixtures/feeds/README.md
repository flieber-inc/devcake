# Recorded feeds (ADR-0042 golden test)

`legacy_three_step.json` is the feed the PRE-CARD build posted for a
three-step mission (ONBOARD → EXECUTE → REVIEW) on the test fakes, recorded
once by running `app/tests/golden_feeds.py` against that build. Bodies are
verbatim, in posting order, with the attachment names each comment linked.
`test_feed_projection_golden.py` runs the same recorder against the current
build and asserts that both feeds unfold to the same Dev folder. Synthetic
identifiers only. Do not regenerate this file from a card-posting build —
it would no longer be the legacy shape.
