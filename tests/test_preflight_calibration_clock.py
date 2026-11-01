"""The clock and scene evidence a calibration trial carries, and what it invalidates.

Every fixture here is synthetic: invented ids, no game, key or scene names. The rule under test is
general: a control that stops the game's clock or replaces its scene makes the displacement measured
across that trial meaningless, and the trial has to say so with its own readings.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from playtest.preflight import calibrate


def binding(path, part="", action="id"):
    return {"id": path, "binding_effective_path": path, "binding_name": part, "action_id": action,
            "expected_control_type": "Vector2"}


def selected(*controls):
    return {"status": "identified", "entity_id": "entity", "stop": {"release_verified": True},
            "candidate_cap": {"truncated": False},
            "candidates": [{"control": name, "binding_ids": [f"<Keyboard>/{name}"]} for name in controls],
            "entity": {"scene_id": "scene-one", "scene_path": "scene-one", "path": "object"}}


class RecordingBridge:
    """A bridge whose game clock can stop, and whose scene can change, under one control's press.

    Real time always advances with every read. Game time advances with it unless the game's clock is
    stopped, so the trial's own two reads carry the evidence the rule is decided on.
    """

    def __init__(self, controls=("one",), *, stops_on=None, changes_scene_on=None, bridge_pause=False,
                 report_clock_mode=True, rewinds_on=None):
        self.controls = tuple(controls)
        self.stops_on = stops_on
        self.changes_scene_on = changes_scene_on
        self.rewinds_on = rewinds_on
        self.bridge_pause = bridge_pause
        self.report_clock_mode = report_clock_mode
        self.real = 0.0
        self.game = 0.0
        self.scene = "scene-one"
        self.position = 0.0
        self.held = None
        self.closed = False
        self.errors = []
        self.observed = 0
        self.monitor_truncated = False
        self.events = []

    @property
    def stopped(self):
        return self.bridge_pause or self.game_frozen

    game_frozen = False
    sampling = False

    def _advance(self, seconds):
        if self.sampling:
            return
        self.real += seconds
        if not self.stopped:
            self.game += seconds

    def _press(self, control):
        if control == self.stops_on:
            self.game_frozen = True
        if control == self.changes_scene_on:
            self.scene = "scene-two"
        if control == self.rewinds_on:
            self.game -= 1.0

    def _clock_mode(self):
        if not self.report_clock_mode:
            return None
        return "stepped" if self.bridge_pause else "realtime"

    def monitor(self):
        return {"active": True, "limit": 256, "truncated": self.monitor_truncated,
                "observed": self.observed, "events": [dict(event) for event in self.errors]}

    def cap(self):
        return SimpleNamespace(truncated=False, observed=1, limit=100, returned=1,
                               model_dump=lambda **_: {"truncated": False, "observed": 1, "limit": 100,
                                                       "returned": 1})

    async def inspect(self, stage):
        self._advance(0.2)
        cap = self.cap()
        wall = datetime.fromtimestamp(1000 + self.real, timezone.utc)
        if stage == "discovery":
            return SimpleNamespace(stage=stage, errors=[], wall_utc=wall,
                caps={name: cap for name in ("game_objects", "components", "bindings", "input_asset_bindings")},
                data={"game_objects": [{"id": "entity", "scene_id": "scene-one", "scene_path": "scene-one",
                                        "path": "object"}],
                      "bindings": [binding(f"<Keyboard>/{name}") for name in self.controls],
                      "input_asset_bindings": [], "components": []})
        if stage == "transforms":
            return SimpleNamespace(stage=stage, errors=[], wall_utc=wall, caps={"game_objects": cap},
                data={"frame": 10, "real_time_s": self.real, "game_time_s": self.game, "time_unit": "seconds",
                      "position_space": "world", "position_unit": "unity_world_unit",
                      "position_order": ["x", "y", "z"], "runtime_errors": self.monitor(),
                      "game_objects": [{"id": "entity", "parent_id": None, "position": [self.position, 0, 0],
                                        "world_forward": [0, 0, 1], "world_right": [1, 0, 0],
                                        "basis_state": "present"}]})
        assert stage == "fields"
        return SimpleNamespace(stage=stage, errors=[], wall_utc=wall,
            caps={"game_objects": cap, "fields": cap},
            data={"frame": 10, "real_time_s": self.real, "game_time_s": self.game, "time_unit": "seconds",
                  "sample_wall_s": 1000 + self.real,
                  "keyboard_state": {"active": True, "keys": [self.held] if self.held else [],
                                     "real_time_s": self.real, "wall_s": 1000 + self.real,
                                     "update_type": "dynamic", "error": None},
                  "fields": [{"id": "field", "scene_id": "scene-one", "value_state": "present",
                              "value_kind": "integer", "value": 1, "is_null": False,
                              "value_truncated": False, "read_error": None}]})

    async def observe_once(self):
        observation = SimpleNamespace(
            seq=1, scene=self.scene, paused=self.stopped, time_scale=0.0 if self.stopped else 1.0,
            clock_mode=self._clock_mode(), game_s=self.game, view=None, frame=10)
        observation.model_dump = lambda **_: {"scene": self.scene, "paused": self.stopped,
                                              "clock_mode": self._clock_mode()}
        return observation

    async def clock(self, op, **kwargs):
        if op == "resume":
            self.bridge_pause = False
            self.game_frozen = False
        if op == "step":
            self.game += kwargs["step_game_s"]
        self.bridge_pause = self.bridge_pause or op == "pause"

    def record(self, status, start, end):
        return SimpleNamespace(status=SimpleNamespace(value=status), release_verified=status == "completed",
            input_start_wall_s=start, input_end_wall_s=end,
            model_dump=lambda **_: {"status": status, "release_verified": status == "completed"})

    def _control(self, kwargs):
        return kwargs["action"].replace("preflight_key_", "")

    async def act(self, **kwargs):
        control = self._control(kwargs)
        self.events.append("act:" + control)
        self.position += 1
        self._press(control)
        return [self.record("completed", 1000 + self.real, 1000 + self.real + 0.2)]

    async def act_with_inspections(self, **kwargs):
        control = self._control(kwargs)
        self.events.append("act:" + control)
        self.position += 1
        self.held = control
        start = 1000 + self.real
        self.sampling = True
        samples = [await self.inspect("fields") for _ in range(4)]
        self.sampling = False
        self.held = None
        self._press(control)
        return [self.record("completed", start, 1000 + self.real + 0.1)], samples


def recorder(bridge, calls=None):
    """A generic restore that records the call and puts a running game back."""
    calls = calls if calls is not None else []

    async def restore(*, trial, evidence, client=None):
        calls.append({"trial": trial, "evidence": evidence})
        bridge.events.append("restore")
        bridge.game_frozen = False
        bridge.bridge_pause = False
        bridge.scene = "scene-one"
        return {"method": "stub-generic-reset", "status": "restored", "error": None, "client": None}

    return restore


def run(bridge, monkeypatch, *, controls=None, restore=None, prior=None):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", (0.1, 0.6))
    monkeypatch.setattr(calibrate, "REPEATS", 1)
    monkeypatch.setattr(calibrate, "FIELD_REPEATS", 1)
    return calibrate.asyncio.run(calibrate.measure(bridge, selected(*(controls or bridge.controls)),
                                                   restore=restore, prior=prior))


def trials_for(result, control):
    return [trial for trial in result["trials"] if trial["control"] == control]


def test_a_press_that_stops_the_game_clock_is_invalid_recorded_and_restored(monkeypatch):
    bridge = RecordingBridge(controls=("one", "two"), stops_on="one")
    calls = []
    result = run(bridge, monkeypatch, restore=recorder(bridge, calls))
    stopped = trials_for(result, "one")[0]
    assert stopped["valid"] is False
    assert "the game clock stopped" in stopped["error"]
    assert "game time advanced 0.0000 s" in stopped["error"]
    change = stopped["state_change"]
    assert change["control"] == "one" and change["effect"] == "game clock stopped"
    assert change["before"]["game_time_s"] == change["after"]["game_time_s"]
    assert change["after"]["real_time_s"] > change["before"]["real_time_s"]
    assert change["before"]["scene"] == change["after"]["scene"] == "scene-one"
    assert change["evidence"]["verdict"] == "stopped"
    assert len(calls) == 1 and calls[0]["evidence"]["verdict"] == "stopped"
    assert change["restore"]["method"] == "stub-generic-reset"
    assert change["restore"]["running_after_restore"]["verdict"] == "running"
    # The restore ran before the next trial, and the control was not pressed a second time.
    assert bridge.events.count("act:one") == 1
    assert bridge.events.index("restore") < bridge.events.index("act:two")
    # The next control's trials ran on a running game and are measured.
    following = trials_for(result, "two")
    assert following and all(trial["valid"] is True for trial in following)
    assert all(trial["clock"]["verdict"] == "running" for trial in following)


def test_a_press_that_changes_the_scene_is_invalid_and_restored(monkeypatch):
    bridge = RecordingBridge(controls=("one", "two"), changes_scene_on="one")
    calls = []
    result = run(bridge, monkeypatch, restore=recorder(bridge, calls))
    changed = trials_for(result, "one")[0]
    assert changed["valid"] is False
    assert "the scene changed" in changed["error"]
    assert changed["state_change"]["effect"] == "scene changed"
    assert changed["state_change"]["before"]["scene"] == "scene-one"
    assert changed["state_change"]["after"]["scene"] == "scene-two"
    assert changed["clock"]["scene"]["changed"] is True
    assert len(calls) == 1
    assert bridge.events.index("restore") < bridge.events.index("act:two")


def test_a_bridge_initiated_pause_is_not_a_game_state_change(monkeypatch):
    bridge = RecordingBridge(controls=("one",), bridge_pause=True)
    calls = []
    result = run(bridge, monkeypatch, restore=recorder(bridge, calls))
    trial = trials_for(result, "one")[0]
    assert trial["clock"]["verdict"] == "bridge_paused"
    assert trial["clock"]["bridge_paused"] is True
    assert trial["valid"] is True
    assert trial["state_change"] is None
    assert calls == []


def test_a_stopped_window_nobody_can_attribute_is_unknown_not_valid():
    before = calibrate._window_reading({"game_time_s": 5.0, "real_time_s": 10.0, "frame": 1})
    after = calibrate._window_reading({"game_time_s": 5.0, "real_time_s": 10.4, "frame": 2})
    window = calibrate.clock_window(before, after)
    assert window["verdict"] == "unknown"
    assert window["valid"] is False and window["state_change"] is False and window["effect"] is None
    assert "who paused" in window["reason"]
    assert window["game_time_s"]["delta_s"] == 0.0
    assert window["real_time_s"]["delta_s"] == pytest.approx(0.4)


def test_a_window_too_short_to_judge_is_unknown_not_valid():
    before = calibrate._window_reading({"game_time_s": 5.0, "real_time_s": 10.0})
    after = calibrate._window_reading({"game_time_s": 5.0, "real_time_s": 10.01})
    window = calibrate.clock_window(before, after, bridge_paused=False)
    assert window["verdict"] == "unknown" and window["valid"] is False
    assert "too short" in window["reason"]


def test_a_running_trial_is_unchanged(monkeypatch):
    bridge = RecordingBridge(controls=("one", "two"))
    calls = []
    result = run(bridge, monkeypatch, restore=recorder(bridge, calls))
    assert result["trials"] and all(trial["valid"] is True for trial in result["trials"])
    assert all(trial["state_change"] is None for trial in result["trials"])
    assert all(trial["clock"]["verdict"] == "running" for trial in result["trials"])
    assert calls == [] and "restore" not in bridge.events


def test_a_stopped_clock_with_no_generic_restore_stops_the_calibration(monkeypatch):
    bridge = RecordingBridge(controls=("one", "two"), stops_on="one")
    result = run(bridge, monkeypatch, restore=None)
    assert result["status"] == "inconclusive"
    assert "no generic restore" in result["error"]
    # The stopped control is still in the record, with the evidence that stopped the run.
    stopped = trials_for(result, "one")[0]
    assert stopped["valid"] is False and stopped["state_change"]["effect"] == "game clock stopped"
    assert trials_for(result, "two") == []


def test_a_window_that_cannot_be_judged_stops_the_calibration(monkeypatch):
    bridge = RecordingBridge(controls=("one", "two"), rewinds_on="one")
    calls = []
    result = run(bridge, monkeypatch, restore=recorder(bridge, calls))
    assert result["status"] == "inconclusive"
    assert "cannot be trusted" in result["error"] and "went backwards" in result["error"]
    unjudged = trials_for(result, "one")[0]
    assert unjudged["valid"] is False and unjudged["clock"]["verdict"] == "unknown"
    assert unjudged["state_change"] is None
    assert calls == [] and trials_for(result, "two") == []


def test_the_window_evidence_measures_rather_than_guesses(monkeypatch):
    bridge = RecordingBridge(controls=("one",), stops_on="one")
    result = run(bridge, monkeypatch, restore=recorder(bridge))
    clock = trials_for(result, "one")[0]["clock"]
    assert clock["game_rate"] == 0.0
    assert clock["real_time_s"]["delta_s"] == pytest.approx(0.2)
    assert clock["paused"] == {"before": False, "after": True}
    assert clock["time_scale"] == {"before": 1.0, "after": 0.0}
    assert clock["clock_mode"] == {"before": "realtime", "after": "realtime"}
    assert clock["bridge_paused"] is False
    assert "quarter of its real time" in clock["basis"]


class TwoStopBridge(RecordingBridge):
    """The same bridge, with more than one control that stops the game's clock."""

    def __init__(self, controls, stops=()):
        super().__init__(controls=controls)
        self.stops = set(stops)

    def _press(self, control):
        if control in self.stops:
            self.game_frozen = True


