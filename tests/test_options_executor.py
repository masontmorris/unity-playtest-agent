"""The option executor against a fake side-view world: measured endings, always released.

Each test runs one `OptionSpec` the primitives built, and reads back the outcome the executor measured.
"""

from __future__ import annotations

import asyncio
import json
import re

from options_fake_world import FakeWorld, calibration, catalog
from playtest.runner.options.executor import OptionExecutor
from playtest.runner.options.model import TERMINATIONS, OptionOutcome, OptionSpec
from playtest.runner.options.primitives import option_specs, primitives_from_calibration


def build(spec_id: str, world: FakeWorld | None = None, *, run_speed: float = 4.0):
    world = world or FakeWorld()
    primitives = primitives_from_calibration(
        calibration(run_speed=run_speed),
        ["move_left", "move_right", "rise_up", "chord_right", "chord_left"],
        catalog=catalog(),
    )
    spec = next(item for item in option_specs(primitives) if item.option_id == spec_id)
    return world, primitives, spec


def run_spec(world: FakeWorld, primitives, spec: OptionSpec, **kwargs) -> OptionOutcome:
    executor = OptionExecutor(world, primitives, clock=world.clock, sleep=world.sleep, **kwargs)
    outcome = asyncio.run(executor.run(spec))
    assert_valid(outcome, spec)
    assert world.held == set(), "input was left held"
    assert world.stops and world.stops[-1] == "release"
    return outcome


def assert_valid(outcome: OptionOutcome, spec: OptionSpec) -> None:
    assert isinstance(outcome, OptionOutcome)
    assert outcome.terminated_by in TERMINATIONS
    assert outcome.option_id == spec.option_id and outcome.template == spec.template
    assert outcome.params == dict(spec.params)
    json.dumps(outcome.to_json())                       # JSON-ready for the trace
    stopped = [row for row in outcome.receipts if row.get("type") == "stopped"]
    assert stopped and stopped[-1]["release_verified"] is True


# --- traverse ----------------------------------------------------------------------------


def test_traverse_is_blocked_by_the_step():
    world, primitives, spec = build("traverse+x")
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by == "blocked"
    assert outcome.start_position is not None and outcome.end_position is not None
    assert 2.9 <= outcome.end_position[0] <= 3.1        # the step face, not through it
    assert outcome.duration_s is not None and outcome.duration_s > 0
    assert outcome.displacement is not None and outcome.displacement[0] > 2.5


def test_traverse_into_the_pit_ends_on_a_measured_state_change():
    world = FakeWorld(pit_from=2.0, pit_to=3.0)
    _, primitives, spec = build("traverse+x")
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by in ("state_changed", "fell")
    assert outcome.end_position is not None and outcome.end_position[1] < -0.3


def test_traverse_succeeds_on_a_requested_distance():
    world = FakeWorld(step_x=None)
    _, primitives, spec = build("traverse+x")
    short = OptionSpec(option_id="traverse+x", template="traverse",
                       params={**spec.params, "distance": 1.0}, intent=spec.intent)
    outcome = run_spec(world, primitives, short)
    assert outcome.terminated_by == "succeeded"
    assert outcome.displacement is not None and outcome.displacement[0] >= 1.0


def test_traverse_times_out_when_it_keeps_moving_and_never_arrives():
    world = FakeWorld(step_x=None)
    _, primitives, spec = build("traverse+x")
    far = OptionSpec(option_id="traverse+x", template="traverse",
                     params={**spec.params, "distance": 100.0}, intent=spec.intent)
    outcome = run_spec(world, primitives, far)
    assert outcome.terminated_by == "timed_out"
    assert outcome.duration_s is not None and outcome.duration_s < 7.0


def test_retreat_moves_the_other_way():
    world = FakeWorld(step_x=None, x=5.0)
    _, primitives, spec = build("retreat+x:4.0")
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by == "succeeded"
    assert outcome.displacement is not None and outcome.displacement[0] <= -4.0


# --- hop ---------------------------------------------------------------------------------


def test_hop_with_run_up_clears_the_step():
    world, primitives, spec = build("hop+x")
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by == "succeeded", outcome.receipts
    assert outcome.displacement is not None and outcome.displacement[0] > 3.0
    assert outcome.end_position is not None and outcome.end_position[1] > 0.5   # landed on the step
    actions = [row["action"] for row in world.requests]
    # Advance until the step stalls the body, back up the run-up from there, then the chord.
    assert actions[:2] == ["move_right", "move_left"]
    assert "chord_right" in actions                      # move and rise in one declaration
    chord = next(row for row in world.requests if row["action"] == "chord_right")
    assert chord["lease_s"] and chord["lease_s"] >= 1.0  # the lease covers the flight


