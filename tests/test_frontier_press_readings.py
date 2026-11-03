"""The frontier is shown what each of the latest presses changed, read at the press."""

from __future__ import annotations

from types import SimpleNamespace

from playtest.runner.frontier_help import press_reading_lines


class Sink:
    def __init__(self, effects):
        self._effects = effects

    def of_kind(self, kind):
        return self._effects if kind == "effect" else []


def effect(choice, before, after):
    return {"kind": "effect", "body": {"choice_id": choice, "observed": {
        "readers_before": {"wall_s": 1.0, "values": before}, "readers_after": {"wall_s": 1.3, "values": after}}}}


def test_a_reset_the_observations_miss_is_stated_and_a_press_without_readings_is_left_out():
    sink = Sink([effect("attack", {"cooldown": 2.19, "dead": False}, {"cooldown": 0.06, "dead": False}),
                 effect("dash", {"cooldown": 0.3}, {"cooldown": 0.3}),
                 {"kind": "effect", "body": {"choice_id": "jump", "observed": {"net_displacement": 2.0}}}])
    assert press_reading_lines(sink) == ["attack: cooldown 2.19 -> 0.06", "dash: no reader changed"]
    assert press_reading_lines(Sink([effect("a", {}, {"x": 1})] * 20), limit=3) == ["a: no reader changed"] * 3
