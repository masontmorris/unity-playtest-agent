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
        ("while left held", "guarded"),         # began mid-range and did not move, but it moves at rest: a guard
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


def magnitude_table(back_speed=5.0, peak=1.0):
    def moving(move_disp, verb_disp):
        return {"held_s": 0.4, "baseline_displacement": move_disp, "displacement": verb_disp, "changed": {}}
    return {"moves": ["forward", "back"], "rise": "jump", "verbs": {
        "dash": {"ambient": [], "rest": {"changed": {}}, "air": {"changed": {}, "baseline_peak_displacement": peak},
                 "moving": {"forward": moving(2.0, 4.0), "back": moving(back_speed * 0.4, back_speed * 0.4)}},
        "aim": {"ambient": [], "rest": {"changed": {}}, "air": {"changed": {}, "baseline_peak_displacement": peak},
                "moving": {"forward": moving(2.0, 2.0)}}}}


def test_a_declared_magnitude_is_checked_against_what_the_guard_tables_measured_within_its_band():
    rows = expectations.score_magnitudes([
        {"quantity": "move_speed", "action": "forward", "state": None, "value": 5.0, "basis": "declared_value",
         "because": "moveSpeed 5"},
        {"quantity": "move_speed", "action": "back", "state": None, "value": 5.0, "basis": "declared_value",
         "because": "moveSpeed 5"},
        {"quantity": "verb_speed", "action": "dash", "state": "forward", "value": 10.5, "basis": "declared_value",
         "because": "dashSpeed 10.5"},
        {"quantity": "rise_height", "action": "jump", "state": None, "value": 1.2, "basis": "declared_value",
         "because": "jumpHeight 1.2"}], [magnitude_table(back_speed=2.5, peak=1.0)] * 3)
    assert [r["verdict"] for r in rows] == ["held", "mismatch", "held", "held"]
    assert rows[1]["measured"] == [2.5, 2.5, 2.5] and "outside -15%/+15% in 3 of 3" in rows[1]["line"]
    # A peak read 17% under its declared height is sampling (a reading rarely lands on the apex); 31% under is not.
    (low,) = expectations.score_magnitudes([{"quantity": "rise_height", "action": "jump", "state": None,
                                             "basis": "declared_value", "because": "x", "value": 1.45}],
                                           [magnitude_table(peak=1.0)] * 2)
    assert low["verdict"] == "mismatch"
    old = {"moves": ["forward"], "verbs": {"dash": {"moving": {"forward": {"baseline_displacement": 2.0}}}}}
    (none,) = expectations.score_magnitudes([{"quantity": "move_speed", "action": "forward", "state": None,
                                              "value": 5, "basis": "declared_value", "because": "x"}], [old])
    assert none["verdict"] == "unmeasured"     # a table without press lengths cannot show a speed


def test_a_declared_duration_is_an_interval_check_on_episodes_seen_whole():
    def flag(*pattern, gap=0.8):
        return [(i * gap, 100 + i, {"dashing": on}) for i, on in enumerate(pattern)]
    rule = {"observable": "dashing", "value": True, "seconds": 1.5, "basis": "declared_value", "because": "dashTime"}
    fits = flag(False, True, True, False, True, True, False)            # each on between 0.8 and 2.4 s: 1.5 fits
    short = flag(False, True, False, False, True, False)                # each on at most 1.6 s: 1.5 fits
    long = flag(False, True, True, True, True, True, False)             # on at least 3.2 s: 1.5 does not fit
    (row,) = expectations.score_durations([rule], [fits, short])
    assert (row["verdict"], row["n"]) == ("held", 4)
    (row,) = expectations.score_durations([rule], [long, long])
    assert (row["verdict"], row["k"], row["n"]) == ("mismatch", 2, 2) and "lasted between 3.2 and 4.8 s" in row["line"]
    edge = flag(True, True, False, True, True)                          # starts on, ends on: neither is whole
    (row,) = expectations.score_durations([rule], [edge])
    assert row["verdict"] == "unmeasured" and row["n"] == 0