def entity_file(directory):
    path = directory / "entity.json"
    path.write_text(json.dumps(selected("one", "two", "three")), encoding="utf-8")
    return path


def pass_prior(bridge, monkeypatch, prior, pass_number=1):
    record = run(bridge, monkeypatch, restore=None, prior=prior)
    new = calibrate.new_state_changes(record, prior)
    return record, new, calibrate.merge_prior(prior, record, pass_number)


def test_a_state_change_with_no_restore_still_writes_its_record(monkeypatch, tmp_path):
    """The pass that stops has to leave its evidence behind, or the next pass cannot know what to skip."""
    result = run(RecordingBridge(controls=("one", "two"), stops_on="one"), monkeypatch, restore=None)
    seen = {}

    async def fake_run(project, selected_record, plan_output=None, prior=None, both_signs=False):
        seen["prior"] = prior
        return result

    monkeypatch.setattr(calibrate, "_run", fake_run)
    project = tmp_path / "project"
    project.mkdir()
    output = tmp_path / "record.json"
    code = calibrate.main(["--project", str(project), "--entity", str(entity_file(tmp_path)),
                           "--output", str(output)])
    assert code == calibrate.EXIT_PASS_ENDED_BY_STATE_CHANGE and seen["prior"] is None
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["status"] == "inconclusive" and "no generic restore" in written["error"]
    stopped = trials_for(written, "one")[0]
    assert stopped["state_change"]["effect"] == "game clock stopped"
    assert stopped["state_change"]["evidence"]["verdict"] == "stopped"
    assert stopped["state_change"]["trial"] == 1


