"""The option executor against a fake side-view world: measured endings, always released.

Each test runs one `OptionSpec` the primitives built, and reads back the outcome the executor measured.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from options_fake_world import (
    FakeWorld,
    calibration,
    catalog,
    lagged_pose_world,
    slow_start_world,
    tall_wall_world,
)
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
    executor = OptionExecutor(world, primitives, clock=world.clock, sleep=world.sleep,
                              wall_clock=world.wall_clock, **kwargs)
    outcome = asyncio.run(executor.run(spec))
    assert_valid(outcome, spec)
    assert world.held == set(), "input was left held"
    assert world.stops and world.stops[-1] == "release"
    return outcome


def assert_valid(outcome: OptionOutcome, spec: OptionSpec) -> None:
    assert isinstance(outcome, OptionOutcome)
    assert outcome.terminated_by in TERMINATIONS
    assert outcome.option_id == spec.option_id and outcome.template == spec.template
    # params are exactly the spec's; what the option measured about the place lives on `outcome.measured`.
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
    """A stale reading is re-bound once; rejected twice running, the option ends rejected."""
    world = FakeWorld(reject_acts=2)
    _, primitives, spec = build("traverse+x")
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by == "rejected"
    rejected = [row for row in outcome.receipts if row.get("status") == "rejected"]
    assert len(rejected) == 2 and rejected[0]["reject_reason"] == "stale_age"


def test_one_stale_rejection_is_sent_once_more_on_a_fresh_reading():
    world = FakeWorld(reject_acts=1)
    _, primitives, spec = build("traverse+x")
    outcome = run_spec(world, primitives, spec)
    assert outcome.terminated_by != "rejected"
    assert any("sent once more" in row.get("reason", "") for row in outcome.receipts
               if row.get("type") == "option_note")


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


@pytest.mark.parametrize("floor_behind", ["pit", "lower_floor"])
def test_hop_keeps_its_run_up_on_the_floor_it_started_on(floor_behind):
    """live-13: a hop backed up its whole run-up, walked off the ledge it had just climbed and failed from
    below, seven times. The floor behind is measured, so the run-up stops at its edge and the note says how
    much run-up was actually available."""
    # A lower surface is also the end of this floor, even when the drop is smaller than a jump.
    # The live probe hits the lower surface; a pit alone only exercises a ray that misses.
    pit = {"pit_from": -9.0, "pit_to": 0.0} if floor_behind == "pit" else {}
    world = FakeWorld(x=2.6, y=1.0, step_x=0.0, step_width=3.0, step_height=1.0,
                      wall_x=3.0, wall_height=2.0, **pit)
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


# --- the take-off (docs/design/borrowed-from-autopilot.md, item 1) ------------------------

LEAD = re.compile(r"lead ([\d.]+) = ([\d.]+) run to apex \+ ([\d.]+) latency "
                  r"\(pose age ([\d.]+) s \+ half of a ([\d.]+) s period\)")
TOOK_OFF = re.compile(r"took off (-?[\d.]+) u from it")


def note_rows(outcome: OptionOutcome) -> list[str]:
    return [row["reason"] for row in outcome.receipts if row.get("type") == "option_note"]


def lead_of(outcome: OptionOutcome) -> tuple[float, float, float, float, float]:
    """(lead, run to apex, latency, pose age, period) as the outcome's own note states them."""
    for note in note_rows(outcome):
        match = LEAD.search(note)
        if match:
            return tuple(float(group) for group in match.groups())    # type: ignore[return-value]
    raise AssertionError(note_rows(outcome))


def took_off_of(outcome: OptionOutcome) -> float:
    for note in note_rows(outcome):
        match = TOOK_OFF.search(note)
        if match:
            return float(match.group(1))
    raise AssertionError(note_rows(outcome))


def test_the_latency_term_moves_the_take_off_earlier_by_the_measured_age():
    """The press is aimed one apex run before the obstacle plus the distance the body covers while the decision
    is being made: the measured age of the pose it read and half an observation for the press to land. A link
    that delivers the pose a step late therefore aims exactly `speed x (age + half a period)` further back, and
    the press lands on the next observation tick, so the take-off point moves back with it."""
    speed = 4.0
    _, primitives, spec = build("hop+x")
    fresh = run_spec(FakeWorld(), primitives, spec)
    late = run_spec(lagged_pose_world(), primitives, spec)
    assert fresh.terminated_by == "succeeded" and late.terminated_by == "succeeded", (
        note_rows(fresh), note_rows(late))

    lead_fresh, lead_late = lead_of(fresh), lead_of(late)
    for lead in (lead_fresh, lead_late):
        total, run_to_apex, latency, age, period = lead
        assert total == pytest.approx(run_to_apex + latency, abs=1e-9)      # the note adds up
        assert run_to_apex == pytest.approx(speed * 0.5, abs=1e-6)          # speed x the time to apex
        assert latency == pytest.approx(speed * (age + 0.5 * period), abs=1e-6)
    assert lead_fresh[3] == pytest.approx(0.0, abs=1e-9)                    # a fresh pose has no age
    assert lead_late[3] == pytest.approx(lead_late[4], abs=1e-9)            # this one is exactly a step old
    assert lead_late[2] - lead_fresh[2] == pytest.approx(speed * lead_late[3], abs=1e-9)

    # The measured take-off point is the aim it read, on the tick it got there: one step of travel at most.
    step = speed * lead_late[4]
    assert took_off_of(fresh) < 0 and took_off_of(late) < 0
    assert abs((-took_off_of(fresh)) - lead_fresh[0]) <= step + 1e-9, note_rows(fresh)
    assert abs((-took_off_of(late)) - lead_late[0]) <= step + 1e-9, note_rows(late)
    assert abs((took_off_of(late) - took_off_of(fresh)) + (lead_late[0] - lead_fresh[0])) <= step + 1e-9


