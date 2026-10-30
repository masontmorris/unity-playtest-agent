"""Pacing: the token budget waits exactly as long as the provider's rate limit needs, and S1 reads the wait.

Every test runs on a fake clock and a fake sleep that moves it; nothing really waits.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from loop_fakes import FakeTime
from playtest.bench.replay import ARMS
from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.runner.pacing import (INITIAL_ESTIMATE, TOKENS_PER_MINUTE, TOKENS_PER_MINUTE_BY_ARM, PacingFacts,
                                    TokenBudget, default_tokens_per_minute, resolve_tokens_per_minute, wait_fact)


def budget(limit: float = 10_000, **kwargs) -> tuple[TokenBudget, FakeTime]:
    fake = FakeTime()
    return TokenBudget(limit, clock=fake.clock, sleep=fake.sleep, **kwargs), fake


def ready(b: TokenBudget) -> float:
    return asyncio.run(b.ready())


def spent(b: TokenBudget, fake: FakeTime, *calls: tuple[float, int]) -> None:
    """Record calls at the given seconds after the start: (second, tokens)."""
    start = fake.now
    for second, tokens in calls:
        fake.now = start + second
        b.record(tokens)


# --- waiting ------------------------------------------------------------------------------


def test_it_waits_exactly_long_enough_for_the_oldest_call_to_leave_the_window():
    b, fake = budget(10_000)
    spent(b, fake, (0, 4000), (10, 4000), (20, 2000))     # 10,000 in the window; the next call is expected to use 2,000
    fake.now = 1025.0
    waited = ready(b)
    # Dropping the first call (4,000) is enough: it leaves the window at second 60, 35 s from now.
    assert fake.slept == [35.0] and waited == 35.0 and fake.now == 1060.0
    assert b.in_window == 6000                            # the first call is out, the other two are in
    assert ready(b) == 0.0 and fake.slept == [35.0]       # and the next call now fits


def test_it_waits_for_as_many_calls_as_it_must_to_fit():
    b, fake = budget(7000)
    spent(b, fake, (0, 3000), (10, 3000), (20, 3000))     # 9,000 + an expected 3,000 is 5,000 over the limit
    fake.now = 1025.0
    waited = ready(b)
    # One call leaving frees 3,000 and is not enough; the second leaving, at second 70, is.
    assert fake.slept == [45.0] and waited == 45.0
    assert b.in_window == 3000


def test_it_does_not_wait_when_the_next_call_fits_and_a_call_that_just_fits_does_not_wait_either():
    b, fake = budget(10_000)
    spent(b, fake, (0, 4000))
    assert ready(b) == 0.0 and fake.slept == []           # 4,000 + an expected 4,000 is under 10,000
    spent(b, fake, (1, 1000))                             # 5,000 in the window; the next is expected to use 1,000
    assert ready(b) == 0.0 and fake.slept == []
    exact, clock = budget(10_000)
    spent(exact, clock, (0, 5000))                        # 5,000 + 5,000 is exactly the limit
    assert ready(exact) == 0.0 and clock.slept == []
    over, clock = budget(10_000)
    spent(over, clock, (0, 5001))                         # one token over: it must wait
    assert ready(over) == 60.0 and clock.slept == [60.0]


def test_old_tokens_leave_the_window():
    b, fake = budget(10_000)
    spent(b, fake, (0, 9000))
    assert b.in_window == 9000
    fake.now += 59.9
    assert b.in_window == 9000
    waited = ready(b)
    assert waited == pytest.approx(0.1) and fake.slept == [pytest.approx(0.1)]
    assert b.in_window == 0                               # a call that is a minute old no longer counts
    fake.now += 1.0
    assert ready(b) == 0.0


def test_the_window_is_the_trailing_one_and_a_wider_window_keeps_calls_longer():
    b, fake = budget(10_000, window_s=120.0)
    spent(b, fake, (0, 9000))
    fake.now += 90.0
    assert b.in_window == 9000
    assert ready(b) == 30.0


def test_it_uses_the_estimate_before_the_first_call_and_the_last_call_after():
    b, fake = budget(10_000)
    assert b.expected == INITIAL_ESTIMATE == 3000
    assert budget(10_000, initial_estimate=500)[0].expected == 500
    b.record(1234)
    assert b.expected == 1234
    b.record(4321)
    assert b.expected == 4321                             # the most recent call, not an average or a maximum
    fake.now += 3600.0
    assert b.in_window == 0 and b.expected == 4321        # it stays the expectation after the window is empty


def test_the_first_turn_never_waits_because_nothing_has_been_counted():
    b, fake = budget(10_000)
    assert ready(b) == 0.0 and fake.slept == []
    tight, clock = budget(2000)                           # a limit below the first call's estimate
    assert ready(tight) == 0.0 and clock.slept == []      # an empty window is never waited on; it could not help


def test_a_call_larger_than_the_limit_goes_out_once_the_window_is_empty():
    b, fake = budget(5000)
    b.record(6000)                                        # the call itself is over the limit
    assert ready(b) == 60.0 and fake.slept == [60.0]      # it waits for the window to empty, no longer
    assert b.in_window == 0 and ready(b) == 0.0


def test_estimated_records_are_counted_like_measured_ones_and_marked():
    measured, fake_m = budget(10_000)
    estimated, fake_e = budget(10_000)
    measured.record(8000)
    estimated.record(8000, estimated=True)
    assert ready(measured) == ready(estimated) == 60.0
    assert (measured.recorded, measured.estimated) == (1, 0)
    assert (estimated.recorded, estimated.estimated) == (1, 1)
    estimated.record(10, estimated=True)
    estimated.record(10)
    assert (estimated.recorded, estimated.estimated) == (3, 2)


def test_the_returned_wait_is_the_time_that_passed_and_a_short_sleep_is_made_up():
    b, fake = budget(10_000)
    spent(b, fake, (0, 9000))
    real_sleep = fake.sleep

    async def slow(seconds):                              # a sleep that overshoots by half a second
        await real_sleep(seconds + 0.5)

    b._sleep = slow
    assert ready(b) == pytest.approx(60.5)                # what the clock measured, not what was asked

    early, clock = budget(10_000)
    spent(early, clock, (0, 9000))
    asked = []

    async def wakes_early(seconds):                       # a sleep that returns 0.2 s short, once
        asked.append(seconds)
        clock.now += seconds - 0.2 if len(asked) == 1 else seconds

    early._sleep = wakes_early
    waited = ready(early)
    assert asked == [60.0, pytest.approx(0.2)] and waited == pytest.approx(60.0)
    assert early.in_window == 0                           # it did not return while the call was still counted


def test_a_stream_of_calls_at_the_measured_rate_never_puts_more_than_the_limit_in_a_minute():
    """About 41 calls a minute at 2,200 tokens is 90,000 a minute; the default budget holds it under 85,000."""
    b, fake = budget(TOKENS_PER_MINUTE)
    waited_total = 0.0
    for _ in range(300):
        waited_total += ready(b)
        fake.now += 1.46                                  # a hold and a call
        b.record(2200)
        assert b.in_window <= TOKENS_PER_MINUTE
    assert waited_total > 0                               # unpaced, a minute of these calls is over the limit


# --- bad input ----------------------------------------------------------------------------


def test_a_budget_and_its_records_refuse_values_that_are_not_finite_positive_numbers():
    for bad in (0, -1, float("nan"), float("inf"), True, "10", None):
        with pytest.raises(ValueError):
            TokenBudget(bad)
    with pytest.raises(ValueError):
        TokenBudget(1000, window_s=0)
    with pytest.raises(ValueError):
        TokenBudget(1000, initial_estimate=-5)
    b, _ = budget()
    for bad in (-1, float("nan"), float("inf"), True, "5", None):
        with pytest.raises(ValueError):
            b.record(bad)
    b.record(0)                                           # a provider-reported zero is a count, not a refusal
    assert b.recorded == 1


def test_the_default_leaves_fifteen_percent_under_the_provider_limit():
    assert TOKENS_PER_MINUTE == 100_000 * 85 // 100


def test_each_arm_resolves_its_own_default_and_an_explicit_rate_still_wins():
    assert set(TOKENS_PER_MINUTE_BY_ARM) <= set(ARMS)      # a typo cannot silently create a new arm
    assert all(isinstance(rate, int) and rate >= 1 for rate in TOKENS_PER_MINUTE_BY_ARM.values())
    for arm in ARMS:
        expected = TOKENS_PER_MINUTE_BY_ARM.get(arm, TOKENS_PER_MINUTE)
        assert default_tokens_per_minute(arm) == expected
        assert resolve_tokens_per_minute(arm, None) == expected
        assert resolve_tokens_per_minute(arm, 12_345) == 12_345
    assert default_tokens_per_minute("no-such-arm") == TOKENS_PER_MINUTE   # no arm means no invented rate


# --- the fact S1 reads --------------------------------------------------------------------


def observation() -> Observation:
    return Observation(session="s", seq=1, game_s=1.0, wall_utc=datetime(2026, 10, 28, tzinfo=timezone.utc),
                       clock_mode=ClockMode.realtime, scene="a", playing=True, paused=False, time_scale=1.0, frame=1)


def test_the_wait_is_stated_as_a_measurement_once_and_only_after_a_real_wait():
    async def main():
        facts = PacingFacts()
        assert await facts.facts([], observation()) == []                  # nothing waited, nothing said
        facts.note(0.0)
        assert await facts.facts([], observation()) == []                  # a noted zero is no wait
        facts.note(0.84)
        assert await facts.facts([], observation()) == [
            "Before this observation the harness waited 0.84 s to stay under the provider's token rate limit."]
        assert await facts.facts([], observation()) == []                  # stated for that observation only
        facts.note(0.004)
        assert await facts.facts([], observation()) == [wait_fact(0.004)]
        assert wait_fact(0.004).startswith("Before this observation the harness waited 0.0040 s")

    asyncio.run(main())


def test_the_wait_line_follows_the_loops_own_facts_and_carries_no_advice():
    class Inner:
        async def facts(self, reports, observation):
            return ["Turn 3: wait was chosen; no input was sent."]

    async def main():
        facts = PacingFacts(Inner())
        facts.note(12.5)
        lines = await facts.facts([], observation())
        assert lines[0] == "Turn 3: wait was chosen; no input was sent."
        assert lines[1] == wait_fact(12.5) and len(lines) == 2
        words = lines[1].lower()
        assert not any(advice in words for advice in ("should", "avoid", "try", "consider", "recommend", "slow"))

    asyncio.run(main())


def test_image_reserve_causes_a_wait_when_the_next_view_could_exceed_the_rate():
    b, fake = budget(10_000)
    b.record(4_750)  # previous call had no image
    assert ready(b) == 0.0
    b.reserve_next_image(1_000)
    assert b.expected == 5_750 and ready(b) == 60.0
    assert fake.slept == [60.0] and b.in_window == 0
    b.record(5_750)  # next call includes an image, so it needs no second reserve
    b.reserve_next_image(0)
    assert b.expected == 5_750