def test_only_this_sessions_own_state_change_ends_a_pass_by_design():
    own_change = {"control": "one", "state_change": {"control": "one"}}
    own_event = {"control": None, "unattributed": True}
    carried_change = dict(own_change, prior_pass=1)
    carried_event = dict(own_event, prior_pass=2)
    assert calibrate.pass_ended_by_state_change({"trials": [own_change]})
    assert calibrate.pass_ended_by_state_change({"trials": [carried_change, own_event]})
    # An earlier session's ending, only carried here, says nothing about why this session stopped.
    assert not calibrate.pass_ended_by_state_change({"trials": [carried_change, carried_event]})
    # A state_change entry with no control is not a measured attribution.
    assert not calibrate.pass_ended_by_state_change({"trials": [{"control": "one", "state_change": {}}]})
    assert not calibrate.pass_ended_by_state_change({"trials": [{"control": "one", "valid": True}]})


def test_a_prior_state_change_is_never_pressed_and_its_trial_is_carried(monkeypatch):
    _, _, prior = pass_prior(TwoStopBridge(("one", "two"), stops=("one",)), monkeypatch,
                             {"state_changes": []})
    second = TwoStopBridge(("one", "two"), stops=("one",))
    result = run(second, monkeypatch, restore=None, prior=prior)
    # The control the prior file lists changed the game's state in an earlier session; this session is
    # not a reset, so pressing it again would only measure a stopped game.
    assert "act:one" not in second.events
    carried = trials_for(result, "one")
    assert len(carried) == 1
    assert carried[0]["prior_pass"] == 1 and carried[0]["valid"] is False
    assert carried[0]["state_change"]["effect"] == "game clock stopped"
    assert carried[0]["state_change"]["evidence"]["verdict"] == "stopped"
    # Everything else is measured exactly as it is without the flag.
    measured = trials_for(result, "two")
    assert measured and all(trial["valid"] is True for trial in measured)
    assert calibrate.new_state_changes(result, prior) == []
    assert result["plan"]["budget"]["trials_recorded"] == len(result["trials"]) - len(carried)


