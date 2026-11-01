"""Travel facts: the vector the probe records, and the window statement S1 reads, measured only."""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace as N

import pytest

from playtest.runner.probe import displacement_between
from playtest.runner.travel import TravelFacts

WALL = "world units"
OBSERVATION = N(public={})
ADVISORY = ("stuck", "progress", "should", "try", "recommend", "instead", "avoid", "blocked",
            "suggest", "better", "best", "need to", "must")


# --- part 1: the probe records the vector and the positions --------------------------------


def test_displacement_between_records_the_vector_and_both_positions():
    before = {"inspection_seq": 4, "position": [1.0, 2.0, 3.0], "game_time_s": 5.0}
    after = {"inspection_seq": 9, "position": [1.5, 2.0, 3.25], "game_time_s": 6.0}
    got = displacement_between(before, after)
    assert got["displacement"] == [0.5, 0.0, 0.25] and got["net_displacement"] == pytest.approx(math.hypot(0.5, 0.25))
    assert got["from_position"] == [1.0, 2.0, 3.0] and got["to_position"] == [1.5, 2.0, 3.25]
    assert got["unit"] == WALL
    assert (got["from_inspection_seq"], got["to_inspection_seq"], got["window_game_s"]) == (4, 9, 1.0)


def test_an_unknown_position_leaves_that_measurement_null_never_zero():
    got = displacement_between({"inspection_seq": 4, "position": None},
                               {"inspection_seq": 5, "position": [1.0, 0.0, 0.0]})
    assert got["displacement"] is None and got["net_displacement"] is None
    assert got["from_position"] is None and got["to_position"] == [1.0, 0.0, 0.0]
    reverse = displacement_between({"inspection_seq": 4, "position": [1.0, 0.0, 0.0]},
                                   {"inspection_seq": 5, "position": [2.0, 0.0, 0.0]})
    assert reverse["to_position"] == [2.0, 0.0, 0.0] and reverse["displacement"] == [1.0, 0.0, 0.0]
    assert displacement_between(None, {"position": [1.0, 0.0, 0.0]}) is None


# --- part 2: the window statement -----------------------------------------------------------


class Inner:
    """A facts source that states one line of its own, so a test can see what the wrapper adds."""

    def __init__(self) -> None:
        self.rebinds = 0

    async def facts(self, *args):
        return ["base"]

    def rebind(self, **values):
        self.rebinds += 1


def observed(step, start=None, end=None):
    """One executed choice's effect as a live probe records it, magnitude derived from the vector."""
    return {"net_displacement": math.sqrt(sum(value * value for value in step)) if step is not None else None,
            "displacement": None if step is None else [float(value) for value in step],
            "from_position": None if start is None else [float(value) for value in start],
            "to_position": None if end is None else [float(value) for value in end],
            "unit": WALL}


def report(choice, effect):
    return N(executed=[N(choice=N(id=choice), execution=N(rejected=False),
                         effect=None if effect is None else N(observed=effect))])


def window():
    """14 jumps that leave the body where it was, 4 running jumps, 2 steps right: the live run's shape."""
    entries = [("jump", (0.0, 0.0, 0.0), (3.0, 0.0, 0.0), (3.0, 0.0, 0.0))] * 14
    entries += [("jump_right", (0.05, 0.0, 0.0), (3.0, 0.0, 0.0), (3.05, 0.0, 0.0))] * 4
    entries += [("move_right", (0.15, 0.0, 0.0), (3.05, 0.0, 0.0), (3.2, 0.0, 0.0))] * 2
    return [report(choice, observed(step, start, end)) for choice, step, start, end in entries]


def say(reports, *, inner=None, facts=None):
    """Every facts line the wrapper states over these reports, the way the loop asks once per turn."""
    async def go():
        source = facts if facts is not None else TravelFacts(inner if inner is not None else Inner())
        return list(await source.facts(reports, OBSERVATION))
    return asyncio.run(go())


def test_the_fact_is_silent_below_the_window():
    assert say(window()[:19]) == ["base"]
    assert say(window()[:20])[0] == "base"
    assert say(window()[:20])[1].startswith("Over your last 20 executed choices")


def test_the_exact_statement_for_a_window_that_returns_to_where_it_started():
    line = say(window())[1]
    assert line == (
        "Over your last 20 executed choices the body moved 0.5 world units in total; its position changed by "
        "(+0.2, +0.0, +0.0) world units. jump x14: mean (+0.0, +0.0, +0.0); jump_right x4: mean "
        "(+0.05, +0.0, +0.0); move_right x2: mean (+0.15, +0.0, +0.0).")


