"""Expectations written from the declarations before measurement, scored by code against fresh guard tables."""

from playtest.runner import expectations

MANIFEST = {"entity_id": "e", "actions": [
    {"name": "aim", "intent": "toggle aim zoom", "calibrations": [{"bindings": [{"path": "<Mouse>/middleButton"}]}]},
    {"name": "dash", "intent": "dash sprint modifier", "calibrations": []},
    {"name": "right", "intent": "move right"}, {"name": "left", "intent": "move left"}, {"name": "jump", "intent": "jump"}],
    "skills": [], "readers": [
        {"alias": "aim_blend", "kind": "number",
         "descriptor": {"declaration_id": "Kit:Kit.Player:aimBlend", "declaring_type": "Kit.Player", "name": "aimBlend"}},
        {"alias": "timer", "kind": "number", "descriptor": {"declaration_id": "Kit:Kit.Clock:t"}}]}
DISCOVERY = {"data": {"il_write_sets": [{"field_id": "Kit:Kit.Player:aimBlend", "direct_writers": [
    {"method": "Kit:Kit.Player:UpdateAim(System.Single)", "constructor": False},
    {"method": "Kit:Kit.Player:.ctor()", "constructor": True}]}]}}


def cell(changed=None, moved=False):
    return {"effect": bool(changed) or moved, "changed": changed or {}, "moved_differently": moved}


def table(aim_rest, dash_left, ambient=()):
    return {"moves": ["right", "left"], "rise": "jump", "verbs": {
        "aim": {"ambient": list(ambient), "rest": aim_rest, "moving": {}, "air": None},
        "dash": {"ambient": [], "rest": cell(), "moving": {"right": cell(moved=True), "left": dash_left}, "air": cell()}}}


def test_the_packet_carries_declarations_and_writers_and_no_measurement(monkeypatch):
    monkeypatch.setattr(expectations, "declared_configuration", lambda *a: {"values": [{"source": "Player.aimFov"}]})
    packet = expectations.build_packet(MANIFEST, DISCOVERY, table(cell({"aim_blend": [0, 1]}), cell(moved=True)))
    assert [v["name"] for v in packet["verbs"]] == ["aim", "dash"] and packet["rise"]["intent"] == "jump"
    assert packet["verbs"][0]["controls"] == ["<Mouse>/middleButton"]
    assert packet["readers"][0] == {"alias": "aim_blend", "kind": "number", "field": "Player.aimBlend",
                                    "written_by": ["Player.UpdateAim"]}       # the constructor is not a writer
    text = str(packet)
    assert "changed" not in text and "moved_differently" not in text          # nothing measured goes in
    assert expectations.request_body(packet)["response_format"]["json_schema"]["strict"] is True


def prediction(**updates):
    base = {"verb": "aim", "state": "rest", "observable": "aim_blend", "expect": "increases", "basis": "intent",
            "because": "toggle aim zoom", "precondition": None}
    return base | updates


def test_only_unconditional_predictions_about_measured_verbs_and_observables_are_scored(monkeypatch):
    monkeypatch.setattr(expectations, "declared_configuration", lambda *a: None)
    packet = expectations.build_packet(MANIFEST, DISCOVERY, table(cell(), cell()))
    kept, aside = expectations.screened([
        prediction(), prediction(precondition="a target in range"), prediction(verb="reload"),
        prediction(observable="ammo"), prediction(observable="body_motion", expect="increases"),
        prediction(verb="dash", state="moving", observable="body_motion", expect="moves_body")], packet)
    assert len(kept) == 2 and [a["set_aside"] for a in aside] == [
        "needs a condition the guard states do not give", "not a measured verb", "not a measured observable",
        "moves_body goes with body_motion only"]


