"""Measured guards: each verb at rest, while moving and in the air, against a baseline of each state."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from playtest.contracts.action_catalog import ActionDefinition, InputBinding
from playtest.runner import guards


def key(control: str) -> ActionDefinition:
    return ActionDefinition(backend="input_system", inputs=[InputBinding(device="Keyboard", control=control)],
                            default_lease_s=0.3)


def direction(action: str, unit) -> SimpleNamespace:
    return SimpleNamespace(action=action, unit=unit, alternatives=())


CATALOG = {"left": key("a"), "right": key("d"), "jump": key("space"), "dash": key("leftshift"),
           "aim": key("e"), "attack": key("j")}
PRIMITIVES = SimpleNamespace(directions=[direction("right", (1.0, 0.0, 0.0)), direction("left", (-1.0, 0.0, 0.0))],
                             body_directions=(), rise=SimpleNamespace(action="jump"), rise_duration_s=0.6,
                             air_press_s=0.2, classified=())


def test_the_plan_finds_verbs_by_their_inputs_and_chords_each_with_both_directions():
    plan = guards.plan_guards(CATALOG, PRIMITIVES)
    assert plan["verbs"] == ["dash", "aim", "attack"] and plan["moves"] == ["right", "left"] and plan["rise"] == "jump"
    names = [c.name for c in plan["chords"]]
    assert names == ["g_right_dash", "g_left_dash", "g_right_aim", "g_left_aim", "g_right_attack", "g_left_attack"]
    dash = plan["chords"][0].definition
    assert [(i.device, i.control) for i in dash.inputs] == [("Keyboard", "d"), ("Keyboard", "leftshift")]
    # A verb that shares a control with movement (a running jump) is not a verb; neither is a long name kept long.
    catalog = {**CATALOG, "run_jump": ActionDefinition(backend="input_system", default_lease_s=0.3,
                                                       inputs=[InputBinding(device="Keyboard", control="d"),
                                                               InputBinding(device="Keyboard", control="space")])}
    assert "run_jump" not in guards.plan_guards(catalog, PRIMITIVES)["verbs"]
    assert guards.chord_name("m" * 30, "v" * 30).startswith("g_") and len(guards.chord_name("m" * 30, "v" * 30)) <= 40


class Game:
    """A side-view body: left/right move it, jump puts it in the air for 0.6 s, a timer ticks by itself.

    dash adds speed only while a direction is held; aim zooms only while held; attack only fires on the ground.
    """

    def __init__(self) -> None:
        self.now, self.x, self.air_until, self.held, self.shots = 0.0, 0.0, -1.0, None, 0
        self.last_shot = 0.0
        self.seq = 0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.step(seconds)

    def step(self, seconds: float) -> None:
        controls = self.controls()
        speed = (4.0 if "d" in controls else -4.0 if "a" in controls else 0.0)
        if speed and "leftshift" in controls:
            speed *= 2.0
        self.x += speed * seconds
        self.now += seconds

    def controls(self) -> set[str]:
        if self.held is None:
            return set()
        return {i.control for i in self.catalog[self.held].inputs}

    async def start_act(self, *, action, action_id, lease_s, **_):
        self.held = action
        controls = self.controls()
        if "space" in controls and self.now >= self.air_until:
            self.air_until = self.now + 0.6
        if "j" in controls and self.now >= self.air_until:
            self.shots += 1
            self.last_shot = self.now
        return [SimpleNamespace(status=SimpleNamespace(value="running"))]

    async def stop(self, reason):
        self.held = None

    async def collect_act(self, action_id):
        return [SimpleNamespace(status=SimpleNamespace(value="completed"))]

    async def observe_once(self):
        self.seq += 1
        self.step(0.01)
        return SimpleNamespace(seq=self.seq, public={"controlled": {"position": [self.x, 0.0, 0.0]},
                                                     "camera": {"field_of_view": 50.0 if "e" in self.controls() else 65.0}})

    async def read(self):
        return SimpleNamespace(values={"timer": round(self.now, 2), "shots": self.shots,
                                       "since_shot": round(self.now - self.last_shot, 2),
                                       "airborne": self.now < self.air_until})


def test_each_verb_is_measured_against_the_baseline_of_its_state():
    game = Game()
    game.catalog = {**CATALOG, **{c.name: c.definition for c in guards.plan_guards(CATALOG, PRIMITIVES)["chords"]}}
    table = asyncio.run(guards.measure_guards(game, game, game.catalog, PRIMITIVES, clock=game.clock, sleep=game.sleep))
    verbs = table["verbs"]
    assert "timer" in verbs["dash"]["ambient"]                    # it changes by itself, so it is never an effect

    dash = verbs["dash"]
    assert dash["rest"]["effect"] is False                        # alone, dash does nothing: the guard
    assert dash["moving"]["right"]["effect"] is True and dash["moving"]["right"]["moved_differently"] is True
    assert dash["moving"]["left"]["effect"] is True

    aim = verbs["aim"]
    assert aim["rest"]["effect"] is True and "camera.field_of_view" in aim["rest"]["changed"]   # seen while held

    attack = verbs["attack"]
    assert attack["rest"]["effect"] is True and "shots" in attack["rest"]["changed"]
    assert "since_shot" in attack["ambient"] and "since_shot" in attack["rest"]["changed"]   # counts up, reset by it
    assert "since_shot" not in dash["rest"]["changed"]                                          # its own clock only
    assert attack["air"]["effect"] is False                       # pressed in the air: no shot

    lines = guards.summary_lines(table)
    assert any(line.startswith("dash: at rest: no effect; while right held: movement") for line in lines)
    assert any("attack:" in line and "in the air: no effect" in line for line in lines)


def test_a_chord_the_host_does_not_serve_is_named_not_guessed():
    game = Game()
    game.catalog = dict(CATALOG)                                   # no generated chords served
    table = asyncio.run(guards.measure_guards(game, game, game.catalog, PRIMITIVES, clock=game.clock, sleep=game.sleep))
    assert table["verbs"]["dash"]["moving"] == {}
    assert any("g_right_dash is not in the served catalog" in note for note in table["notes"])


def test_an_effect_seen_while_one_direction_is_held_and_not_its_opposite_is_an_asymmetry_and_tallies():
    cell = lambda changed, moved=False: {"effect": bool(changed) or moved, "changed": changed, "moved_differently": moved}
    table = {"moves": ["back", "forward"], "verbs": {
        "dash": {"moving": {"back": cell({"blend": [0, 1]}, True), "forward": cell({}, False)}},
        "aim": {"moving": {"back": cell({"zoom": [65, 50]}), "forward": cell({"zoom": [65, 50]})}}}}
    found = guards.asymmetries(table)
    assert [(a["verb"], a["key"], a["present_while"]) for a in found] == [("dash", "blend", "back"),
                                                                           ("dash", "displacement", "back")]
    assert "dash changed blend while back was held, and not while forward" in guards.asymmetry_line(found[0])
    seeded = guards.tally([table, table, {"moves": ["back", "forward"], "verbs": {}}])
    assert seeded["n"] == 3 and {a["k"] for a in seeded["asymmetries"]} == {2}
    control = guards.tally([{"moves": ["back", "forward"], "verbs": {}}] * 3)
    rows = guards.compare_tallies(seeded, control)
    assert {r["verdict"] for r in rows} == {"specific"} and rows[0]["control_k"] == 0
    rule = guards.compare_tallies(seeded, seeded)
    assert {r["verdict"] for r in rule} == {"not_specific"}


def test_only_a_chord_that_does_what_its_verb_alone_does_not_is_offered_to_play():
    table = {"moves": ["right", "left"], "verbs": {
        "dash": {"rest": {"changed": {}}, "moving": {"right": {"changed": {"dashing": [0, 1]}, "moved_differently": True},
                                                      "left": {"changed": {}, "moved_differently": False}}},
        "aim": {"rest": {"changed": {"zoom": [65, 50]}},
                "moving": {"right": {"changed": {"zoom": [65, 50]}, "moved_differently": False}}}}}
    chords = [guards.GuardChord("g_right_dash", "right", "dash", None), guards.GuardChord("g_left_dash", "left", "dash", None),
              guards.GuardChord("g_right_aim", "right", "aim", None)]
    offered = guards.unlocking_chords(table, chords, {"dash": "dash sprint: hold shift", "right": "move right: hold d"})
    assert [(c.name, label) for c, label in offered] == [
        ("g_right_dash", "dash sprint while move right: both held together; measured before play: dashing changed; "
                         "the body moved differently from the direction alone")]
