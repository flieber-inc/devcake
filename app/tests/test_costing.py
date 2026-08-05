"""App-side cost estimation (ADR-0021): a pure rate-card estimator for
harnesses that report token splits but no native cost (grok). The harness
layer stays estimate-free (docs/08 §5 — test_entrypoint_tokens guards it);
everything here happens in the app on the already-extracted token_report.

Rate math uses known literals, never a reimplementation of the estimator."""

from devcake.config import (BUILTIN_RATE_CARD_ID, DEFAULT_MODEL_RATES,
                            CostInputs, ModelRate)
from devcake.domain import costing

GROK_45 = DEFAULT_MODEL_RATES  # $2.00 / $0.30 / $6.00 per 1M, cache_write 0


def _grok_report(**over):
    base = {
        "input_tokens": 1_000_000, "cache_read_tokens": 2_000_000,
        "cache_write_tokens": None, "output_tokens": 500_000,
        "total_tokens": 3_500_000, "cost_usd": None,
        "model": "grok-4.5-build", "extraction_method": "end_event",
        "num_turns": 10, "notes": "reasoning_tokens=20616",
    }
    base.update(over)
    return base


# ── estimate_cost_usd ────────────────────────────────────────────────────────

def test_known_split_prices_to_known_usd():
    # 1M in × $2 + 2M cache-read × $0.30 + 0.5M out × $6 = 2 + 0.6 + 3
    assert costing.estimate_cost_usd(_grok_report(), GROK_45) == 5.60


def test_worked_example_from_fli2_184():
    # the feedback doc's FLI2-184 step 2-EXECUTE ≈ $1.08
    got = costing.estimate_cost_usd(_grok_report(
        input_tokens=95_488, cache_read_tokens=2_428_288,
        output_tokens=26_030, total_tokens=2_549_806), GROK_45)
    assert round(got, 2) == 1.08


def test_missing_any_split_field_means_no_estimate():
    for hole in ("input_tokens", "cache_read_tokens", "output_tokens"):
        assert costing.estimate_cost_usd(_grok_report(**{hole: None}),
                                         GROK_45) is None
    # grok signals.json fallback shape: totals only — never estimated
    totals_only = {"total_tokens": 3_500_000, "cost_usd": None,
                   "model": "grok-4.5-build",
                   "extraction_method": "session_json"}
    assert costing.estimate_cost_usd(totals_only, GROK_45) is None


def test_non_numeric_token_fields_yield_no_estimate_not_500():
    """AUD-017: a malformed token report (a string count from some harness)
    must return None, never TypeError on the multiply — which would 500
    GET /runs at read-time repricing."""
    for hole in ("input_tokens", "cache_read_tokens", "output_tokens"):
        assert costing.estimate_cost_usd(
            _grok_report(**{hole: "lots"}), GROK_45) is None
    # a bool is an int subclass but a nonsense count — also rejected
    assert costing.estimate_cost_usd(
        _grok_report(input_tokens=True), GROK_45) is None
    # a non-numeric cache_write degrades to 0, never crashes (still estimates)
    assert costing.estimate_cost_usd(
        _grok_report(cache_write_tokens="oops"), GROK_45) is not None


def test_null_cache_write_prices_as_zero_not_blocking():
    with_write = costing.estimate_cost_usd(
        _grok_report(cache_write_tokens=1_000_000),
        [ModelRate(model_prefix="grok-4.5", input_per_mtok=2.00,
                   cache_read_per_mtok=0.30, cache_write_per_mtok=3.75,
                   output_per_mtok=6.00)])
    assert with_write == 5.60 + 3.75
    assert costing.estimate_cost_usd(_grok_report(), GROK_45) == 5.60


def test_unmapped_model_means_no_estimate():
    assert costing.estimate_cost_usd(
        _grok_report(model="claude-opus-5"), GROK_45) is None
    assert costing.estimate_cost_usd(_grok_report(model=None), GROK_45) is None


