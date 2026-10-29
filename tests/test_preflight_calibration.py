from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from playtest.contracts.calibration import CalibrationRecord, HoldMeasurement
from playtest.preflight import calibrate
from playtest.preflight.calibrate import _trial_measure, discover_actions, summarize_action
from playtest.preflight.calibration_record import combine


def binding(path, part="", action="id"):
    return {"id": path, "binding_effective_path": path, "binding_name": part, "action_id": action,
            "expected_control_type": "Vector2"}


def selected_entity(path="object"):
    return {"status": "identified", "entity_id": "entity", "stop": {"release_verified": True},
            "candidate_cap": {"truncated": False},
            "candidates": [{"control": "w", "binding_ids": ["<Keyboard>/w"]}],
            "entity": {"scene_id": "scene", "scene_path": "scene", "path": path}}


def test_discovery_resolves_keyboard_and_pointer_controls_and_skips_composites():
    found, unsupported = discover_actions({"bindings": [binding("<Keyboard>/w", "up"),
        binding("<Keyboard>/shift"), binding("<Keyboard>/1"), binding("<Mouse>/delta"),
        {"id": "composite", "binding_is_composite": True, "binding_effective_path": "2DVector"}]})
    assert [x["control"] for x in found] == ["digit1", "leftshift", "mouse_delta", "w"]
    assert found[0]["resolution"] == "unity_digit_key"
    assert found[1]["resolution"] == "unity_modifier_left_key"
    assert found[2]["device"] == "Mouse" and found[2]["control_type"] == "Delta"
    assert found[2]["x"] == 1.0 and found[2]["y"] == 0.0
    assert unsupported == []


def test_gamepad_controls_and_unsupported_paths_keep_stable_ids_and_reasons():
    found, unsupported = discover_actions({"bindings": [
        binding("<Gamepad>/leftStick", "up"), binding("<Gamepad>/rightTrigger"),
        binding("<Gamepad>/buttonSouth"), binding("<Gamepad>/dpad"),
        binding("<Mouse>/position"), binding("<Mouse>/scroll"),
        binding("<Touchscreen>/primaryTouch/position"),
    ]})
    by_control = {item["control"]: item for item in found}
    assert set(by_control) == {"gamepad_leftstick", "gamepad_righttrigger", "gamepad_buttonsouth", "gamepad_dpad"}
    assert by_control["gamepad_leftstick"]["control_type"] == "Stick"
    assert by_control["gamepad_leftstick"]["device"] == "Gamepad"
    assert by_control["gamepad_righttrigger"]["control_type"] == "Axis"
    assert by_control["gamepad_buttonsouth"]["control_type"] == "Button"
    reasons = {item["path"]: item["reason"] for item in unsupported}
    assert "absolute pointer" in reasons["<Mouse>/position"]
    assert "scroll" in reasons["<Mouse>/scroll"]
    assert "touch" in reasons["<Touchscreen>/primaryTouch/position"]


def test_summary_uses_sample_variance_and_only_reports_tested_effective_hold():
    action = {"control": "a", "resolution": "unity_key_name", "bindings": [{"part": "left"}]}
    def trial(hold, value):
        return {"requested_hold_s": hold, "valid": True, "effect_magnitude": abs(value),
                "effect_delta": [value, 0, 0], "before": {"world_right": [1, 0, 0]},
                "execution": [{"applied_duration_s": hold + 0.01}]}
    summary = summarize_action(action, [trial(0.05, -0.01), trial(0.05, -0.02),
                                        trial(0.1, -1), trial(0.1, -2)])
    assert summary["lowest_tested_effective_requested_hold_s"] == 0.1
    assert summary["shortest_observed_effective_hold_s"] == pytest.approx(0.11)
    measured = summary["holds"][1]
    assert measured["mean_delta"] == [-1.5, 0, 0]
    assert measured["mean_magnitude"] == 1.5
    assert measured["sample_variance"] == 0.5
    assert summary["inverted_relative_to_entity_basis"] is False
    assert summary["dead_zone"] is None
    assert summary["sensitivity"] is None


def test_inversion_and_unobserved_values_are_not_guessed():
    action = {"control": "x", "resolution": "unity_key_name", "bindings": [{"part": "up"}]}
    trial = {"requested_hold_s": 0.1, "valid": True, "effect_magnitude": 1,
             "effect_delta": [0, 0, -1], "before": {"world_forward": [0, 0, 1]}}
    assert summarize_action(action, [trial, trial])["inverted_relative_to_entity_basis"] is True
    empty = summarize_action(action, [])
    assert empty["lowest_tested_effective_requested_hold_s"] is None
    assert empty["shortest_observed_effective_hold_s"] is None
    assert empty["inverted_relative_to_entity_basis"] is None
    assert all(item["mean_magnitude"] is None and item["sample_variance"] is None for item in empty["holds"])


