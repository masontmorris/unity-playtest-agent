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
    rare = guards.tally([table, {"moves": ["back", "forward"], "verbs": {}}, {"moves": ["back", "forward"], "verbs": {}}])
    assert {r["verdict"] for r in guards.compare_tallies(rare, control)} == {"sporadic"}   # 1 of 3: noise-sized
    flaky = {"n": 3, "asymmetries": [{"verb": "aim", "key": "blend", "present_while": "back", "absent_while": "forward",
                                      "k": 1}]}
    assert {r["verdict"] for r in guards.compare_tallies(seeded, flaky)} == {"noisy_key", "specific"}
    mirrored = {"n": 3, "asymmetries": [{"verb": "dash", "key": "blend", "present_while": "forward",
                                         "absent_while": "back", "k": 3}]}
    assert {r["key"]: r["verdict"] for r in guards.compare_tallies(seeded, mirrored)}["blend"] == "specific"


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


def test_each_cell_keeps_what_a_scorer_needs_to_tell_a_hidden_effect_from_a_missing_one():
    game = Game()
    game.catalog = {**CATALOG, **{c.name: c.definition for c in guards.plan_guards(CATALOG, PRIMITIVES)["chords"]}}
    table = asyncio.run(guards.measure_guards(game, game, game.catalog, PRIMITIVES, clock=game.clock, sleep=game.sleep))
    dash = table["verbs"]["dash"]
    right = dash["moving"]["right"]
    assert len(right["starts"]) == 2 and "shots" in right["starts"][0]          # each trial's readings before it
    assert right["peak_displacement"] > right["baseline_peak_displacement"] > 0  # dash went further, at its furthest
    attack = table["verbs"]["attack"]
    assert attack["rest"]["baseline_changed"] == sorted(attack["rest"]["baseline_changed"])
    assert dash["rest"]["peak_displacement"] == 0.0


def test_the_in_air_press_waits_for_the_body_to_leave_the_ground():
    class Lagging(Game):
        """The rise lifts the body only 0.3 s after it is pressed, later than the planned apex press."""

        def step(self, seconds):
            super().step(seconds)
            self.y = 1.0 if self.air_until > 0 and self.now >= self.air_until - 0.6 + 0.3 and self.now < self.air_until else 0.0

        async def observe_once(self):
            observation = await super().observe_once()
            observation.public["controlled"]["position"][1] = getattr(self, "y", 0.0)
            return observation

    primitives = SimpleNamespace(**{**vars(PRIMITIVES), "air_press_s": 0.05})
    game = Lagging()
    game.catalog = dict(CATALOG)
    table = asyncio.run(guards.measure_guards(game, game, game.catalog, primitives, clock=game.clock, sleep=game.sleep))
    assert all(d > 0 for d in table["verbs"]["attack"]["air"]["pressed_at"])   # pressed once it had risen


def test_a_body_that_lifts_after_the_rise_is_released_is_still_pressed_in_the_air():
    class LateLift(Game):
        """The rise lifts the body from 0.7 s to 1.3 s after it is pressed, after its 0.6 s hold ends; attack fires
        only while the body is down."""

        def __init__(self):
            super().__init__()
            self.lift = (-1.0, -1.0)

        def y(self):
            return 1.0 if self.lift[0] <= self.now < self.lift[1] else 0.0

        async def start_act(self, *, action, action_id, lease_s, **_):
            self.held = action
            controls = self.controls()
            if "space" in controls and self.y() == 0.0:
                self.lift = (self.now + 0.7, self.now + 1.3)
            if "j" in controls and self.y() == 0.0:
                self.shots += 1
                self.last_shot = self.now
            return [SimpleNamespace(status=SimpleNamespace(value="running"))]

        async def observe_once(self):
            observation = await super().observe_once()
            observation.public["controlled"]["position"][1] = self.y()
            return observation

    game = LateLift()
    game.catalog = dict(CATALOG)
    table = asyncio.run(guards.measure_guards(game, game, game.catalog, PRIMITIVES, clock=game.clock, sleep=game.sleep))
    air = table["verbs"]["attack"]["air"]
    assert all(d > 0 for d in air["pressed_at"]) and air["effect"] is False    # pressed aloft: no shot


def test_a_short_jump_is_pressed_at_its_measured_peak_not_a_planned_apex_after_landing():
    class Hop(Game):
        """The rise is up for 0.4 s only; the planned apex (0.6 s) is after it lands."""

        def __init__(self):
            super().__init__()
            self.lift = (-1.0, -1.0)

        def y(self):
            return 1.0 if self.lift[0] <= self.now < self.lift[1] else 0.0

        async def start_act(self, *, action, action_id, lease_s, **_):
            self.held = action
            controls = self.controls()
            if "space" in controls and self.y() == 0.0:
                self.lift = (self.now + 0.05, self.now + 0.4)
            if "j" in controls and self.y() == 0.0:
                self.shots += 1
                self.last_shot = self.now
            return [SimpleNamespace(status=SimpleNamespace(value="running"))]

        async def observe_once(self):
            observation = await super().observe_once()
            observation.public["controlled"]["position"][1] = self.y()
            return observation

    primitives = SimpleNamespace(**{**vars(PRIMITIVES), "rise_duration_s": 1.2, "air_press_s": 0.6})
    game = Hop()
    game.catalog = dict(CATALOG)
    table = asyncio.run(guards.measure_guards(game, game, game.catalog, primitives, clock=game.clock, sleep=game.sleep))
    air = table["verbs"]["attack"]["air"]
    assert all(d > 0 for d in air["pressed_at"]) and air["effect"] is False and air["timing"]["pressed_after_s"] < 0.4