def test_reasoning_tokens_never_added_on_top():
    # reasoning ⊆ output in end_event accounting: the estimate for a report
    # with a huge reasoning note equals the plain-output estimate
    assert (costing.estimate_cost_usd(
        _grok_report(notes="reasoning_tokens=499999"), GROK_45)
        == costing.estimate_cost_usd(_grok_report(notes=""), GROK_45))


# ── rate_for ─────────────────────────────────────────────────────────────────

def test_longest_matching_prefix_wins():
    rates = [
        ModelRate(model_prefix="grok", input_per_mtok=1.0,
                  cache_read_per_mtok=0.2, output_per_mtok=2.0),
        ModelRate(model_prefix="grok-4.5", input_per_mtok=2.0,
                  cache_read_per_mtok=0.3, output_per_mtok=6.0),
    ]
    assert costing.rate_for("grok-4.5-build", rates).model_prefix == "grok-4.5"
    assert costing.rate_for("grok-3-mini", rates).model_prefix == "grok"
    assert costing.rate_for("codex-large", rates) is None
    assert costing.rate_for(None, rates) is None


# ── rate_card_id ─────────────────────────────────────────────────────────────

def test_rate_card_id_builtin_and_operator():
    assert CostInputs().rate_card_id == BUILTIN_RATE_CARD_ID
    edited = CostInputs(rates=[
        ModelRate(model_prefix="grok-4.5", input_per_mtok=2.50,
                  cache_read_per_mtok=0.30, output_per_mtok=6.00)])
    assert edited.rate_card_id.startswith("operator:")
    assert len(edited.rate_card_id) == len("operator:") + 8
    # stable: same rates → same id
    again = CostInputs(rates=[
        ModelRate(model_prefix="grok-4.5", input_per_mtok=2.50,
                  cache_read_per_mtok=0.30, output_per_mtok=6.00)])
    assert again.rate_card_id == edited.rate_card_id


# ── stamp_estimate ───────────────────────────────────────────────────────────

def test_stamp_adds_estimate_and_card_id_when_computable():
    out = costing.stamp_estimate(_grok_report(), CostInputs())
    assert out["cost_usd_estimated"] == 5.60
    assert out["rate_card_id"] == BUILTIN_RATE_CARD_ID
    assert out["cost_usd"] is None            # native never touched


def test_stamp_is_noop_when_not_computable():
    report = _grok_report(model="codex-large")
    out = costing.stamp_estimate(report, CostInputs())
    assert "cost_usd_estimated" not in out
    assert "rate_card_id" not in out
    assert costing.stamp_estimate({}, CostInputs()) == {}


def test_stamp_also_estimates_alongside_native_cost():
    # a mapped model WITH native cost still gets the estimate stamped —
    # the override_native display mode needs it; native stays untouched
    claude_like = _grok_report(model="grok-4.5-build", cost_usd=4.4321)
    out = costing.stamp_estimate(claude_like, CostInputs())
    assert out["cost_usd"] == 4.4321
    assert out["cost_usd_estimated"] == 5.60


# ── effective_cost ───────────────────────────────────────────────────────────

def test_effective_cost_truth_table():
    ci_off = CostInputs()
    ci_on = CostInputs(override_native=True)
    # (native, estimate) → effective under each mode
    assert costing.effective_cost(3.0, 5.6, ci_off) == 3.0
    assert costing.effective_cost(None, 5.6, ci_off) == 5.6
    assert costing.effective_cost(3.0, None, ci_off) == 3.0
    assert costing.effective_cost(None, None, ci_off) is None
    assert costing.effective_cost(3.0, 5.6, ci_on) == 5.6
    assert costing.effective_cost(3.0, None, ci_on) == 3.0   # fallback: native
    assert costing.effective_cost(None, 5.6, ci_on) == 5.6
    assert costing.effective_cost(None, None, ci_on) is None