def test_three_passes_one_state_change_at_a_time_end_with_no_new_change(monkeypatch):
    prior = {"state_changes": []}
    passes = []
    for pass_number, stops in enumerate((("one",), ("one", "two")), start=1):
        record, new, prior = pass_prior(TwoStopBridge(("one", "two", "three"), stops=stops),
                                       monkeypatch, prior, pass_number)
        passes.append([trial["control"] for trial in new])
        assert calibrate.new_state_changes(record, prior) == []
    assert passes == [["one"], ["two"]]
    assert [entry["control"] for entry in prior["state_changes"]] == ["one", "two"]
    final = run(TwoStopBridge(("one", "two", "three"), stops=("one", "two")), monkeypatch,
                restore=None, prior=prior)
    assert calibrate.new_state_changes(final, prior) == []
    changes = {trial["control"]: trial for trial in final["trials"]
               if isinstance(trial.get("state_change"), dict)}
    assert set(changes) == {"one", "two"}
    assert all(trial["prior_pass"] in (1, 2) for trial in changes.values())
    assert all(trial["state_change"]["evidence"]["verdict"] == "stopped" for trial in changes.values())


def test_a_pass_takes_one_new_state_change_and_carries_it_onward(monkeypatch):
    prior = {"state_changes": []}
    _, new, prior = pass_prior(TwoStopBridge(("one", "two", "three"), stops=("one",)),
                               monkeypatch, prior, 1)
    assert [trial["control"] for trial in new] == ["one"]
    assert [entry["control"] for entry in prior["state_changes"]] == ["one"]
    _, new, prior = pass_prior(TwoStopBridge(("one", "two", "three"), stops=("one", "two")),
                               monkeypatch, prior, 2)
    assert [trial["control"] for trial in new] == ["two"]
    # The next pass starts knowing both, so its own record carries both.
    assert [entry["control"] for entry in prior["state_changes"]] == ["one", "two"]


