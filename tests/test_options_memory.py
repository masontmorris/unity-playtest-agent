"""Outcome memory: cells, suffixes, settled points and the place fact (docs/design/options-layer.md, 5 and 7).

Feed it measured outcomes and observations, then read what it renders. Every assertion here is about a
quantity, a cell or a format, never about a game.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from playtest.contracts.bridge import Observation
from playtest.contracts.common import ClockMode
from playtest.runner.options.memory import MINUS, OutcomeMemory
from playtest.runner.options.model import OptionOutcome

# Words that would turn a measured fact into advice. The rendered facts may state quantities only.
ADVICE = frozenset(
    """
    should must need needs try tries trying stuck progress recommend suggests consider better worse
    perhaps maybe avoid instead again retry attempt attempts probably likely
    """.split()
)

CELL = 1.0


def observation(scene: str = "scene-a", position=(0.0, 0.0, 0.0), velocity=(0.0, 0.0, 0.0),
                alive_readers: list[str] | None = None, pose: bool = True, **readers) -> Observation:
    """One observation carrying the controlled pose and any declared reader values."""
    public: dict = {}
    if pose:
        public["controlled"] = {"object_id": "o", "path": "p", "position": list(position),
                                "velocity": None if velocity is None else list(velocity),
                                "source": "rigidbody"}
    if alive_readers is not None:
        public["alive_readers"] = list(alive_readers)
    public.update(readers)
    return Observation(session="s", seq=0, game_s=1.0,
                       wall_utc=datetime(2026, 11, 1, tzinfo=timezone.utc),
                       clock_mode=ClockMode.realtime, scene=scene, playing=True, paused=False,
                       time_scale=1.0, frame=0, public=public)


def outcome(option_id: str = "hop+x", terminated_by: str = "blocked",
            start: list[float] | None = None, end: list[float] | None = None,
            direction: list[float] | None = None, displacement: list[float] | None = None,
            scene: str | None = "scene-a") -> OptionOutcome:
    """One measured option outcome."""
    params = {} if direction is None else {"direction": list(direction)}
    if displacement is None and start is not None and end is not None:
        displacement = [b - a for a, b in zip(start, end)]
    return OptionOutcome(option_id=option_id, template="traverse", params=params,
                         start_position=None if start is None else list(start),
                         end_position=None if end is None else list(end),
                         displacement=displacement, terminated_by=terminated_by, duration_s=0.5,
                         scene=scene)


def still(memory: OutcomeMemory, count: int, position=(0.0, 0.0, 0.0), **kwargs) -> None:
    for _ in range(count):
        memory.observe(observation(position=position, **kwargs))


# --- cells -----------------------------------------------------------------------


def test_a_cell_is_the_floor_of_the_position_over_the_step_and_the_scene_is_in_the_key():
    memory = OutcomeMemory(CELL)
    for _ in range(3):
        memory.record(outcome(start=[0.5, 0.2, 0.0], displacement=[0.4, 0.0, 0.0],
                              direction=[1.0, 0.0, 0.0]))
    here = "here: 3 tried \u2014 blocked 3, best +0.4"
    assert memory.suffix("scene-a", [0.9, 0.0, 0.9], "hop+x") == here
    assert memory.suffix("scene-a", [-0.1, 0.9, 0.0], "hop+x") == "here: untried"  # floor(-0.1) is -1, not 0
    assert memory.suffix("scene-a", [1.5, 0.2, 0.0], "hop+x") == "here: untried"  # the next cell along
    assert memory.suffix("scene-b", [0.9, 0.0, 0.9], "hop+x") == "here: untried"  # another scene, another key

    # A negative coordinate floors down to its own cell, not to zero.
    memory.record(outcome(start=[-0.5, 0.2, 0.0], displacement=[0.4, 0.0, 0.0], direction=[1.0, 0.0, 0.0]))
    assert memory.suffix("scene-a", [-0.1, 0.9, 0.0], "hop+x") == "here: 1 tried \u2014 blocked 1, best +0.4"


def test_a_cell_steps_by_the_measured_step_not_by_one_world_unit():
    memory = OutcomeMemory(2.0)
    memory.record(outcome(start=[0.5, 0.0, 0.0]))
    assert memory.suffix("scene-a", [1.9, 1.9, 0.0], "hop+x").startswith("here: 1 tried")
    assert memory.suffix("scene-a", [2.0, 0.0, 0.0], "hop+x") == "here: untried"


def test_an_outcome_is_filed_where_it_started_not_where_it_ended():
    memory = OutcomeMemory(CELL)
    memory.record(outcome(terminated_by="succeeded", start=[0.1, 0.0, 0.0], end=[3.0, 0.0, 0.0]))
    assert memory.suffix("scene-a", [0.2, 0.0, 0.0], "hop+x") == "here: 1 tried \u2014 succeeded 1"
    assert memory.suffix("scene-a", [3.2, 0.0, 0.0], "hop+x") == "here: untried"


def test_a_cell_size_must_be_a_positive_measured_step():
    for bad in (0.0, -1.0, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            OutcomeMemory(bad)


# --- suffix ----------------------------------------------------------------------


def test_the_suffix_lists_tries_and_terminations_in_order_with_the_signed_best():
    memory = OutcomeMemory(2.0)
    memory.record(outcome(terminated_by="blocked", start=[0.0, 0.0, 0.0],
                          displacement=[0.1, 0.0, 0.0], direction=[1.0, 0.0, 0.0]))
    memory.record(outcome(terminated_by="blocked", start=[0.2, 0.0, 0.0],
                          displacement=[-0.4, 0.0, 0.0], direction=[1.0, 0.0, 0.0]))
    memory.record(outcome(terminated_by="succeeded", start=[0.1, 0.0, 0.0],
                          displacement=[0.4, 0.0, 0.0], direction=[1.0, 0.0, 0.0]))
    assert memory.suffix("scene-a", [0.0, 0.0, 0.0], "hop+x") == \
        "here: 3 tried \u2014 succeeded 1, blocked 2, best +0.4"


def test_a_negative_best_is_signed_with_the_typographic_minus():
    memory = OutcomeMemory(CELL)
    memory.record(outcome(start=[0.0, 0.0, 0.0], displacement=[-0.45, 0.0, 0.0],
                          direction=[1.0, 0.0, 0.0]))
    assert memory.suffix("scene-a", [0.0, 0.0, 0.0], "hop+x") == \
        f"here: 1 tried \u2014 blocked 1, best {MINUS}0.5"


def test_best_is_omitted_when_no_try_carried_a_measured_direction_or_displacement():
    memory = OutcomeMemory(CELL)
    memory.record(outcome(terminated_by="fell", start=[0.0, 0.0, 0.0]))
    memory.record(outcome(terminated_by="fell", start=[0.1, 0.0, 0.0],
                          direction=[1.0, 0.0, 0.0]))  # direction but no displacement: still unknown
    assert memory.suffix("scene-a", [0.0, 0.0, 0.0], "hop+x") == "here: 2 tried \u2014 fell 2"


def test_an_untried_place_and_an_unmeasured_position_are_stated_not_scored():
    memory = OutcomeMemory(CELL)
    assert memory.suffix("scene-a", [0.0, 0.0, 0.0], "hop+x") == "here: untried"
    memory.record(outcome(start=[0.0, 0.0, 0.0]))
    assert memory.suffix("scene-a", None, "hop+x") == "here: position unmeasured"
    assert memory.suffix("scene-a", [float("nan"), 0.0, 0.0], "hop+x") == "here: position unmeasured"


def test_the_suffix_is_per_option_at_the_same_place():
    memory = OutcomeMemory(CELL)
    memory.record(outcome(option_id="hop+x", start=[0.0, 0.0, 0.0]))
    assert memory.suffix("scene-a", [0.0, 0.0, 0.0], "traverse+x") == "here: untried"


# --- unplaced --------------------------------------------------------------------


def test_an_outcome_without_a_start_position_only_counts_in_the_unplaced_tally():
    memory = OutcomeMemory(CELL)
    memory.record(outcome(terminated_by="unmeasured", start=None))
    memory.record(outcome(terminated_by="unmeasured", start=None))
    memory.record(outcome(terminated_by="timed_out", start=[float("nan"), 0.0, 0.0]))
    assert memory.suffix("scene-a", [0.0, 0.0, 0.0], "hop+x") == "here: untried"
    assert memory.cell_line("scene-a", [0.0, 0.0, 0.0]) is None
    assert memory.unplaced == {("scene-a", "hop+x"): {"timed_out": 1, "unmeasured": 2}}


# --- settled points --------------------------------------------------------------


def test_a_still_run_of_observations_makes_one_settled_point():
    memory = OutcomeMemory(CELL)
    for x in (0.0, 0.05, 0.1):
        memory.observe(observation(position=(x, 0.0, 0.0)))
    assert memory.settled_points("scene-a") == [[0.1, 0.0, 0.0]]
    assert memory.settled_points("scene-b") == []


def test_too_short_a_still_run_makes_none():
    memory = OutcomeMemory(CELL)
    memory.observe(observation(position=(0.0, 0.0, 0.0)))
    memory.observe(observation(position=(0.0, 0.0, 0.0)))
    assert memory.settled_points("scene-a") == []


def test_moving_observations_make_no_settled_point():
    memory = OutcomeMemory(CELL)
    for x in (0.0, 2.0, 4.0, 6.0):
        memory.observe(observation(position=(x, 0.0, 0.0)))
    assert memory.settled_points("scene-a") == []


def test_a_measured_speed_above_one_step_per_second_makes_no_settled_point():
    memory = OutcomeMemory(CELL)
    still(memory, 3, velocity=(1.5, 0.0, 0.0))
    assert memory.settled_points("scene-a") == []
    still(memory, 3, velocity=None)  # unknown velocity: not still, not moving either
    assert memory.settled_points("scene-a") == [[0.0, 0.0, 0.0]]


def test_a_dead_or_zero_alive_reader_makes_no_settled_point():
    dead = OutcomeMemory(CELL)
    still(dead, 3, alive_readers=["vitality"], vitality=0.0)
    assert dead.settled_points("scene-a") == []

    alive = OutcomeMemory(CELL)
    still(alive, 3, alive_readers=["vitality"], vitality=4.0)
    assert alive.settled_points("scene-a") == [[0.0, 0.0, 0.0]]


def test_an_undeclared_reader_disqualifies_nothing_unknown_is_not_dead():
    memory = OutcomeMemory(CELL)
    still(memory, 3, counter=0.0)  # declared alive? no: its zero is no evidence either way
    assert memory.settled_points("scene-a") == [[0.0, 0.0, 0.0]]


def test_a_scene_change_or_a_missing_pose_breaks_the_run():
    changed = OutcomeMemory(CELL)
    still(changed, 2, scene="scene-a")
    still(changed, 2, scene="scene-b")
    assert changed.settled_points("scene-a") == []
    assert changed.settled_points("scene-b") == []

    gap = OutcomeMemory(CELL)
    still(gap, 2)
    gap.observe(observation(pose=False))
    still(gap, 2)
    assert gap.settled_points("scene-a") == []


def test_one_settled_point_per_cell_and_the_latest_wins():
    memory = OutcomeMemory(CELL)
    still(memory, 3, position=(0.0, 0.0, 0.0))
    still(memory, 3, position=(0.5, 0.0, 0.0))
    still(memory, 3, position=(0.55, 0.05, 0.0))
    assert memory.settled_points("scene-a") == [[0.55, 0.05, 0.0]]


def test_settled_points_come_back_in_first_seen_order():
    memory = OutcomeMemory(CELL)
    still(memory, 3, position=(5.0, 0.0, 0.0))
    still(memory, 3, position=(0.0, 0.0, 0.0))
    assert memory.settled_points("scene-a") == [[5.0, 0.0, 0.0], [0.0, 0.0, 0.0]]


# --- the place fact ---------------------------------------------------------------


def test_the_place_fact_lists_every_option_at_this_cell_with_its_terms():
    memory = OutcomeMemory(CELL)
    for _ in range(3):
        memory.record(outcome(option_id="hop+x", start=[-5.5, -1.5, 0.0]))
    for _ in range(12):
        memory.record(outcome(option_id="traverse+x", start=[-5.2, -1.9, 0.0]))
    assert memory.cell_line("scene-a", [-5.1, -1.1, 0.0]) == (
        f"This place (cell ({MINUS}6, {MINUS}2, 0)): "
        "hop+x 3 tried, blocked 3; traverse+x 12 tried, blocked 12.")
    assert memory.cell_line("scene-b", [-5.1, -1.1, 0.0]) is None
    assert memory.cell_line("scene-a", [0.0, 0.0, 0.0]) is None
    assert memory.cell_line("scene-a", None) is None


def test_the_place_fact_states_each_term_in_order_and_keeps_mixed_terminations_apart():
    memory = OutcomeMemory(CELL)
    memory.record(outcome(option_id="hop+x", terminated_by="fell", start=[0.0, 0.0, 0.0]))
    memory.record(outcome(option_id="hop+x", terminated_by="succeeded", start=[0.0, 0.0, 0.0]))
    memory.record(outcome(option_id="hop+x", terminated_by="fell", start=[0.0, 0.0, 0.0]))
    assert memory.cell_line("scene-a", [0.0, 0.0, 0.0]) == (
        "This place (cell (0, 0, 0)): hop+x 3 tried, succeeded 1, fell 2.")


# --- rendering rules ---------------------------------------------------------------


def rendered(memory: OutcomeMemory) -> list[str]:
    """Every fact this memory can render, for the word and determinism checks."""
    return [memory.suffix("scene-a", position, option)
            for position in ([0.0, 0.0, 0.0], [5.0, 5.0, 5.0], None)
            for option in ("hop+x", "traverse-x", "hop+z", "wait:1.0")] + \
        [memory.cell_line("scene-a", position) or "" for position in ([0.0, 0.0, 0.0], None)]


def test_rendered_facts_state_quantities_only_without_advice_words():
    memory = OutcomeMemory(CELL)
    memory.record(outcome(option_id="hop+x", terminated_by="blocked", start=[0.0, 0.0, 0.0],
                          displacement=[0.1, 0.0, 0.0], direction=[1.0, 0.0, 0.0]))
    memory.record(outcome(option_id="hop+x", terminated_by="fell", start=[0.0, 0.0, 0.0]))
    memory.record(outcome(option_id="traverse-x", terminated_by="succeeded", start=[0.0, 0.0, 0.0]))
    words = {word.lower() for text in rendered(memory) for word in re.findall(r"[A-Za-z]+", text)}
    assert words & ADVICE == set()
    assert "tried" in words


def test_the_same_memory_renders_the_same_text_every_time():
    def build(order: list[str]) -> OutcomeMemory:
        memory = OutcomeMemory(CELL)
        for option in order:
            for _ in range(2):
                memory.record(outcome(option_id=option, terminated_by="blocked", start=[0.0, 0.0, 0.0],
                                      displacement=[0.25, 0.0, 0.0], direction=[1.0, 0.0, 0.0]))
        for position in ((0.0, 0.0, 0.0), (0.05, 0.0, 0.0), (0.1, 0.0, 0.0)):
            memory.observe(observation(position=position))
        return memory

    first, second = build(["hop+x", "traverse+x"]), build(["hop+x", "traverse+x"])
    assert rendered(first) == rendered(second)
    assert first.settled_points("scene-a") == second.settled_points("scene-a")
    assert first.cell_line("scene-a", [0.0, 0.0, 0.0]) == second.cell_line("scene-a", [0.0, 0.0, 0.0])
    # The place fact orders options, so the order they were recorded in does not change it.
    assert rendered(build(["traverse+x", "hop+x"])) == rendered(build(["hop+x", "traverse+x"]))


def test_a_body_frozen_by_a_stopped_game_clock_never_settles():
    """Death, pause and menus stop the clock in many games; a body frozen there is not a safe point."""
    from types import SimpleNamespace
    memory = OutcomeMemory(cell=0.5)
    for scale, paused in ((0.0, False), (1.0, True)):
        for _ in range(10):
            memory.observe(SimpleNamespace(scene='s', time_scale=scale, paused=paused,
                                           public={'controlled': {'position': [1.0, 2.0, 0.0], 'velocity': None}}))
    assert memory.settled_points('s') == []
    for _ in range(10):
        memory.observe(SimpleNamespace(scene='s', time_scale=1.0, paused=False,
                                       public={'controlled': {'position': [1.0, 2.0, 0.0], 'velocity': None}}))
    assert memory.settled_points('s') == [[1.0, 2.0, 0.0]]