def test_hop_with_zero_run_up_cannot_clear_the_step():
    world = FakeWorld(x=2.9)
    _, primitives, spec = build("hop+x")
    flat = OptionSpec(option_id="hop+x", template="hop",
                      params={**spec.params, "run_up": 0.0, "launch_distance": 0.0},
                      intent=spec.intent)
    outcome = run_spec(world, primitives, flat, dead_fields={"dead": True})
    assert outcome.terminated_by == "blocked", outcome.receipts
    assert outcome.displacement is not None and outcome.displacement[0] < 0.5
    assert "chord_right" in [row["action"] for row in world.requests]  # press and rise together


def test_hop_without_a_declared_chord_is_rejected_with_the_reason():
    world, primitives, spec = build("hop+x")
    plain = OptionSpec(option_id="hop+x", template="hop",
                       params={**spec.params, "chord": None}, intent=spec.intent)
    outcome = run_spec(world, primitives, plain)
    assert outcome.terminated_by == "rejected"
    notes = [row for row in outcome.receipts if row.get("type") == "option_note"]
    assert notes and "chord" in notes[0]["reason"]
    assert not world.requests                          # it sent nothing at all


# --- endings -----------------------------------------------------------------------------


def test_a_rejected_input_ends_the_option_rejected():
    world = FakeWorld(reject_acts=1)
    _, primitives, spec = build("traverse+x")
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by == "rejected"
    rejected = [row for row in outcome.receipts if row.get("status") == "rejected"]
    assert rejected and rejected[0]["reject_reason"] == "stale_age"


def test_no_pose_is_unmeasured():
    world = FakeWorld(publish_pose=False)
    _, primitives, spec = build("traverse+x")
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by == "unmeasured"
    assert outcome.start_position is None and outcome.end_position is None
    assert outcome.displacement is None
    assert not world.requests


def test_the_scene_changing_ends_the_option():
    world, primitives, spec = build("traverse+x")
    original = world.observe_once

    async def change_scene_after_three():
        observation = await original()
        if observation.seq > 3:
            return observation.model_copy(update={"scene": "fixture:other"})
        return observation

    world.observe_once = change_scene_after_three        # type: ignore[method-assign]
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by == "state_changed"
    assert outcome.scene == "fixture:scene"


def test_wait_holds_nothing_and_succeeds():
    world, primitives, spec = build("wait:0.5")
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by == "succeeded"
    assert outcome.duration_s is not None and outcome.duration_s >= 0.5
    assert not world.requests


def test_place_reports_what_the_physics_did():
    world, primitives, _spec = build("wait:0.25")
    spec = OptionSpec(option_id="place:1", template="place",
                      params={"position": [1.0, 0.0, 0.0], "settle_s": 0.25}, intent="place")
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by == "succeeded"
    assert outcome.end_position == [1.0, 0.0, 0.0]
    placed = [row for row in outcome.receipts if row.get("type") == "place"]
    assert placed and placed[0]["placed"] is True and placed[0]["moved_during_settle"] is not None


def test_declared_death_reader_ends_the_option():
    world, primitives, spec = build("traverse+x")
    original = world.observe_once

    async def dying_after_three():
        observation = await original()
        if observation.seq > 3:
            return observation.model_copy(update={
                "public": {**observation.public, "vitals": {"dead": True}}
            })
        return observation

    world.observe_once = dying_after_three             # type: ignore[method-assign]
    outcome = run_spec(world, primitives, spec, dead_fields={"vitals.dead": True})
    assert outcome.terminated_by == "state_changed"


def test_hop_started_against_the_step_clears_it_after_its_run_up():
    """A hop is chosen facing what it should clear, so it starts pressed against the obstacle: it must back up,
    run in, and take off short of the obstacle rather than at it."""
    world = FakeWorld(x=2.9)
    _, primitives, spec = build("hop+x")
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by == "succeeded", outcome.receipts
    assert outcome.end_position is not None and outcome.end_position[1] > 0.5   # on top of the step
    assert outcome.end_position[0] > 3.0