def test_magnitudes_and_durations_are_screened_against_what_was_measured():
    packet = {"verbs": [{"name": "dash"}], "moves": [{"name": "forward"}], "rise": {"name": "jump"},
              "readers": [{"alias": "dashing", "kind": "bool"}]}
    answer = {"magnitudes": [
        {"quantity": "move_speed", "action": "forward", "state": None, "value": 5},
        {"quantity": "move_speed", "action": "dash", "state": None, "value": 5},
        {"quantity": "verb_speed", "action": "dash", "state": None, "value": 9},
        {"quantity": "rise_height", "action": "jump", "state": None, "value": 0}],
        "durations": [{"observable": "dashing", "value": True, "seconds": 0.2},
                      {"observable": "dashing", "value": True, "seconds": 0}]}
    kept, aside = expectations.screened_play(answer, packet)
    assert len(kept["magnitudes"]) == 1 and len(kept["durations"]) == 1
    assert [a["set_aside"] for a in aside] == ["seconds must be positive, at most 30",
                                               "move_speed names a measured move, with no state",
                                               "verb_speed names a measured verb and a measured move",
                                               "a magnitude must be positive"]
    body = expectations.request_body({"moves": [{"name": "forward"}]})
    props = body["response_format"]["json_schema"]["schema"]["properties"]
    assert props["magnitudes"]["items"]["properties"]["state"]["enum"] == ["forward", None]


def hit_sessions():
    """Hits every 3.2 s; each gives invulnerability unless the player was dashing at the hit."""
    rows, t, seq = [], 0.0, 0
    health = 100
    for k in range(8):
        dashing = k in (2, 5, 7)
        for step in range(4):
            values = {"health": health, "invuln": step in (1, 2) and not dashing, "dashing": dashing and step == 1}
            if step == 1:
                health -= 10
                values["health"] = health
            rows.append((t, seq, values))
            t, seq = t + 0.8, seq + 1
    return [rows]


def test_the_investigation_tests_a_proposed_condition_against_every_event():
    rule = {"when": "health", "when_change": "decreases", "when_value": None, "then": "invuln",
            "then_expect": "reaches", "then_value": True, "within_s": 1.0, "basis": "declared_value", "because": "x"}
    events = expectations.transition_events(rule, hit_sessions())
    assert len(events) == 8 and sum(not e["followed"] for e in events) == 3
    inputs = [[(1.0, 1.4, "dash"), (8.5, 8.9, "jump")]]
    described = expectations.describe_events(events, inputs, [0.0])
    assert described[0]["last_action"] is None                          # the first hit came before any input
    assert (described[1]["last_action"], described[1]["last_action_ended_s_before"]) == ("dash", 2.6)
    assert described[2]["trigger"]["dashing"] is True and described[3]["last_action"] == "jump"
    tested = expectations.check_hypotheses([
        {"observable": "dashing", "at": "trigger", "op": "==", "value": True, "explains": "dashing skips i-frames"},
        {"observable": "health", "at": "before", "op": ">", "value": 1000, "explains": "never"},
        {"observable": "last_action", "at": "before", "op": "==", "value": "dash", "explains": "after a dash"}],
        described)
    dash, never, last = tested
    assert (dash["verdict"], dash["failed_held"], dash["followed_held"], dash["failed_not"]) == ("supported", 3, 0, 0)
    assert dash["p"] < 0.05 and "proposed after seeing these events" in dash["line"]
    assert never["verdict"] == "does not separate" and last["verdict"] == "does not separate"
    packet = expectations.investigation_packet(rule, described)
    assert len(packet["events"]) == 8 and packet["rule"]["then"] == "invuln"
    assert abs(expectations._fisher_greater(3, 0, 0, 5) - 1 / 56) < 1e-9


