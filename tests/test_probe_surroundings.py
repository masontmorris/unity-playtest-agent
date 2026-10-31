from __future__ import annotations

import asyncio
from types import SimpleNamespace

from playtest.contracts.loop import Choice
from playtest.runner.probe import DisplacementProbe, describe_surroundings

STEP = {"blockers": [{"height": 0.15, "max_distance": 1.8, "hit": True, "distance": 0.375},
                     {"height": 0.9, "max_distance": 1.8, "hit": False, "distance": None}],
        "samples": [{"forward": 0.6, "lateral": -0.35, "delta_y": 0.36}, {"forward": 0.6, "lateral": 0.0, "delta_y": 0.3},
                    {"forward": 1.2, "lateral": 0.0, "delta_y": None}, {"forward": 1.8, "lateral": 0.0, "delta_y": 0.9}]}


def test_the_surroundings_line_states_measurements_and_unknowns_only():
    line = describe_surroundings(STEP)
    assert line == ("a cast 0.15 above the feet hit a surface 0.38 ahead; a cast 0.9 above the feet was clear for 1.8 "
                    "ahead; ground height against the ground under the feet: 0.6 ahead +0.30 to +0.36, 1.2 ahead "
                    "unknown, 1.8 ahead +0.90")
    assert describe_surroundings(None) is None and describe_surroundings({}) is None
    assert describe_surroundings({"blockers": [{"height": 0.15, "hit": True, "distance": None}]}) == \
        "a cast 0.15 above the feet hit a surface at unknown distance"


def test_the_probe_attaches_the_bound_observations_surroundings_to_fact_and_comparison():
    positions = iter([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])

    async def snapshot():
        return {"position": next(positions), "inspection_seq": 1, "game_time_s": 1.0}

    expected = {"move_forward": {"net_displacement": 2.0, "sample_variance": 0.0001, "requested_hold_s": 0.4,
                                 "valid_trials": 2}}
    probe = DisplacementProbe(snapshot, expected)
    probe.bind_observation_source(lambda: SimpleNamespace(public={"probe": STEP}))
    move = Choice(id="move_forward", kind="action", label="Move", action="move_forward", source="manifest")
    execution = SimpleNamespace(terminal=SimpleNamespace(applied_duration_s=0.4))

    async def run():
        await probe.before_turn(0)
        return await probe.measure(move, execution)

    report = asyncio.run(run())
    assert report.comparison is not None and report.comparison.context == describe_surroundings(STEP)
    assert report.fact.endswith(f" Before the action: {describe_surroundings(STEP)}.")


def test_without_a_source_or_readings_nothing_is_added():
    async def snapshot():
        return {"position": [0.0, 0.0, 0.0], "inspection_seq": 1, "game_time_s": 1.0}

    expected = {"move_forward": {"net_displacement": 2.0, "sample_variance": 0.0001, "requested_hold_s": 0.4,
                                 "valid_trials": 2}}
    move = Choice(id="move_forward", kind="action", label="Move", action="move_forward", source="manifest")
    execution = SimpleNamespace(terminal=SimpleNamespace(applied_duration_s=0.4))
    for source in (None, lambda: SimpleNamespace(public={}), lambda: None):
        probe = DisplacementProbe(snapshot, expected)
        if source is not None:
            probe.bind_observation_source(source)

        async def run():
            await probe.before_turn(0)
            return await probe.measure(move, execution)

        report = asyncio.run(run())
        assert report.comparison.context is None and "Before the action" not in report.fact