def test_hop_toward_a_gap_takes_off_where_the_floor_ends():
    """No wall stalls the body at a gap; the hop launches the moment the floor ends."""
    world = FakeWorld(step_x=None, pit_from=2.0, pit_to=5.0, coyote_s=0.15)
    _, primitives, spec = build("hop+x", world)
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by == "succeeded", outcome.receipts
    assert outcome.end_position is not None and outcome.end_position[0] > 5.0 and outcome.end_position[1] > -0.1


def test_hop_toward_a_gap_in_a_game_without_coyote_time_falls_honestly():
    world = FakeWorld(step_x=None, pit_from=2.0, pit_to=5.0, coyote_s=0.0)
    _, primitives, spec = build("hop+x", world)
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by in ("fell", "state_changed"), outcome.receipts


def test_hop_keeps_its_run_up_on_the_floor_it_started_on():
    """live-13: a hop backed up its whole run-up, walked off the ledge it had just climbed and failed from
    below, seven times. The floor behind is measured, so the run-up stops at its edge and the note says how
    much run-up was actually available."""
    # A three-unit top with a drop behind it and a taller face ahead; the body starts on the top.
    world = FakeWorld(x=2.6, y=1.0, step_x=0.0, step_width=3.0, step_height=1.0,
                      wall_x=3.0, wall_height=2.0, pit_from=-9.0, pit_to=0.0)
    _, primitives, spec = build("hop+x", world)
    heights: list[float] = []
    original = world.observe_once

    async def track():
        observation = await original()
        heights.append(observation.public["controlled"]["position"][1])
        return observation

    world.observe_once = track            # type: ignore[method-assign]
    outcome = run_spec(world, primitives, spec)

    notes = [row["reason"] for row in outcome.receipts if row.get("type") == "option_note"]
    available = [note for note in notes if "available of" in note and "(floor edge at" in note]
    assert available, notes
    match = re.search(r"run-up ([\d.]+) available of ([\d.]+) \(floor edge at (-?[\d.]+) behind", available[0])
    assert match, available[0]
    assert 0.0 < float(match.group(1)) <= 3.0, "the run-up stayed on the three-unit top"
    assert float(match.group(2)) > 3.0, "the run-up it asked for was longer than the top"
    assert 0.0 <= float(match.group(3)) < float(match.group(1)), "the edge is reported inside the run-up it cut short"
    assert min(heights) >= 1.0 - 1e-6, "the hop never left the top it started on"
    assert outcome.terminated_by == "succeeded", outcome.receipts
    assert "chord_right" in [row["action"] for row in world.requests], "the shortened run-up still hopped"


def test_hop_without_a_probe_stops_the_run_up_when_the_body_walks_off():
    """With no probe behind, the body leaving the floor is the measurement: the back-up stops there, says
    how far it got, and the hop carries on from where it ended up instead of silently running up from below."""
    world = FakeWorld(x=2.6, y=1.0, step_x=0.0, step_width=3.0, step_height=1.0,
                      wall_x=3.0, wall_height=2.0, publish_probe=False)
    _, primitives, spec = build("hop+x", world)
    outcome = run_spec(world, primitives, spec)

    notes = [row["reason"] for row in outcome.receipts if row.get("type") == "option_note"]
    truncated = [note for note in notes if note.startswith("run-up truncated: floor ended after")]
    assert truncated, notes
    match = re.search(r"floor ended after ([\d.]+)$", truncated[0])
    assert match, truncated[0]
    assert 2.9 <= float(match.group(1)) < spec.params["run_up"], "the back-up stopped at the edge of the top"
    assert "available of" not in " ".join(notes), "no probe took a reading, so none is claimed"
    # It went on: the back-up ended the phase, not the option, and the hop ran the approach from there.
    assert [row["action"] for row in world.requests] == ["move_right", "move_left", "move_right"]
    assert outcome.terminated_by in ("blocked", "fell", "timed_out", "state_changed"), outcome.receipts