def test_investigate_sends_one_call_per_rule_that_failed_sometimes(tmp_path, monkeypatch):
    import json as _json
    rule = {"when": "health", "when_change": "decreases", "when_value": None, "then": "invuln",
            "then_expect": "reaches", "then_value": True, "within_s": 1.0, "basis": "declared_value", "because": "x"}
    always = {**rule, "then": "health", "then_expect": "changes", "then_value": None}
    (tmp_path / "e.json").write_text(_json.dumps({"transitions": [rule, always]}))
    run = tmp_path / "run"
    run.mkdir()
    rows = [{"kind": "observation", "trace_seq": seq, "body": {"wall_utc": 1_000_000.0 + t, "public": values}}
            for t, seq, values in hit_sessions()[0]]
    (run / "trace.jsonl").write_text("\n".join(_json.dumps(r) for r in rows))
    monkeypatch.setattr("playtest.bench.replay._load", lambda path: ({}, "sha"))
    monkeypatch.setattr("playtest.bench.replay.validate_approval", lambda *a: None)
    monkeypatch.setenv("META_API_KEY", "fake")
    sent = []

    def send(body, approval, folder, name):
        sent.append(name)
        return {"hypotheses": [{"observable": "dashing", "at": "trigger", "op": "==", "value": True,
                                "explains": "x"}]}, {"cost": 0.001}
    monkeypatch.setattr(expectations, "_send", send)
    assert expectations.main(["investigate", "--expectations", str(tmp_path / "e.json"), "--runs", str(run),
                              "--approval", str(tmp_path / "a"), "--output", str(tmp_path / "out")]) == 0
    result = _json.loads((tmp_path / "out" / "investigation.json").read_text())
    assert sent == ["transition-1"] and result[0]["hypotheses"][0]["verdict"] == "supported"


def test_an_effect_seen_in_some_trials_is_intermittent_not_missing():
    flicker = table({"changed": {}, "trial_changed": [["aim_blend"], []]}, cell())
    dead = table({"changed": {}, "trial_changed": [[], []]}, cell())
    rows = expectations.score([prediction(expect="changes")], [flicker, flicker, dead])
    assert rows[0]["outcomes"] == ["held", "held", "missing"] and rows[0]["verdict"] == "mixed"


def test_a_duration_that_ran_long_because_its_input_was_given_again_is_found_by_the_inputs_during_it():
    rule = {"observable": "dashing", "value": True, "seconds": 0.2, "basis": "declared_value", "because": "x"}
    pattern = [False, True, False, False, True, True, True, True, False, False, True, False, False, True, True, True,
               True, True, False]
    readings = [(i * 0.8, 100 + i, {"dashing": on}) for i, on in enumerate(pattern)]
    events = expectations.duration_events(rule, [readings])
    assert [e["followed"] for e in events] == [True, False, True, False]
    inputs = [[(0.7, 0.9, "dash"), (3.1, 3.3, "dash"), (3.9, 4.1, "dash"), (4.7, 4.9, "dash"), (7.9, 8.1, "dash"),
               (10.3, 10.5, "dash"), (11.1, 11.3, "dash"), (12.0, 12.2, "dash"), (12.7, 12.9, "dash")]]
    described = expectations.describe_events(events, inputs, [0.0])
    assert described[1]["inputs_during"] == {"dash": 3} and described[1]["lasted"] == [2.4, 4.0]
    (repeat,) = expectations.check_hypotheses([{"observable": "inputs_during", "at": "trigger", "op": "contains",
                                                "value": "dash", "explains": "dash pressed again"}], described)
    assert repeat["verdict"] == "does not separate"    # every episode had a dash during it: no separation
    counts = [{"observable": "inputs_during", "at": "trigger", "op": "contains", "value": "jump", "explains": "x"}]
    assert expectations.check_hypotheses(counts, described)[0]["failed_held"] == 0


def test_the_magnitudes_call_asks_for_the_sizes_only():
    body = expectations.request_body({"moves": [{"name": "forward"}]}, "magnitudes")
    assert body["messages"][0]["content"] == expectations.MAGNITUDE_SYSTEM
    assert body["response_format"]["json_schema"]["schema"]["required"] == ["magnitudes"]
    assert expectations.request_body({"moves": []})["response_format"]["json_schema"]["name"] == "expectations"


