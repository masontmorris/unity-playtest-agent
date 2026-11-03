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


def test_each_press_says_how_far_it_was_from_each_situations_object():
    def at(choice, body, target):
        row = effect(choice, {"typing": False}, {"typing": False})
        row["body"]["observed"].update({"from_position": body, "targets_before": target})
        return row

    places = {"npc": ("situation_1", 3.0)}
    sink = Sink([at("interact", [0.0, 0.0, 0.0], {"npc": {"position": [2.0, 0.0, 0.0], "active": True}}),
                 at("interact", [0.0, 0.0, 0.0], {"npc": {"position": [10.0, 0.0, 0.0], "active": True}}),
                 at("interact", [0.0, 0.0, 0.0], {"npc": {"position": [1.0, 0.0, 0.0], "active": False}}),
                 at("interact", [0.0, 0.0, 0.0], {})])
    assert press_reading_lines(sink, places=places) == [
        "interact [situation_1 distance 2.00 (inside 3)]: no reader changed",
        "interact [situation_1 distance 10.00 (outside 3)]: no reader changed",
        "interact [situation_1 object inactive]: no reader changed",
        "interact: no reader changed"]
    assert press_reading_lines(sink)[0] == "interact: no reader changed"


def test_an_earlier_press_inside_a_situation_stays_shown():
    def at(choice, x):
        row = effect(choice, {}, {})
        row["body"]["observed"].update({"from_position": [x, 0.0, 0.0],
                                        "targets_before": {"npc": {"position": [0.0, 0.0, 0.0], "active": True}}})
        return row

    sink = Sink([at("interact", 1.0), at("jump", 9.0), *[at("move", 20.0)] * 3])
    lines = press_reading_lines(sink, limit=3, places={"npc": ("situation_1", 3.0)})
    assert lines[0] == "interact [situation_1 distance 1.00 (inside 3)]: no reader changed"
    assert len(lines) == 4 and all(line.startswith("move") for line in lines[1:])