def test_a_choice_without_a_measured_displacement_is_counted_as_unmeasured():
    unmeasured = report("jump", None)
    # 20 executed, only 19 measured: nothing is stated yet.
    assert say([*window()[:19], unmeasured]) == ["base"]
    # 22 executed, 20 measured: the last 20 hold two unmeasured choices.
    line = say([*window(), unmeasured, unmeasured])[1]
    assert line == (
        "Over your last 20 executed choices the body moved 0.5 world units in total; its position changed by "
        "(+0.2, +0.0, +0.0) world units. jump x14 (12 with a measured vector): mean (+0.0, +0.0, +0.0); "
        "jump_right x4: mean (+0.05, +0.0, +0.0); move_right x2: mean (+0.15, +0.0, +0.0). "
        "2 of those choices were unmeasured.")


def test_the_window_starts_over_when_the_session_restarts():
    first = window()
    later = [report("move_right", observed((0.3, 0.0, 0.0), (100.0, 0.0, 0.0), (100.3, 0.0, 0.0)))] * 20
    inner = Inner()

    async def go():
        facts = TravelFacts(inner)
        before = await facts.facts(first, OBSERVATION)
        facts.rebind()                       # the play session restarted: a new probe, a new window
        after_restart = await facts.facts(first, OBSERVATION)
        grown = await facts.facts([*first, *later], OBSERVATION)
        return before, after_restart, grown

    before, after_restart, grown = asyncio.run(go())
    assert before[1].startswith("Over your last 20 executed choices")
    assert after_restart == ["base"]                     # nothing measured after the restart yet
    assert grown[1].startswith("Over your last 20 executed choices")
    assert "its position changed by (+0.3, +0.0, +0.0)" in grown[1]   # only positions from this session
    assert inner.rebinds == 1                            # the inner source is rebound too


def test_the_fact_states_quantities_only():
    line = say(window())[1]
    assert "Over your last 20 executed choices the body moved" in line
    assert f"{WALL} in total" in line
    lowered = line.lower()
    assert not [word for word in ADVISORY if word in lowered]


def test_an_old_trace_without_positions_states_the_change_as_unmeasured():
    """An effect that carries only the magnitude leaves the vector unmeasured, rather than zero."""
    old = [report("jump", {"net_displacement": 2.5, "unit": WALL})] * 20
    assert say(old)[1] == (
        "Over your last 20 executed choices the body moved 50.0 world units in total; "
        "its position change was not measured. jump x20: mean displacement vector not measured.")


# --- default off: the flag, the plan, the context and the trace ------------------------------


def test_the_flag_is_off_by_default_and_the_environment_turns_it_on(monkeypatch):
    from playtest.runner import cli
    required = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    monkeypatch.delenv("UPA_RUN_TRAVEL_FACTS", raising=False)
    assert not cli._parser().parse_args(required).travel_facts
    assert cli._parser().parse_args(required + ["--travel-facts"]).travel_facts
    monkeypatch.setenv("UPA_RUN_TRAVEL_FACTS", "1")
    assert cli._parser().parse_args(required).travel_facts


from test_speed_finish_options import plan_run  # noqa: E402,F401  (the fixture that runs main to the plan)


def test_the_plan_records_travel_facts_only_when_on(plan_run, monkeypatch):
    monkeypatch.delenv("UPA_RUN_TRAVEL_FACTS", raising=False)
    assert "travel_facts" not in plan_run()
    assert plan_run("--travel-facts")["travel_facts"] is True


def test_the_facts_and_the_trace_gain_nothing_without_the_flag():
    from catalog_game import CatalogGame
    from loop_fakes import FakeS1, FakeTime
    from test_runner_session import OBJECTIVE, closed, connected, harness_trace, session, two_actions

    from playtest.core.facts import RecordFacts
    from playtest.preflight.catalog import export
    from playtest.runner.choices import build_choices
    from playtest.runner.probe import DisplacementProbe, make_snapshot

    async def run(wrapped):
        value = two_actions()
        game, client = await connected(CatalogGame, catalog=export(value))
        built = build_choices(value)
        sink = harness_trace()
        try:
            facts = TravelFacts(RecordFacts()) if wrapped else RecordFacts()
            s1 = FakeS1("move", "look", "wait", "finish")
            await session(client, built, s1=s1, objective=OBJECTIVE, sink=sink, facts=facts, clock=FakeTime().clock,
                          probe=DisplacementProbe(make_snapshot(client, "entity"), built.expected),
                          max_age_s=2.0, wall_limit_s=60.0)
            return [list(context["facts"]) for context in s1.contexts], sink
        finally:
            await closed(game, client)

    plain_facts, plain_sink = asyncio.run(run(False))
    wrapped_facts, wrapped_sink = asyncio.run(run(True))
    assert wrapped_facts == plain_facts                        # the same contexts, byte for byte
    assert not [line for lines in plain_facts for line in lines if line.startswith("Over your last")]
    # The vector keys are always on, flag or no flag: they are the probe's measured data.
    for sink in (plain_sink, wrapped_sink):
        bodies = [entry["body"]["observed"] for entry in sink.of_kind("effect")]
        assert bodies and all({"displacement", "from_position", "to_position"} <= set(body) for body in bodies)