def test_a_turn_across_zero_keeps_its_sign_for_an_angle_and_only_for_an_angle():
    turn = {"ambient": [], "rest": {"changed": {"yaw": [356.082, 43.402]}}, "moving": {}, "air": None}
    health = {"ambient": [], "rest": {"changed": {"health": [300.0, 50.0]}}, "moving": {}, "air": None}
    tables = [{"moves": [], "verbs": {"look_right": turn, "drink": health}}]
    rows = expectations.score([prediction(verb="look_right", observable="yaw", expect="increases"),
                               prediction(verb="drink", observable="health", expect="decreases")], tables)
    assert [r["verdict"] for r in rows] == ["held", "held"]       # +47 degrees; a drop of 250 is a drop
    assert expectations.is_angle("camera.rotation") and expectations.is_angle("player_yaw")
    assert not expectations.is_angle("health")


def test_the_rise_pressed_again_in_the_air_is_scored_from_the_air_rise_probe():
    def table(gained, rest=2.0, pressed=4.0):
        return {"moves": [], "rise": "jump", "verbs": {},
                "rise_air": {"rest_peak": rest, "pressed_peak": pressed, "gained": gained}}
    again = prediction(verb="jump", state="air", observable="body_motion", expect="moves_body",
                       because="maxJumps 2")
    (row,) = expectations.score([again], [table(True), table(True), table(False, pressed=2.1)])
    assert (row["verdict"], row["outcomes"]) == ("mixed", ["held", "held", "missing"])
    (row,) = expectations.score([again], [table(False, pressed=2.1)] * 2)
    assert row["verdict"] == "mismatch" and row["line"].startswith("jump pressed again in the air")
    (row,) = expectations.score([again], [{"moves": [], "rise": "jump", "verbs": {}, "rise_air": None}])
    assert row["verdict"] == "unmeasured"
    packet = {"verbs": [{"name": "dash"}], "rise": {"name": "jump"}, "readers": []}
    kept, aside = expectations.screened([again, prediction(verb="jump", state="rest", observable="body_motion",
                                                           expect="moves_body")], packet)
    assert len(kept) == 1 and aside[0]["set_aside"].startswith("the rise is measured only pressed again")


def test_an_effect_missing_here_that_the_input_shows_in_another_state_is_a_guard_not_a_mismatch():
    dash = {"ambient": [], "rest": {"changed": {}}, "air": None,
            "moving": {"right": {"changed": {"dashing": [False, True]}}, "left": {"changed": {"dashing": [False, True]}}}}
    one_way = {"ambient": [], "rest": {"changed": {}}, "air": None,
               "moving": {"right": {"changed": {"dashing": [False, True]}}, "left": {"changed": {}}}}
    dead = {"ambient": [], "rest": {"changed": {}}, "air": None,
            "moving": {"right": {"changed": {}}, "left": {"changed": {}}}}
    tables = [{"moves": ["right", "left"], "verbs": {"dash": dash, "aim": dead, "sprint": one_way}}] * 3
    rows = expectations.score([prediction(verb="dash", observable="dashing", expect="changes"),
                               prediction(verb="aim", observable="dashing", expect="changes"),
                               prediction(verb="sprint", state="moving", observable="dashing", expect="changes")],
                              tables)
    guard, missing, right, left = rows
    assert (right["verdict"], left["cell"], left["verdict"]) == ("held", "while left held", "mismatch")  # its mirror works
    assert (guard["verdict"], guard["works"]) == ("guarded", "while left held and while right held")
    assert "a guard, which is a rule of the game or a bug" in guard["line"]
    assert missing["verdict"] == "mismatch"            # no effect anywhere: the stronger evidence
    assert expectations.unmet_in_session({"predictions": [prediction(verb="dash", observable="dashing",
                                                                     expect="changes")]},
                                         tables[0]) == []   # a guard is not stated to S1 as an unmet expectation