def test_calibration_contract_rejects_numbers_without_evidence():
    row = {"requested_hold_s": 0.1, "valid_trials": 0, "mean_delta": None, "mean_magnitude": None,
           "sample_variance": None, "effective_in_all_repeats": None}
    HoldMeasurement.model_validate(row)
    with pytest.raises(ValidationError, match="mean needs at least one"):
        HoldMeasurement.model_validate(dict(row, mean_magnitude=0.0))
    with pytest.raises(ValidationError, match="variance and repeat effectiveness"):
        HoldMeasurement.model_validate(dict(row, valid_trials=1, mean_delta=[0, 0, 0], mean_magnitude=0,
                                            sample_variance=0))


def test_trial_subtracts_observed_idle_velocity_and_rejects_unverified_release():
    def record(verified):
        return SimpleNamespace(status=SimpleNamespace(value="completed"), release_verified=verified,
                               model_dump=lambda **_: {"release_verified": verified})
    idle = {"position": [0, 0, 0], "real_time_s": 1.0}
    before = {"position": [0.005, 0, 0], "real_time_s": 2.0}
    after = {"position": [1.01, 0, 0], "real_time_s": 3.0}
    value = _trial_measure(before, after, idle, [record(True)], 0.4)
    assert value["effect_delta"][0] == pytest.approx(1.0)
    assert value["valid"] is True
    assert _trial_measure(before, after, idle, [record(False)], 0.4)["valid"] is False
    with pytest.raises(ValueError, match="timestamps"):
        _trial_measure(before, dict(after, real_time_s=2.0), idle, [record(True)], 0.4)


def reload_field(value, field_id="one", comparable=True):
    return {"id": field_id, "state": "present", "kind": "number", "value": value, "comparable": comparable}


def reload_trial(*, before=(5, 0, 0), after=(0.001, 0, 0), initial=("1",), changed=("2",), reloaded=("1",),
                 verified=True):
    return {"key": "a", "requested_hold_s": 1.2, "applied_hold_s": 1.21, "release_verified": verified,
            "initial_position": [0, 0, 0], "before_reload_position": list(before),
            "after_reload_position": list(after),
            "initial_fields": [reload_field(v) for v in initial],
            "before_reload_fields": [reload_field(v) for v in changed],
            "after_reload_fields": [reload_field(v) for v in reloaded]}


CALIBRATED = {"status": "measured_with_gaps", "stop": {"release_verified": True}, "entity_id": "entity"}


def reset_evidence(*trials):
    return {"method": "scene_reload", "entity_id": "entity", "trials": list(trials)}


def test_scene_reload_reports_k_of_n_over_trials_that_moved_the_entity():
    reload = combine(CALIBRATED, reset_evidence(
        reload_trial(),                                  # moved 5, back to start
        reload_trial(after=(0.5, 0, 0), reloaded=("9",)),  # moved 5, not back
        reload_trial(before=(0.4, 0, 0)),                # barely moved: reported, not counted
    ))["reset"]["scene_reload"]

    assert reload["trial_count"] == 3
    assert reload["moved_trial_count"] == 2
    assert reload["position_restored"] == {"restored": 1, "of": 2}
    assert [t["entity_moved_before_reload"] for t in reload["trials"]] == [True, True, False]
    assert [t["entity_position_restored"] for t in reload["trials"]] == [True, False, None]
    assert reload["minimum_pre_reset_movement"] == 1.0
    assert reload["full_state_restored"] is None
    # The one field changed in both moved trials and came back in one of them.
    assert reload["fields"] == [{"id": "one", "trials_compared": 2, "changed_before_reload": 2,
                                 "restored_after_change": 1, "matches_initial_after_reload": 1}]


def test_scene_reload_leaves_unmeasured_values_unknown_and_never_counts_them():
    nothing_moved = combine(CALIBRATED, reset_evidence(reload_trial(before=(0.2, 0, 0))))["reset"]["scene_reload"]
    assert nothing_moved["moved_trial_count"] == 0
    assert nothing_moved["position_restored"] is None
    assert nothing_moved["fields"] == []

    uncertain = reload_trial()
    uncertain["after_reload_fields"] = [reload_field(None, comparable=False)]
    unknown = combine(CALIBRATED, reset_evidence(uncertain))["reset"]["scene_reload"]["fields"]
    assert unknown == [{"id": "one", "trials_compared": 0, "changed_before_reload": None,
                        "restored_after_change": None, "matches_initial_after_reload": None}]