def test_a_hop_that_ran_past_its_take_off_point_retries_with_a_fresh_run_up():
    """A body that takes time to reach its run speed can arrive at its take-off point still accelerating. That is
    not a jump that measured the obstacle, so the attempt is given up and the hop backs up for a fresh run-up;
    the second attempt reaches speed in time and clears the step. Both attempts stay in the receipts."""
    world = slow_start_world()
    _, primitives, spec = build("hop+x")
    # The seek stalls at the step face, so this run-up leaves 2.6 u of run-in: a unit and half a second to
    # reach speed, then 0.6 u more, 0.65 s in all. That is still inside the window the measured speed needs
    # to stop rising, so the first run ends still accelerating; the retry backs up from where it stopped and
    # so gets a longer run-in.
    patient = OptionSpec(option_id="hop+x", template="hop",
                         params={**spec.params, "run_up": 2.6}, intent=spec.intent)
    outcome = run_spec(world, primitives, patient)

    notes = note_rows(outcome)
    assert outcome.terminated_by == "succeeded", notes
    assert any("attempt 1 of 3" in note for note in notes), notes
    assert any("attempt 2 of 3" in note for note in notes), notes
    retried = [note for note in notes if "ran past the take-off point" in note]
    assert retried and "fresh run-up" in retried[0], notes

    # Every attempt's inputs are in the receipts: seek, two run-ups, two run-ins, one rise.
    actions = [row["action"] for row in world.requests]
    assert actions == ["move_right", "move_left", "move_right", "move_left", "move_right", "chord_right"]
    acts = [row["action_id"] for row in outcome.receipts if row.get("action_id")]
    assert len(set(acts)) == len(actions) == 6, (acts, outcome.receipts)


def test_a_hop_against_a_wall_above_its_reach_measures_the_wall():
    """live-15: a hop that jumped and ended blocked against a wall never passed it, so the highest point it
    reached is a lower bound on the wall's height here. The note names the apex and params carries the same
    number, so the next label can compare it with what the rise reaches."""
    world = tall_wall_world()
    _, primitives, spec = build("hop+x")
    outcome = run_spec(world, primitives, spec)

    notes = note_rows(outcome)
    assert outcome.terminated_by == "blocked", notes
    assert outcome.end_position is not None and outcome.end_position[0] <= 3.0 + 0.3
    measured = outcome.measured
    assert isinstance(measured, dict) and "wall_min_height" in measured, outcome.measured
    height = measured["wall_min_height"]
    peak = primitives.rise_peak
    assert peak is not None and 0.5 * peak <= height <= peak + 0.1, (height, peak)
    wall = [note for note in notes if note.startswith("wall higher than")]
    assert wall, notes
    assert "apex" in wall[0] and f"+{height:.3f} u" in wall[0], wall[0]


def test_a_wall_already_known_to_be_out_of_reach_is_stated_and_the_hop_still_runs():
    """A bound the outcome memory carries (or the probe's own casts) is compared with the measured reach before
    the hop runs, so the next label can say why it was expected to be blocked. Code never refuses the hop S1
    chose: it states the expectation and runs it."""
    world = tall_wall_world(x=2.9)          # pressed against the wall, so the probe's casts both hit it
    _, primitives, spec = build("hop+x")
    reach = primitives.rise_peak + primitives.short_step
    known = round(reach + 1.0, 1)
    remembered = OptionSpec(option_id="hop+x", template="hop",
                           params={**spec.params, "wall_min_height": known}, intent=spec.intent)
    outcome = run_spec(world, primitives, remembered)

    notes = note_rows(outcome)
    pre = [note for note in notes if note.startswith("reach ")]
    assert pre, notes
    assert pre[0] == (f"reach {reach:.1f} < wall ≥ {known:.1f}: expected blocked "
                      f"(memory; the hop runs anyway)"), pre[0]
    assert [row["action"] for row in world.requests][:1] == ["move_right"], "the hop ran anyway"
    assert any("wall ≥0.9 (probe)" in note for note in notes), notes
    assert outcome.terminated_by == "blocked", notes


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