def test_presses_come_from_plain_executions_and_from_inside_options():
    rows = [{"kind": "execution", "body": {"request": {"action": "attack"}, "records": [
                {"status": "completed", "input_start_wall_s": 10.0, "applied_duration_s": 0.2}]}},
            {"kind": "execution", "body": {"request": {"action": "dash"}, "records": [{"status": "rejected"}]}},
            {"kind": "option", "body": {"receipts": [
                {"type": "request", "action_id": "o.1", "action": "attack_air"},
                {"type": "execution", "action_id": "o.1", "status": "running", "input_start_wall_s": 12.0},
                {"type": "execution", "action_id": "o.1", "status": "completed", "input_start_wall_s": 12.0,
                 "applied_duration_s": 0.3}]}}]
    assert expectations.play_presses(rows) == [(10.0, 10.2, "attack"), (12.0, 12.3, "attack_air")]
    catalog = {"actions": {"attack": {"inputs": [{"device": "Mouse", "control": "leftButton"}]},
                           "attack_air": {"inputs": [{"device": "Keyboard", "control": "space"},
                                                     {"device": "Mouse", "control": "leftButton"}]}}}
    controls = expectations.catalog_controls(catalog)
    assert controls["attack"] <= controls["attack_air"]           # the chord presses attack too


def attack_rule(**updates):
    base = {"action": "attack", "condition_observable": "cooldown", "condition_op": "<=", "condition_value": 0,
            "situation": None, "then": "shots", "then_expect": "increases", "then_value": None, "within_s": 1.0,
            "basis": "declared_value", "because": "attackCooldown 0.5"}
    return base | updates


def test_an_action_rule_is_checked_at_every_press_whose_condition_held_just_before_it():
    def values(t):
        return {"cooldown": 0.4 if 2.0 <= t < 2.5 else 0.0, "shots": 0 if t < 0.5 else 1 if t < 6.5 else 2}
    readings = [(i * 0.5, 100 + i, values(i * 0.5)) for i in range(21)]
    presses = [(0.2, 0.3, "attack"), (2.2, 2.3, "attack_air"), (4.2, 4.3, "attack"), (6.2, 6.3, "attack"),
               (8.0, 8.1, "jump")]
    controls = {"attack": frozenset({("Mouse", "left")}), "attack_air": frozenset({("Mouse", "left"), ("K", "space")}),
                "jump": frozenset({("K", "space")})}
    (row,) = expectations.score_action_rules([attack_rule()], [readings], [presses], [controls])
    # 0.2: ready, a shot follows. 2.2 (the chord presses attack): cooldown 0.4 just before, so not counted.
    # 4.2: ready, no shot by 5.3. 6.2: ready, a shot at 6.5. The jump does not press attack.
    assert (row["n"], row["k"], row["verdict"]) == (3, 1, "mixed")
    assert row["not_counted"]["condition_not_met"] == 1 and row["examples"] == [108]
    assert row["line"].startswith("pressing attack with cooldown <= 0: shots increases within 1 s")
    no_condition = attack_rule(condition_observable=None, condition_op=None, condition_value=None)
    (row,) = expectations.score_action_rules([no_condition], [readings], [presses], [controls])
    assert (row["n"], row["k"]) == (4, 2)                          # every press counts, the chord's too
    crowded = [(0.2, 0.3, "attack"), (0.8, 0.9, "attack"), (4.2, 4.3, "attack")]
    (row,) = expectations.score_action_rules([no_condition], [readings], [crowded], [controls])
    assert row["not_counted"]["overlapped"] == 1 and row["n"] == 2   # 0.2's window holds the next press


def test_a_rule_whose_condition_no_reader_shows_is_kept_as_a_situation_for_a_directed_test():
    packet = {"verbs": [{"name": "attack"}, {"name": "interact"}], "moves": [], "rise": None,
              "readers": [{"alias": "shots", "kind": "number"}, {"alias": "cooldown", "kind": "number"},
                          {"alias": "talking", "kind": "bool"}]}
    answer = {"action_rules": [
        attack_rule(),
        attack_rule(action="interact", condition_observable=None, condition_op=None, condition_value=None,
                    situation="standing next to the NPC", then="talking", then_expect="reaches", then_value=True),
        attack_rule(condition_observable="cooldown", condition_op=None),
        attack_rule(action="reload")]}
    kept, aside = expectations.screened_play(answer, packet)
    assert len(kept["action_rules"]) == 1 and kept["situations"][0]["situation"] == "standing next to the NPC"
    assert [a["set_aside"] for a in aside] == ["a condition is a reader, a comparator and a value",
                                               "not a measured input"]


