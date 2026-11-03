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