def test_a_prediction_that_failed_in_most_fresh_sessions_is_a_mismatch_with_its_declaration():
    dead = table(cell(), cell())                                  # aim did nothing; dash moving left did nothing
    works = table(cell({"aim_blend": [0, 0.5, 1]}), cell(moved=True))
    rows = expectations.score([prediction(), prediction(verb="dash", state="moving", observable="body_motion",
                                                        expect="moves_body", because="dash sprint modifier")],
                              [dead, dead, works])
    aim, right, left = rows
    assert (aim["verdict"], aim["k"], aim["n"]) == ("mismatch", 2, 3)
    assert aim["line"] == ("aim at rest: expected to increase aim_blend (intent: toggle aim zoom); "
                           "not in 2 of 3 fresh sessions")
    assert (right["cell"], right["verdict"]) == ("while right held", "held")   # checked in both directions
    assert (left["cell"], left["verdict"], left["k"]) == ("while left held", "mismatch", 2)


def test_a_wrong_direction_is_reversed_and_an_observable_that_changes_by_itself_is_never_missing():
    reversed_ = table(cell({"aim_blend": [1.0, 0.4, 0.0]}), cell())
    (row,) = expectations.score([prediction()], [reversed_] * 3)
    assert row["verdict"] == "mismatch" and row["outcomes"] == ["reversed"] * 3 and "the other way in 3 of 3" in row["line"]
    noisy = table(cell(), cell(), ambient=["aim_blend"])
    (row,) = expectations.score([prediction()], [noisy] * 3)
    assert row["verdict"] == "ambient" and row["k"] == 0 and row["n"] == 0
    (row,) = expectations.score([prediction(expect="changes")], [table(None, cell())])
    assert row["verdict"] == "unmeasured"
    assert expectations._short("Kit:Kit.Player:UpdateAim(System.Single)") == "Player.UpdateAim"


ASSET = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &11400000
MonoBehaviour:
  m_ObjectHideFlags: 0
  m_Script: {fileID: 11500000, guid: abc, type: 3}
  m_Name: PlayerConfig
  speed: 7.5
  maxJumps: 2
  canDash: 1
  dash:
    distance: 4
    target: {fileID: 0}
  tags:
  - a