def test_the_test_plan_asks_for_each_rules_press_in_plain_words_and_nothing_more():
    plan = expectations.test_plan({"action_rules": [attack_rule(), attack_rule(then="cooldown", then_expect="changes")],
                                   "situations": [attack_rule(action="interact", situation="stand next to the NPC.",
                                                              condition_observable=None, condition_op=None,
                                                              condition_value=None)]})
    assert plan == ("Try each of these on purpose, 4 times or more, a few seconds apart, so each one can be seen on "
                    "its own: (1) press attack when cooldown <= 0; (2) stand next to the NPC, then press interact. "
                    "Then keep exploring.")
    assert expectations.test_plan({}) == ""
    body = expectations.request_body({"moves": []}, "actions")
    assert body["response_format"]["json_schema"]["schema"]["required"] == ["action_rules"]


def test_a_failed_action_rule_is_read_against_what_the_guard_tables_measured_of_its_input():
    def values(t):
        return {"horiz": 0.0, "dashing": False}
    readings = [(i * 0.5, 100 + i, values(i * 0.5)) for i in range(21)]
    presses = [(0.2, 0.3, "stick"), (3.2, 3.3, "stick"), (6.2, 6.3, "stick")]
    controls = {"stick": frozenset({("Pad", "stick")})}
    rule = attack_rule(action="stick", condition_observable=None, condition_op=None, condition_value=None,
                       then="horiz", then_expect="changes")
    cell = lambda changed, persisted: {"changed": {k: [0, 1] for k in changed}, "trial_changed": [changed, changed],
                                       "trial_persisted": [persisted, persisted]}
    held_only = {"verbs": {"stick": {"rest": cell(["horiz"], []), "moving": {}, "air": None}}}
    lasting = {"verbs": {"stick": {"rest": cell([], []), "moving": {"right": cell(["horiz"], ["horiz"])}, "air": None}}}
    dead = {"verbs": {"stick": {"rest": cell([], []), "moving": {}, "air": None}}}
    verdicts = [expectations.score_action_rules([rule], [readings], [presses], [controls], guards)[0]["verdict"]
                for guards in ([held_only], [lasting], [dead], [])]
    assert verdicts == ["while_held", "guarded", "mismatch", "mismatch"]
    assert expectations.guard_evidence("stick", "horiz", [held_only]) == "while_held"
    assert expectations.guard_evidence("other", "horiz", [held_only]) is None


def test_one_stick_pushed_two_ways_is_two_inputs_and_a_reader_at_its_clamp_cannot_show_a_press():
    catalog = {"actions": {"look_up": {"inputs": [{"device": "Pad", "control": "stick", "x": 0.0, "y": 1.0}]},
                           "look_down": {"inputs": [{"device": "Pad", "control": "stick", "x": 0.0, "y": -1.0}]}}}
    controls = expectations.catalog_controls(catalog)
    assert controls["look_up"] != controls["look_down"]
    pitch = [10.0, 10.0, 40.0, 40.0, 70.0, 70.0, 80.0, 80.0, 80.0, 80.0, 80.0, 80.0, 80.0]
    readings = [(i * 1.0, 100 + i, {"pitch": v}) for i, v in enumerate(pitch)]
    presses = [(1.2, 1.3, "look_down"), (3.2, 3.3, "look_down"), (5.2, 5.3, "look_down"), (8.2, 8.3, "look_down"),
               (10.2, 10.3, "look_up")]
    rule = attack_rule(action="look_down", condition_observable=None, condition_op=None, condition_value=None,
                       then="pitch", then_expect="changes", within_s=1.0)
    (row,) = expectations.score_action_rules([rule], [readings], [presses], [controls])
    # Three presses raised the pitch; the fourth began at 80, the furthest the session read: at its clamp.
    assert (row["n"], row["k"], row["verdict"], row["not_counted"]["at_limit"]) == (3, 0, "held", 1)
    dead = [(i * 1.0, 100 + i, {"pitch": 10.0}) for i in range(13)]
    (row,) = expectations.score_action_rules([rule], [dead], [presses], [controls])
    assert (row["k"], row["verdict"]) == (4, "mismatch")          # never moved: no direction, nothing left out