def test_every_measured_opposite_pair_is_chorded_and_compared_within_itself():
    body = [direction("forward", (0.0, 0.0, 1.0)), direction("back", (0.0, 0.0, -1.0))]
    catalog = {**CATALOG, "forward": key("w"), "back": key("s")}
    primitives = SimpleNamespace(**{**vars(PRIMITIVES), "body_directions": body})
    plan = guards.plan_guards(catalog, primitives)
    assert plan["moves"] == ["forward", "back", "right", "left"]
    assert plan["pairs"] == [["forward", "back"], ["right", "left"]]
    assert {c.name for c in plan["chords"]} >= {"g_forward_dash", "g_back_dash", "g_right_dash", "g_left_dash"}
    cell = lambda changed: {"effect": bool(changed), "changed": changed, "moved_differently": False}
    table = {"moves": plan["moves"], "pairs": plan["pairs"], "verbs": {"dash": {"moving": {
        "forward": cell({"blend": [0, 1]}), "back": cell({}), "right": cell({"blend": [0, 1]}), "left": cell({})}}}}
    found = [(a["present_while"], a["absent_while"]) for a in guards.asymmetries(table)]
    assert found == [("forward", "back"), ("right", "left")]       # never forward against right
    old = {"moves": ["forward", "back"], "verbs": table["verbs"]}   # a table from before pairs: its two moves
    assert [(a["present_while"], a["absent_while"]) for a in guards.asymmetries(old)] == [("forward", "back")]


def test_an_asymmetry_the_direction_causes_or_a_limit_explains_is_not_the_verbs():
    def cell(changed, starts=None, moved=False):
        return {"effect": bool(changed), "changed": changed, "moved_differently": moved, "starts": starts or []}

    pickup = {"pickup_timer": [5.0, 0.0]}
    verbs = {v: {"moving": {"right": cell(dict(pickup)), "left": cell({})}} for v in ("aim", "fire", "jump2")}
    # look_down drives the pitch up; while left was held it began at the clamp a second trial pushed it to.
    verbs["look_down"] = {"rest": cell({"pitch": [-80.0, -40.0]}), "moving": {
        "right": cell({"pitch": [-30.0, 10.0, 70.0]}, [{"pitch": -30.0}, {"pitch": 70.0}]),
        "left": cell({}, [{"pitch": 80.0}, {"pitch": 80.0}])}}
    # the camera's Euler pitch wraps at 360 as it goes: 330 -> 13 is a rise, and 80 is its clamp.
    verbs["look_rot"] = {"rest": cell({"camera.rotation": [[280.0, 10.0, 0.0], [305.0, 10.0, 0.0]]}), "moving": {
        "right": cell({"camera.rotation": [[330.0, 10.0, 0.0], [13.0, 10.0, 0.0], [70.0, 10.0, 0.0]]}),
        "left": cell({}, [{"camera.rotation": [80.0, 10.0, 0.0]}])}}
    # crouch drives its blend up from 0; beginning at 0 is its rest, not its limit.
    verbs["crouch"] = {"rest": cell({"crouch_blend": [0.0, 1.0]}), "moving": {
        "right": cell({"crouch_blend": [0.0, 1.0]}, [{"crouch_blend": 0.0}]),
        "left": cell({}, [{"crouch_blend": 0.0}])}}
    verbs["dash"] = {"moving": {"right": cell({}, moved=True), "left": cell({}, moved=False)}}
    table = {"moves": ["right", "left"], "pairs": [["right", "left"]], "verbs": verbs}
    dropped = []
    kept = guards.asymmetries(table, dropped=dropped)
    assert [(a["verb"], a["key"]) for a in kept] == [("crouch", "crouch_blend"), ("dash", "displacement")]
    reasons = {(d["verb"], d["key"]): d["reason"] for d in dropped}
    assert reasons[("aim", "pickup_timer")] == "the direction's: 3 verbs share it"
    assert reasons[("look_down", "pitch")] == reasons[("look_rot", "camera.rotation")] == "at a limit where it was absent"
    own = {"crouch_blend", "displacement", "camera.rotation"}
    assert [(a["verb"], a["key"]) for a in guards.asymmetries(table, own=own)] == [
        ("crouch", "crouch_blend"), ("dash", "displacement")]
    manifest = {"entity_id": "e", "readers": [{"alias": "crouch_blend", "descriptor": {"object_id": "e.0"}},
                                              {"alias": "pickup_timer", "descriptor": {"object_id": "p"}}]}
    assert "crouch_blend" in guards.own_keys(manifest) and "pickup_timer" not in guards.own_keys(manifest)


def test_each_trial_says_which_of_its_changes_outlasted_the_press():
    game = Game()
    game.catalog = dict(CATALOG)
    table = asyncio.run(guards.measure_guards(game, game, game.catalog, PRIMITIVES, clock=game.clock, sleep=game.sleep))
    aim, attack = table["verbs"]["aim"]["rest"], table["verbs"]["attack"]["rest"]
    assert all("camera.field_of_view" in keys for keys in aim["trial_changed"])
    assert not any("camera.field_of_view" in keys for keys in aim["trial_persisted"])   # zoom only while held
    assert all("shots" in keys for keys in attack["trial_persisted"])                    # a shot stays fired