def test_a_prior_file_round_trips_through_merge_and_load(monkeypatch, tmp_path):
    record, new, prior = pass_prior(TwoStopBridge(("one", "two"), stops=("one",)), monkeypatch,
                                    {"state_changes": []})
    path = tmp_path / "prior.json"
    path.write_text(json.dumps(prior), encoding="utf-8")
    loaded = calibrate.load_prior_state_changes(path)
    assert [entry["control"] for entry in loaded["state_changes"]] == ["one"]
    assert loaded["state_changes"][0]["state_change"]["effect"] == "game clock stopped"
    assert new and calibrate.new_state_changes(record, loaded) == []


@pytest.mark.parametrize("payload", [
    [],
    "not json",
    {"state_change": []},
    {"state_changes": "one"},
    {"state_changes": [], "pass": 1},
    {"state_changes": [{"control": "one"}]},
    {"state_changes": [{"control": "one", "state_change": None}]},
    {"state_changes": [{"control": "one", "state_change": {"control": "two"}}]},
    {"state_changes": [{"control": "", "state_change": {"control": ""}}]},
    {"state_changes": [{"control": "one", "state_change": {"control": "one"}}] * 2},
])
def test_a_malformed_prior_file_is_rejected(payload, tmp_path):
    path = tmp_path / "prior.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        calibrate.load_prior_state_changes(path)


def test_a_malformed_prior_file_stops_the_run_before_the_session(monkeypatch, tmp_path):
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        return {}

    monkeypatch.setattr(calibrate, "_run", fake_run)
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps({"state_changes": [{"control": "one"}]}), encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    output = tmp_path / "record.json"
    code = calibrate.main(["--project", str(project), "--entity", str(entity_file(tmp_path)),
                           "--output", str(output), "--prior-state-changes", str(prior)])
    assert code == 1 and calls == [] and not output.exists()


def test_the_flag_hands_the_loaded_prior_to_the_run(monkeypatch, tmp_path):
    seen = {}

    async def fake_run(project, selected_record, plan_output=None, prior=None, both_signs=False):
        seen["prior"] = prior
        return {"status": "inconclusive"}

    monkeypatch.setattr(calibrate, "_run", fake_run)
    _, _, prior = pass_prior(TwoStopBridge(("one", "two"), stops=("one",)), monkeypatch,
                             {"state_changes": []})
    path = tmp_path / "prior.json"
    path.write_text(json.dumps(prior), encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    code = calibrate.main(["--project", str(project), "--entity", str(entity_file(tmp_path)),
                           "--output", str(tmp_path / "record.json"), "--prior-state-changes", str(path)])
    assert code == 1
    assert [entry["control"] for entry in seen["prior"]["state_changes"]] == ["one"]


def test_without_a_prior_file_a_pass_is_unchanged(monkeypatch):
    bridge = RecordingBridge(controls=("one", "two"))
    result = run(bridge, monkeypatch, restore=None)
    assert all("prior_pass" not in trial for trial in result["trials"])
    assert len(result["trials"]) == 4
    assert bridge.events.count("act:one") == 2 and bridge.events.count("act:two") == 2
    assert result["plan"]["budget"]["trials_recorded"] == 4
    # A trial that recorded no change is not a state change, structurally.
    assert calibrate.new_state_changes(result, {"state_changes": []}) == []
