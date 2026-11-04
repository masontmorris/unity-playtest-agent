"""The frontier model is a run setting: one priced table, and every request, ledger and cost follows it."""

from __future__ import annotations

import pytest

from playtest.bench.replay import provider_of
from playtest.frontier import provider


def test_choosing_a_model_changes_its_requests_price_and_arm_and_an_unknown_one_is_refused():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "prompt_tokens_details": {"cached_tokens": 0}}
    try:
        provider.use_model("muse-wonder-1.0")
        assert provider.MODEL == "muse-wonder-1.0" and provider._cost(usage) == pytest.approx(24.0)
        provider.use_model("muse-spark-1.3")
        assert provider._cost(usage) == pytest.approx(5.5)
        with pytest.raises(ValueError):
            provider.use_model("muse-unknown")
    finally:
        provider.use_model(provider.DEFAULT_MODEL)
    assert provider_of("muse-wonder-1.0") == "meta"


def test_the_contributor_tier_is_priced_as_preflight_prices_it_and_needs_its_own_approved_arm():
    from playtest.bench.replay import validate_approval
    from playtest.preflight.provider import CONTRIBUTOR_MODEL, MODEL_PRICES

    assert provider.PRICES[CONTRIBUTOR_MODEL] == MODEL_PRICES[CONTRIBUTOR_MODEL]
    assert provider_of(CONTRIBUTOR_MODEL) == "meta"
    standard_only = {"approved_by": "user", "approved_at_utc": "2026-11-04T00:00:00Z", "max_calls": 5,
                     "max_cost_usd": 1, "arms": {"muse-spark-1.3": {
                         "max_calls": 5, "per_call_max_usd": 0.1, "max_cost_usd": 1,
                         "verified_balance_usd": 10, "balance_checked_at_utc": "2026-11-04T00:00:00Z"}}}
    with pytest.raises(ValueError, match="muse-spark-1.3-contributor needs a positive call limit"):
        validate_approval(standard_only, [CONTRIBUTOR_MODEL])   # a run on one tier with the other's approval
