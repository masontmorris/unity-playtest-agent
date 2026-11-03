"""`--press-readings`: every reader read just before and just after each input, into its effect record.

What a press leaves behind is read at the press, not at S1's pace, and the play scorer uses those readings like any
other. Nothing S1 reads changes: the fact line is the same with and without them.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from playtest.contracts.loop import Choice
from playtest.runner import expectations
from playtest.runner.probe import DisplacementProbe


def probe_with(readers):
    positions = iter([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])

    async def snapshot():
        return {"position": next(positions), "inspection_seq": 1, "game_time_s": 1.0}

    return DisplacementProbe(snapshot, {}, readers=readers)


def run(probe):
    attack = Choice(id="attack", kind="action", label="Attack", action="attack", source="manifest")
    execution = SimpleNamespace(terminal=SimpleNamespace(applied_duration_s=0.2))

    async def go():
        await probe.before_turn(0)
        return await probe.measure(attack, execution)

    return asyncio.run(go())


def test_each_input_carries_every_reader_just_before_and_just_after_it():
    values = iter([{"shots": 0}, {"shots": 1}])

    class Readers:
        async def read(self):
            return SimpleNamespace(values=next(values))

    with_readings = run(probe_with(Readers()))
    observed = with_readings.observed
    assert observed["readers_before"]["values"] == {"shots": 0} and observed["readers_after"]["values"] == {"shots": 1}
    assert observed["readers_after"]["wall_s"] >= observed["readers_before"]["wall_s"]
    without = run(probe_with(None))
    assert "readers_after" not in without.observed and without.fact == with_readings.fact   # S1 reads the same


def test_a_reading_that_fails_is_unmeasured_and_the_scorer_reads_the_ones_that_were_taken():
    class Broken:
        async def read(self):
            raise RuntimeError("bridge closed")

    assert run(probe_with(Broken())).observed["readers_after"] is None
    rows = [{"kind": "observation", "trace_seq": 1, "body": {"wall_utc": 100.0, "public": {"shots": 0}}},
            {"kind": "effect", "trace_seq": 2, "body": {"observed": {
                "readers_before": {"wall_s": 100.4, "values": {"shots": 0}},
                "readers_after": {"wall_s": 100.7, "values": {"shots": 1}}}}},
            {"kind": "observation", "trace_seq": 3, "body": {"wall_utc": 101.5, "public": {"shots": 0}}}]
    assert [(t, values["shots"]) for t, _, values in expectations.play_readings(rows)] == [
        (100.0, 0), (100.4, 0), (100.7, 1), (101.5, 0)]
