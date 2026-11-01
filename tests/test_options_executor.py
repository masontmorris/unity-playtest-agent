"""The option executor against a fake side-view world: measured endings, always released.

Each test runs one `OptionSpec` the primitives built, and reads back the outcome the executor measured.
"""

from __future__ import annotations

import asyncio
import json

from options_fake_world import FakeWorld, calibration, catalog
from playtest.runner.options.executor import OptionExecutor
from playtest.runner.options.model import TERMINATIONS, OptionOutcome, OptionSpec
from playtest.runner.options.primitives import option_specs, primitives_from_calibration


def build(spec_id: str, world: FakeWorld | None = None):
    world = world or FakeWorld()
    primitives = primitives_from_calibration(
        calibration(),
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
    assert actions[0] == "move_left"                    # the run-up
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