def test_scene_reload_rejects_missing_trials_and_unverified_release():
    with pytest.raises(ValueError, match="no trials"):
        combine(CALIBRATED, reset_evidence())
    with pytest.raises(ValueError, match="verify the key release"):
        combine(CALIBRATED, reset_evidence(reload_trial(verified=False)))
    with pytest.raises(ValueError, match="verified input release"):
        combine(dict(CALIBRATED, stop=None), reset_evidence(reload_trial()))
    with pytest.raises(ValueError, match="does not match"):
        combine(CALIBRATED, dict(reset_evidence(reload_trial()), entity_id="other"))


class FakeCalibrationBridge:
    def __init__(self, reject=False, game_error=False):
        self.t = 0.0
        self.position = 0.0
        self.value = 0
        self.paused = False
        self.closed = False
        self.reject = reject
        self.game_error = game_error
        self.errors = []
        self.held = False
        self.calls = []

    def cap(self):
        return SimpleNamespace(truncated=False, observed=1, limit=100, returned=1,
                               model_dump=lambda **_: {"truncated": False, "observed": 1, "limit": 100, "returned": 1})

    async def inspect(self, stage):
        self.t += 0.1
        cap = self.cap()
        wall = datetime.fromtimestamp(1000 + self.t, timezone.utc)
        if stage == "discovery":
            return SimpleNamespace(stage=stage, errors=[], wall_utc=wall,
                caps={k: cap for k in ("game_objects", "components", "bindings", "input_asset_bindings")},
                data={"game_objects": [{"id": "entity", "scene_id": "scene", "scene_path": "scene", "path": "object"}],
                      "bindings": [binding("<Keyboard>/w", "up")], "input_asset_bindings": [], "components": []})
        if stage == "transforms":
            return SimpleNamespace(stage=stage, errors=[], wall_utc=wall, caps={"game_objects": cap},
                data={"frame": 10, "real_time_s": self.t, "game_time_s": self.t, "time_unit": "seconds",
                      "position_space": "world", "position_unit": "unity_world_unit", "position_order": ["x", "y", "z"],
                      "runtime_errors": {"active": True, "truncated": False, "observed": len(self.errors),
                                         "events": list(self.errors)},
                      "game_objects": [{"id": "entity", "parent_id": None, "position": [0, 0, self.position],
                                        "world_forward": [0, 0, 1], "world_right": [1, 0, 0], "basis_state": "present"}]})
        assert stage == "fields"
        return SimpleNamespace(stage=stage, errors=[], wall_utc=wall, caps={"game_objects": cap, "fields": cap},
            data={"frame": 10, "real_time_s": self.t, "game_time_s": self.t, "time_unit": "seconds",
                  "sample_wall_s": 1000 + self.t,
                  "keyboard_state": {"active": True, "keys": ["w"] if self.held else [],
                                     "real_time_s": self.t, "wall_s": 1000 + self.t,
                                     "update_type": "dynamic", "error": None},
                  "fields": [{"id": "field", "scene_id": "scene", "value_state": "present", "value_kind": "integer",
                              "value": self.value, "is_null": False, "value_truncated": False, "read_error": None}]})

    async def observe_once(self):
        return SimpleNamespace(seq=1, paused=self.paused, game_s=self.t,
                               model_dump=lambda **_: {"paused": self.paused, "game_s": self.t})

    async def clock(self, op, **kwargs):
        if op == "pause": self.paused = True
        if op == "resume": self.paused = False
        if op == "step": self.t += kwargs["step_game_s"]

    def record(self, status, start, end):
        return SimpleNamespace(status=SimpleNamespace(value=status), release_verified=status == "completed",
            input_start_wall_s=start, input_end_wall_s=end,
            model_dump=lambda **_: {"status": status, "release_verified": status == "completed"})

    async def act(self, **kwargs):
        self.calls.append(kwargs["action"])
        if self.reject:
            return [self.record("rejected", None, None)]
        self.position += 1
        if self.game_error:
            self.errors.append({"last_sequence": len(self.errors) + 1, "message": "observed error"})
        return [self.record("completed", 1000 + self.t, 1000 + self.t + 1)]

    async def act_with_inspections(self, **kwargs):
        self.calls.append(kwargs["action"])
        self.position += 1
        self.held = True
        start = 1000 + self.t
        samples = []
        for _ in range(4):
            self.value += 1
            samples.append(await self.inspect("fields"))
        self.held = False
        return [self.record("completed", start, 1000 + self.t + 0.1)], samples