def test_an_interrupted_option_ends_interrupted_with_its_input_released_and_its_motion_measured():
    """S1 chose something else mid-move: the option stops at its next await, measured and released."""
    world = FakeWorld(step_x=None)
    _, primitives, spec = build("traverse+x")

    async def yielding(seconds):                 # a real bridge yields on every wait; the fake world does not
        await world.sleep(seconds)
        await asyncio.sleep(0)

    async def main():
        executor = OptionExecutor(world, primitives, clock=world.clock, sleep=yielding,
                                  wall_clock=world.wall_clock)
        task = asyncio.create_task(executor.run(spec))
        for _ in range(5):                        # the run starts holding the move
            await asyncio.sleep(0)
        executor.interrupt("S1 chose wait")
        return await task

    outcome = asyncio.run(main())
    assert_valid(outcome, spec)
    assert outcome.terminated_by == "interrupted"
    assert world.held == set() and world.stops[-1] == "release"
    assert any(row.get("reason") == "S1 chose wait" for row in outcome.receipts if row.get("type") == "option_note")


def test_an_interrupt_during_a_hops_flight_waits_for_the_landing():
    """Physics owns the flight: an interrupt sent while airborne lets the hop land and end on its own measure."""
    world = FakeWorld()
    _, primitives, spec = build("hop+x", world)

    async def yielding(seconds):
        await world.sleep(seconds)
        await asyncio.sleep(0)

    async def main():
        executor = OptionExecutor(world, primitives, clock=world.clock, sleep=yielding,
                                  wall_clock=world.wall_clock)
        task = asyncio.create_task(executor.run(spec))
        for _ in range(2000):
            if executor.committed or task.done():
                break
            await asyncio.sleep(0)
        assert executor.committed, "the hop never took off"
        executor.interrupt("S1 chose wait")
        return await task

    outcome = asyncio.run(main())
    assert_valid(outcome, spec)
    assert outcome.terminated_by in ("succeeded", "blocked")
    assert world.held == set()


def test_a_hop_that_presses_again_releases_then_presses_the_chord_once_more_in_the_air():
    world = FakeWorld()
    _, primitives, spec = build("hop+x", world)
    double = OptionSpec(option_id="hop2+x", template="hop",
                        params={**spec.params, "air_press_s": 0.2, "flight_s": 1.0}, intent=spec.intent)
    outcome = run_spec(world, primitives, double)
    chords = [row for row in world.requests if row["action"] == "chord_right"]
    assert len(chords) == 2                                   # take-off, then the press in the air
    assert any("pressed the rise again" in row.get("reason", "") for row in outcome.receipts
               if row.get("type") == "option_note")
    plain = run_spec(FakeWorld(), primitives, spec)
    assert "pressed the rise again" not in str(plain.receipts)


def test_a_sweep_presses_each_listed_control_once_and_ends_blocked_when_nothing_changes():
    world, primitives, _ = build("traverse+x")
    sweep = OptionSpec(option_id="sweep", template="sweep", params={"actions": ["rise_up", "move_left"]},
                       intent="try each untried control once")
    outcome = run_spec(world, primitives, sweep)
    assert outcome.terminated_by == "blocked"
    assert [row["action"] for row in outcome.measured["tried"]] == ["rise_up", "move_left"]
    assert [row["action"] for row in world.requests] == ["rise_up", "move_left"]


def test_a_sweep_ends_state_changed_at_the_first_control_the_scene_changes_on():
    world, primitives, _ = build("traverse+x")
    original, pressed = world.observe_once, []
    original_start = world.start_act

    async def start_act(**request):
        pressed.append(request["action"])
        return await original_start(**request)

    async def observe_once():
        observation = await original()
        return observation.model_copy(update={"scene": "fixture:next"}) if pressed[-1:] == ["move_left"] else observation

    world.start_act, world.observe_once = start_act, observe_once  # type: ignore[method-assign]
    sweep = OptionSpec(option_id="sweep", template="sweep", params={"actions": ["move_left", "rise_up"]},
                       intent="try each untried control once")
    outcome = run_spec(world, primitives, sweep)
    assert outcome.terminated_by == "state_changed" and "changed the scene" in str(outcome.receipts)
    assert [row["action"] for row in outcome.measured["tried"]] == ["move_left"]


def test_a_sweep_is_committed_so_an_interrupt_lets_its_presses_finish():
    world, primitives, _ = build("traverse+x")
    sweep = OptionSpec(option_id="sweep", template="sweep", params={"actions": ["rise_up", "move_left"]},
                       intent="try each untried control once")

    async def yielding(seconds):
        await world.sleep(seconds)
        await asyncio.sleep(0)

    async def main():
        executor = OptionExecutor(world, primitives, clock=world.clock, sleep=yielding, wall_clock=world.wall_clock)
        task = asyncio.create_task(executor.run(sweep))
        for _ in range(2000):
            if executor.committed or task.done():
                break
            await asyncio.sleep(0)
        assert executor.committed
        executor.interrupt("S1 chose wait")
        return await task

    outcome = asyncio.run(main())
    assert outcome.terminated_by == "blocked"
    assert [row["action"] for row in outcome.measured["tried"]] == ["rise_up", "move_left"]
