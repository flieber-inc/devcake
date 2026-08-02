"""Pure app-side cost estimation from the operator rate card (ADR-0021).

The harness layer never estimates (docs/08 §5): grok reports token splits
with cost_usd null, and inventing dollars there would pollute the native
`devcake.cost.usd` accounting. This module prices an already-extracted
token_report against config.cost_inputs — labeled, separate, revocable.

Honesty rules (feedback 2026-08-01):
- no estimate without the full input/cache_read/output split (totals-only
  fallback shapes stay blank), and none for a model no rate row matches;
- reasoning tokens are a SUBSET of output_tokens — never priced on top;
- a null cache_write prices as 0 (grok has no write counter) but stays
  null in the report itself — display renders it as "—", not $0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import CostInputs, ModelRate


def rate_for(model: str | None, rates: list["ModelRate"]) -> "ModelRate | None":
    """The rate row whose model_prefix is the LONGEST prefix of `model`
    (so an operator can keep a broad "grok" row and a sharper "grok-4.5"
    one). None when nothing matches — unknown SKUs are never priced."""
    best = None
    for r in rates:
        if model and model.startswith(r.model_prefix):
            if best is None or len(r.model_prefix) > len(best.model_prefix):
                best = r
    return best


def estimate_cost_usd(token_report: dict,
                      rates: list["ModelRate"]) -> float | None:
    """USD estimate for one token report, or None when honesty forbids one
    (missing split / unmapped model). Rounded to 6 decimals so equal inputs
    compare equal regardless of the caller's arithmetic path."""
    rate = rate_for(token_report.get("model"), rates)
    if rate is None:
        return None
    inp = token_report.get("input_tokens")
    cache_read = token_report.get("cache_read_tokens")
    out = token_report.get("output_tokens")
    if inp is None or cache_read is None or out is None:
        return None
    cache_write = token_report.get("cache_write_tokens") or 0
    return round((inp * rate.input_per_mtok
                  + cache_read * rate.cache_read_per_mtok
                  + cache_write * rate.cache_write_per_mtok
                  + out * rate.output_per_mtok) / 1_000_000, 6)


def stamp_estimate(token_report: dict, cost_inputs: "CostInputs") -> dict:
    """Return the report with `cost_usd_estimated` + `rate_card_id` added
    when an estimate is computable; the input dict is never mutated and
    native `cost_usd` is never touched. Estimates are stamped even when
    native cost exists — the override_native display mode needs both."""
    est = estimate_cost_usd(token_report, cost_inputs.rates)
    if est is None:
        return token_report
    return {**token_report, "cost_usd_estimated": est,
            "rate_card_id": cost_inputs.rate_card_id}


def effective_cost(native: float | None, estimate: float | None,
                   cost_inputs: "CostInputs") -> float | None:
    """The cost a DISPLAY surface should show. Default: native harness cost
    is authoritative, estimates only fill gaps. override_native flips the
    preference; either mode falls back to the other value rather than
    showing nothing when its preferred one is absent."""
    if cost_inputs.override_native:
        return estimate if estimate is not None else native
    return native if native is not None else estimate