class ScriptedMotionBridge(FakeCalibrationBridge):
    """Serves the entity's height from a script, one value per transform read."""

    def __init__(self, heights):
        super().__init__()
        self.heights = list(heights)
        self.reads = 0

    async def inspect(self, stage):
        response = await super().inspect(stage)
        if stage == "transforms":
            height = self.heights[min(self.reads, len(self.heights) - 1)]
            response.data["game_objects"][0]["position"] = [0, height, 0]
            self.reads += 1
        return response


def test_quiet_window_is_not_fooled_by_motion_that_returns_to_its_start(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    # The first two reads straddle a jump apex and match; the entity is still falling afterwards.
    client = ScriptedMotionBridge([2.0, 2.0, 1.2, 0.08, 0.08, 0.08])
    before, after = calibrate.asyncio.run(calibrate._quiet_window(client, "entity"))
    assert client.reads == 6
    assert before["position"][1] == after["position"][1] == 0.08


def test_quiet_window_needs_consecutive_still_intervals(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    client = ScriptedMotionBridge([0.08])
    calibrate.asyncio.run(calibrate._quiet_window(client, "entity"))
    assert calibrate.QUIET_INTERVALS == 2
    assert client.reads == calibrate.QUIET_INTERVALS + 1


def test_full_calibration_records_action_field_and_clock_evidence(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", (0.1, 0.6))
    client = FakeCalibrationBridge()
    selected = selected_entity()
    result = calibrate.asyncio.run(calibrate.measure(client, selected))
    assert result["status"] == "measured_with_gaps"
    assert result["plan"]["quiet_intervals"] == calibrate.QUIET_INTERVALS
    assert result["plan"]["requested_field_interval_s"] == calibrate.FIELD_INTERVAL_S
    assert result["plan"]["field_repeats"] == calibrate.FIELD_REPEATS
    # Two repeats at the shorter hold, and the field-sampled hold repeated more often.
    assert len(client.calls) == len(result["trials"]) == calibrate.REPEATS + calibrate.FIELD_REPEATS
    assert len(result["input_field_segments"]) == calibrate.FIELD_REPEATS
    assert len(result["idle_fields"]) == calibrate.IDLE_SAMPLES
    assert [len(window) for window in result["later_idle_windows"]] == [calibrate.IDLE_SAMPLES]
    assert result["freshness"]["field"]["idle"]["segment_count"] == 2
    assert result["freshness"]["field"]["input"]["classification"] == "observed_continuous"
    assert result["freshness"]["field"]["held_controls"]["changed"] == ["w"]
    assert result["freshness"]["field"]["input_by_control"]["w"]["window_count"] == calibrate.FIELD_REPEATS
    assert result["action_summaries"][0]["lowest_tested_effective_requested_hold_s"] == 0.1
    assert result["clock"]["stepped_mode_supported"] is None
    assert client.paused is False
    result["stop"] = {"protocol": "upa/1", "session": "s", "seq": 1, "game_s": None,
                      "wall_utc": "2026-10-27T00:00:00Z", "type": "stopped", "released": [],
                      "release_verified": True}
    reset = reset_evidence(reload_trial(before=(0, 0, 3), after=(0, 0, 0), initial=(), changed=(), reloaded=()))
    assert CalibrationRecord.model_validate(combine(result, reset)).status == "measured_with_gaps"


def test_rejected_control_is_not_retried_or_invented(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", (0.1, 0.6))
    client = FakeCalibrationBridge(reject=True)
    selected = selected_entity()
    result = calibrate.asyncio.run(calibrate.measure(client, selected))
    assert client.calls == ["preflight_key_w"]
    assert result["status"] == "inconclusive"
    assert result["trials"][0]["valid"] is False
    assert result["action_summaries"][0]["lowest_tested_effective_requested_hold_s"] is None


def test_fields_are_not_classified_as_input_without_measured_key_state(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", (0.1, 0.6))
    client = FakeCalibrationBridge()
    original = client.act_with_inspections
    async def no_pressed_state(**kwargs):
        records, responses = await original(**kwargs)
        for response in responses:
            response.data["keyboard_state"]["keys"] = []
        return records, responses
    client.act_with_inspections = no_pressed_state
    selected = selected_entity()
    result = calibrate.asyncio.run(calibrate.measure(client, selected))
    assert result["status"] == "inconclusive"
    assert "enough field samples" in result["error"]
    assert all(not segment["samples"] for segment in result["input_field_segments"])


def test_game_error_marks_control_unknown_and_stops_repeating_it(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", (0.1, 0.6))
    client = FakeCalibrationBridge(game_error=True)
    result = calibrate.asyncio.run(calibrate.measure(client, selected_entity()))
    assert client.calls == ["preflight_key_w"]
    assert result["status"] == "inconclusive"
    assert result["trials"][0]["valid"] is False
    assert result["trials"][0]["game_errors"][0]["message"] == "observed error"
    assert result["action_summaries"][0]["lowest_tested_effective_requested_hold_s"] is None


def test_stale_entity_is_rejected_before_any_action():
    client = FakeCalibrationBridge()
    selected = selected_entity("different object")
    with pytest.raises(ValueError, match="differs from the stage 2 record"):
        calibrate.asyncio.run(calibrate.measure(client, selected))
    assert client.calls == []


def test_changed_discovery_binding_is_rejected_before_any_action():
    client = FakeCalibrationBridge()
    selected = selected_entity()
    selected["candidates"][0]["binding_ids"] = ["old-binding"]
    with pytest.raises(ValueError, match="bindings differ"):
        calibrate.asyncio.run(calibrate.measure(client, selected))
    assert client.calls == []


def test_clock_failure_attempts_resume_without_claiming_support(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    client = FakeCalibrationBridge()
    original_clock = client.clock
    async def fail_step(op, **kwargs):
        if op == "step": raise ValueError("step failed")
        await original_clock(op, **kwargs)
    client.clock = fail_step
    result = calibrate.asyncio.run(calibrate.measure_clock(client, "entity", "scene"))
    assert result["stepped_mode_supported"] is None
    assert "step failed" in result["error"]
    assert client.paused is False


def test_unverified_stop_makes_measurement_inconclusive(monkeypatch, tmp_path):
    async def no_delay(_):
        return None
    client = FakeCalibrationBridge()
    async def connect(*args, **kwargs):
        return client
    async def no_stop():
        return None
    client.aclose = no_stop
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", (0.1, 0.6))
    monkeypatch.setattr(calibrate.BridgeClient, "connect_from_project", connect)
    selected = selected_entity()
    result = calibrate.asyncio.run(calibrate._run(tmp_path, selected))
    assert result["status"] == "inconclusive"
    assert result["stop"] is None
    assert "did not verify" in result["error"]


def test_generic_input_state_reports_held_device_controls_without_a_keyboard_alias():
    record = SimpleNamespace(input_start_wall_s=1000.0, input_end_wall_s=1000.2)
    state = {
        "active": True,
        "error": None,
        "update_type": "dynamic",
        "real_time_s": 10.05,
        "wall_s": 1000.1,
        "devices": {
            "Gamepad": {"controls": {"leftStick": {"x": 1.0, "y": 0.0}, "buttonSouth": True}},
            "Mouse": {"controls": {"delta": {"x": 0.5, "y": 0.0}}},
        },
    }
    response = SimpleNamespace(data={"real_time_s": 10.1, "sample_wall_s": 1000.1, "input_state": state})
    assert calibrate._held_during_sample(response, record, "leftStick", "Gamepad") is True
    assert calibrate._held_during_sample(response, record, "buttonSouth", "Gamepad") is True
    assert calibrate._held_during_sample(response, record, "rightTrigger", "Gamepad") is False
    assert calibrate._held_during_sample(response, record, "delta", "Mouse") is True


def test_unity_input_state_pressed_and_values_are_read_per_device():
    state = {"devices": {
        "Keyboard": {"pressed": ["w"], "values": {}},
        "Mouse": {"pressed": ["leftButton"], "values": {"delta": [0.0, 0.0]}},
        "Gamepad": {"pressed": ["buttonSouth"],
                    "values": {"leftStick": [0.5, 0.0], "rightStick": [0.0, 0.0],
                               "leftTrigger": 0.0, "rightTrigger": None}},
    }}
    for device, control in (("Keyboard", "w"), ("Mouse", "leftButton"),
                            ("Gamepad", "buttonSouth"), ("Gamepad", "leftStick")):
        assert calibrate._control_held(state, device, control) is True
    for device, control in (("Keyboard", "a"), ("Mouse", "rightButton"),
                            ("Mouse", "delta"), ("Gamepad", "buttonNorth"),
                            ("Gamepad", "rightStick"), ("Gamepad", "leftTrigger")):
        assert calibrate._control_held(state, device, control) is False
    assert calibrate._control_held(state, "Gamepad", "rightTrigger") is None
    assert calibrate._control_held({"devices": {"Gamepad": None}}, "Gamepad", "buttonSouth") is None
    assert calibrate._active_value([0.0, None]) is None
    assert calibrate._active_value([None, 1.0]) is True