def test_a_fast_run_up_with_a_stale_probe_stops_on_the_floor():
    """live-15: at a measured speed of several units per observation, with a probe sample that refreshes
    less often than the body moves, reading only the nearest backward ray stopped the back-up after the
    body had already left the ledge. Every ray inside the distance the body can travel before the probe
    refreshes is read, so the stop lands on the floor."""
    world = FakeWorld(x=2.6, y=1.0, step_x=0.0, step_width=3.0, step_height=1.0,
                      wall_x=3.0, wall_height=2.0, pit_from=-9.0, pit_to=0.0,
                      run_speed=12.0, probe_every=2)
    _, primitives, spec = build("hop+x", world, run_speed=12.0)
    heights: list[float] = []
    original = world.observe_once

    async def track():
        observation = await original()
        heights.append(observation.public["controlled"]["position"][1])
        return observation

    world.observe_once = track            # type: ignore[method-assign]
    outcome = run_spec(world, primitives, spec)

    notes = [row["reason"] for row in outcome.receipts if row.get("type") == "option_note"]
    edge = [note for note in notes if "available of" in note and "(floor edge at" in note]
    assert edge, notes
    match = re.search(r"run-up ([\d.]+) available of ([\d.]+) \(floor edge at (-?[\d.]+) behind", edge[0])
    assert match, edge[0]
    assert float(match.group(2)) > 3.0, "the run-up it asked for was longer than the top"
    assert min(heights) >= 1.0 - 1e-6, "the back-up stopped while the body was still on the top"
    assert not any(note.startswith("run-up truncated") for note in notes), notes


def test_a_hop_over_a_floor_that_continues_behind_runs_its_whole_run_up():
    """Every backward ray inside the stopping distance hits this floor, so nothing cuts the run-up short."""
    world = FakeWorld(x=2.6, y=1.0, step_x=-50.0, step_width=100.0, step_height=1.0,
                      wall_x=3.0, wall_height=2.0)
    _, primitives, spec = build("hop+x", world)
    xs: list[float] = []
    original = world.observe_once

    async def track():
        observation = await original()
        xs.append(observation.public["controlled"]["position"][0])
        return observation

    world.observe_once = track            # type: ignore[method-assign]
    outcome = run_spec(world, primitives, spec)

    notes = [row["reason"] for row in outcome.receipts if row.get("type") == "option_note"]
    assert not any("floor edge" in note or "truncated" in note for note in notes), notes
    assert min(xs) <= 3.0 - spec.params["run_up"] + 0.1, "the back-up ran the whole run-up the spec asked for"


def _jittered(world):

    """A real clock is never on the observation grid: each read lands a little after the physics step."""
    reads = {"n": 0}
    def clock():
        reads["n"] += 1
        return world.now + 0.0007 * (reads["n"] % 7)
    return clock


def test_a_stall_is_detected_under_a_jittery_clock():
    """live-11: with timestamps off the observation grid the stall window never filled, so a body pressed against
    a wall timed out instead of ending blocked."""
    world = FakeWorld(x=2.9)
    _, primitives, spec = build("traverse+x")
    executor = OptionExecutor(world, primitives, clock=_jittered(world), sleep=world.sleep)
    outcome = asyncio.run(executor.run(spec))
    assert outcome.terminated_by == "blocked", outcome.receipts


def test_hop_against_the_step_clears_it_under_a_jittery_clock():
    world = FakeWorld(x=2.9)
    _, primitives, spec = build("hop+x")
    executor = OptionExecutor(world, primitives, clock=_jittered(world), sleep=world.sleep)
    outcome = asyncio.run(executor.run(spec))
    assert outcome.terminated_by == "succeeded", outcome.receipts
    assert outcome.end_position is not None and outcome.end_position[0] > 3.0 and outcome.end_position[1] > 0.5


def test_a_hop_measures_its_flight_and_later_hops_use_it():
    """The take-off lead starts from the symmetric-flight assumption; once a flight is measured, the measured
    time to apex is used instead, and each hop's note states its take-off, obstacle and apex."""
    flight: dict = {}
    world = FakeWorld(x=2.9)
    _, primitives, spec = build("hop+x")
    executor = OptionExecutor(world, primitives, clock=world.clock, sleep=world.sleep, flight=flight)
    outcome = asyncio.run(executor.run(spec))
    assert outcome.terminated_by == "succeeded", outcome.receipts
    assert len(flight["time_to_apex_s"]) == 1 and flight["time_to_apex_s"][0] > 0
    note = next(r["reason"] for r in outcome.receipts if r.get("type") == "option_note")
    assert "measured obstacle at" in note and "took off" in note and "apex" in note
    second = OptionExecutor(FakeWorld(x=2.9), primitives, flight=flight)
    assert second._measured_apex_s() == flight["time_to_apex_s"][0]