"""


def test_an_asset_the_entity_references_is_read_for_its_authored_values(tmp_path):
    (tmp_path / "Assets").mkdir()
    (tmp_path / "Assets" / "PlayerConfig.asset").write_text(ASSET, encoding="utf-8")
    assert expectations.asset_values(ASSET) == {"speed": 7.5, "maxJumps": 2, "canDash": 1, "dash.distance": 4}
    discovery = {"data": {"game_objects": [{"id": "e", "parent_id": None}, {"id": "c", "parent_id": "e"},
                                           {"id": "x", "parent_id": None}],
                          "fields": [{"object_id": "c", "declaring_type": "Kit.Player", "name": "config",
                                      "value": {"kind": "asset_object", "type": "Kit.PlayerConfigSO",
                                                "asset_path": "Assets/PlayerConfig.asset"}},
                                     {"object_id": "x", "declaring_type": "Kit.Enemy", "name": "config",
                                      "value": {"kind": "asset_object", "asset_path": "Assets/Other.asset"}}]}}
    (found,) = expectations.referenced_assets(discovery, "e", expectations.project_reader(tmp_path))
    assert (found["type"], found["referenced_by"], found["values"]["maxJumps"]) == ("PlayerConfigSO", "Player.config", 2)


def test_an_effect_the_state_hides_or_a_reader_at_its_limit_is_not_missing():
    look = {"ambient": [], "rest": {"changed": {"pitch": [-30.0, -55.0, -80.0]}, "starts": [{"pitch": -30.0}]},
            "moving": {"right": {"changed": {}, "starts": [{"pitch": -60.0}, {"pitch": -80.0}]},
                       "left": {"changed": {}, "starts": [{"pitch": -40.0}, {"pitch": -41.0}]}},
            "air": None}
    stick = {"ambient": [], "rest": {"changed": {"horiz": [0.0, -1.0]}},
             "moving": {"right": {"changed": {}, "baseline_changed": ["horiz"]}}, "air": None}
    tables = [{"moves": ["right", "left"], "verbs": {"look_up": look, "stick": stick}}] * 3
    rows = expectations.score([prediction(verb="look_up", state="moving", observable="pitch", expect="decreases"),
                               prediction(verb="stick", state="right", observable="horiz", expect="decreases")], tables)
    assert [(r["cell"], r["verdict"]) for r in rows] == [
        ("while right held", "at_limit"),       # the second trial began at -80, as far as the verb ever moved it
        ("while left held", "mismatch"),        # began mid-range and still did not move: missing
        ("while right held", "masked")]         # the move alone changes the stick's reader: it cannot show it


def test_a_jump_that_lands_still_moves_the_body_and_a_declared_value_is_checked_with_its_arithmetic():
    jump = {"ambient": [], "rest": {"changed": {}, "moved_differently": False, "peak_displacement": 1.2,
                                    "baseline_peak_displacement": 0.0}, "moving": {}, "air": None}
    aim = {"ambient": [], "rest": {"changed": {"camera.field_of_view": [65.0, 52.0, 40.1]}}, "moving": {}, "air": None}
    tables = [{"moves": [], "verbs": {"jump_pad": jump, "aim": aim}}] * 3
    rows = expectations.score([
        prediction(verb="jump_pad", observable="body_motion", expect="moves_body"),
        prediction(observable="camera.field_of_view", expect="reaches", value=40),
        prediction(observable="camera.field_of_view", expect="reaches", value=30)], tables)
    assert [r["verdict"] for r in rows] == ["held", "held", "mismatch"]
    assert "camera.field_of_view to reach 30" in rows[2]["line"] and "a different value in 3 of 3" in rows[2]["line"]
    packet = {"verbs": [{"name": "aim"}], "readers": []}
    kept, aside = expectations.screened([prediction(observable="camera.field_of_view", expect="reaches", value=None),
                                         prediction(observable="camera.field_of_view", value=3)], packet)
    assert not kept and {a["set_aside"] for a in aside} == {"a value goes with reaches only"}
    assert expectations.schema(["right"])["properties"]["predictions"]["items"]["properties"]["state"]["enum"] == [
        "rest", "moving", "air", "right"]


def readings(*rows):
    """(time, seq, values) per reading."""
    return [(float(t), 100 + i, values) for i, (t, values) in enumerate(rows)]


def test_a_range_is_checked_at_every_reading_of_every_session():
    ok = readings((0, {"health": 30}), (1, {"health": 20}))
    over = readings((0, {"health": 30}), (1, {"health": 45}), (2, {"health": 30}))
    (row,) = expectations.score_ranges([{"observable": "health", "min": 0, "max": 30, "basis": "declared_value",
                                         "because": "maxHealth 30"}], [ok, over, readings()])
    assert (row["verdict"], row["k"], row["n"], row["first_violation"]) == ("mismatch", 1, 2, (101, 45.0))
    assert "left that range in 1 of 2 play sessions (first: 45 at trace seq 101)" in row["line"]


def test_a_transition_must_follow_its_trigger_within_its_window_counting_a_reading_of_lag():
    rule = {"when": "health", "when_change": "decreases", "when_value": None, "then": "invuln",
            "then_expect": "reaches", "then_value": True, "within_s": 1.5, "basis": "declared_value",
            "because": "iFramesDuration 1.5"}
    works = readings((0, {"health": 30, "invuln": False}), (0.8, {"health": 20, "invuln": False}),
                     (1.6, {"health": 20, "invuln": True}), (2.4, {"health": 20, "invuln": True}))
    broken = readings((0, {"health": 30, "invuln": False}), (0.8, {"health": 20, "invuln": False}),
                      (1.6, {"health": 20, "invuln": False}), (2.4, {"health": 20, "invuln": False}),
                      (3.2, {"health": 10, "invuln": False}), (4.0, {"health": 10, "invuln": False}),
                      (4.8, {"health": 10, "invuln": False}))
    cut = readings((0, {"health": 30, "invuln": False}), (0.8, {"health": 20, "invuln": False}))
    (row,) = expectations.score_transitions([rule], [works, broken, cut])
    assert (row["verdict"], row["k"], row["n"]) == ("mismatch", 2, 3)   # the cut session's window never closed
    assert row["line"].startswith("when health decreases, invuln reaches true within 1.5 s")
    (row,) = expectations.score_transitions([rule], [works])
    assert row["verdict"] == "unmeasured"                                  # once is too few to call
    answer = {"ranges": [{"observable": "health", "min": None, "max": None}, {"observable": "camera.position",
                                                                             "min": 0, "max": 1}],
              "transitions": [rule, {**rule, "within_s": 0}, {**rule, "then_value": None}]}
    packet = {"readers": [{"alias": "health", "kind": "number"}, {"alias": "invuln", "kind": "bool"}]}
    kept, aside = expectations.screened_play(answer, packet)
    assert kept["ranges"] == [] and len(kept["transitions"]) == 1
    assert [a["set_aside"] for a in aside] == ["no bound", "not a reader",
                                               "within_s must be a positive number of seconds, at most 30",
                                               "a then_value goes with reaches only"]


def test_a_trigger_nothing_could_follow_is_not_counted():
    rule = {"when": "health", "when_change": "decreases", "when_value": None, "then": "invuln",
            "then_expect": "changes", "then_value": None, "within_s": 2.0, "basis": "declared_value", "because": "x"}
    hits = readings((0, {"health": 30, "invuln": False}), (0.8, {"health": 20, "invuln": True}),
                    (1.6, {"health": 10, "invuln": True}),          # a second hit inside the first one's window
                    (2.4, {"health": 10, "invuln": True}), (3.2, {"health": 10, "invuln": False}),
                    (4.0, {"health": 0, "invuln": False}),           # a lethal hit: the respawn is not read
                    (12.0, {"health": 30, "invuln": False}), (12.8, {"health": 30, "invuln": False}))
    (row,) = expectations.score_transitions([rule], [hits])
    assert (row["n"], row["k"]) == (1, 0)
    assert row["not_counted"] == {"window_cut": 0, "unobserved_gap": 1, "overlapped": 1}


def test_the_report_groups_each_expectation_by_what_measurement_made_of_it():
    dead = table(cell(), cell())
    rows = expectations.score([prediction(), prediction(verb="dash", state="moving", observable="body_motion",
                                                        expect="moves_body")], [dead, dead])
    text = expectations.report(rows, [], [{"verb": "interact", "set_aside": "needs a condition",
                                           "precondition": "a target in range"}], title="T")
    assert text.startswith("# T") and "## Not met (2)" in text and "## Held (1)" in text
    assert "aim at rest: expected to increase aim_blend" in text
    assert "- interact: needs a condition (a target in range)" in text


def test_unmet_expectations_reach_s1_as_facts_and_nothing_else_changes():
    import asyncio

    class Inner:
        async def facts(self, reports, observation):
            return ["coverage: 3 of 5"]

        def finish(self, **values):
            self.finished = values

    unmet = expectations.unmet_in_session({"predictions": [prediction()]}, table(cell(), cell()))
    assert len(unmet) == 1 and unmet[0]["line"].endswith("not in 1 of 1 fresh sessions")
    inner = Inner()
    facts = expectations.ExpectationFacts(inner, [row["line"] for row in unmet])
    lines = asyncio.run(facts.facts([], None))
    assert lines[0] == "coverage: 3 of 5" and lines[1].startswith("Measured before play on this build, against what")
    facts.finish(ending="x")
    assert inner.finished == {"ending": "x"} and facts.directive_view(None) is None


def test_an_air_press_that_went_in_on_the_ground_and_a_timers_direction_cannot_fail():
    jump = {"ambient": ["cooldown"], "rest": {"changed": {"cooldown": [80.0, 0.0, 0.5]}}, "moving": {},
            "air": {"changed": {}, "pressed_at": [0.0, 0.0], "peak_displacement": 2.6,
                    "baseline_peak_displacement": 2.6}}
    tables = [{"moves": [], "verbs": {"jump_pad": jump}}] * 3
    rows = expectations.score([
        prediction(verb="jump_pad", state="air", observable="body_motion", expect="moves_body"),
        prediction(verb="jump_pad", observable="cooldown", expect="increases")], tables)
    assert [r["verdict"] for r in rows] == ["not_airborne", "held"]
