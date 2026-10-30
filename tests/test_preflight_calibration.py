import json
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
    """A bridge whose error monitor behaves like the Editor's: an exact counter, the newest events."""

    def __init__(self, reject=False, game_error=False, flood=None, monitor_truncated=False):
        self.t = 0.0
        self.position = 0.0
        self.value = 0
        self.paused = False
        self.closed = False
        self.reject = reject
        self.game_error = game_error
        self.flood = flood or {}
        self.errors = []
        self.observed = 0
        self.monitor_active = True
        self.monitor_limit = 256
        self.monitor_truncated = monitor_truncated
        self.held = False
        self.calls = []

    def log_error(self, message, times=1, kinds=1):
        """Log `times` errors; with `kinds` > 1 neighbours differ, so none of them merge."""
        for i in range(times):
            text = message if kinds == 1 else f"{message} {i % kinds}"
            self.observed += 1
            if self.errors and self.errors[-1]["message"] == text:
                self.errors[-1].update(last_sequence=self.observed, count=self.errors[-1]["count"] + 1)
                continue
            if len(self.errors) >= self.monitor_limit:
                self.errors.pop(0)
                self.monitor_truncated = True
            self.errors.append({"type": "Error", "message": text, "stack": "at the game", "count": 1,
                                "first_sequence": self.observed, "last_sequence": self.observed})

    def monitor(self):
        return {"active": self.monitor_active, "limit": self.monitor_limit, "truncated": self.monitor_truncated,
                "observed": self.observed, "events": [dict(event) for event in self.errors]}

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
                      "runtime_errors": self.monitor(),
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
        if op == "pause":
            self.paused = True
            if "monitor_off" in self.flood:
                self.monitor_active = False
        if op == "resume": self.paused = False
        if op == "step": self.t += kwargs["step_game_s"]
        if self.flood.get(op):
            self.log_error("flood", times=self.flood[op], kinds=2)

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
            self.log_error("observed error")
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


def seen(yaw, pitch, seq=1):
    """An observation that reports the game view, as the bridge does."""
    return SimpleNamespace(seq=seq, frame=seq, view=SimpleNamespace(yaw_deg=yaw, pitch_deg=pitch))


@pytest.mark.parametrize("before, after, turn", [
    (10.0, 17.68, 7.68), (17.68, 10.0, -7.68),
    (179.0, -179.0, 2.0),      # across the seam turning right: a small turn, not -358
    (-179.0, 179.0, -2.0),     # and back across it turning left
    (175.0, -172.32, 12.68),
    (0.0, 0.0, 0.0),           # a view that did not move is a measured zero, distinct from an unmeasured one
])
def test_view_turn_wraps_yaw_across_the_seam(before, after, turn):
    value = calibrate.view_turn(calibrate.view_reading(seen(before, 3.0)), calibrate.view_reading(seen(after, 5.5)))
    assert value["yaw_deg"] == pytest.approx(turn)
    assert value["pitch_deg"] == pytest.approx(2.5)  # pitch is bounded by the poles and is not wrapped


def test_a_view_that_was_not_read_is_unknown_never_a_turn_of_zero():
    reading = calibrate.view_reading(seen(10.0, 0.0))
    assert calibrate.view_turn(reading, None) is None and calibrate.view_turn(None, reading) is None
    assert calibrate.view_reading(SimpleNamespace(seq=1, frame=1)) is None              # an older bridge: no view at all
    assert calibrate.view_reading(SimpleNamespace(seq=1, frame=1, view=None)) is None   # no game-view camera
    assert calibrate.view_reading(seen(float("nan"), 0.0)) is None
    assert calibrate.view_reading(seen(10.0, None)) is None                              # a half view is not a view
    assert calibrate.view_reading(seen(True, 0.0)) is None


def test_summary_averages_only_valid_trials_that_measured_the_view():
    action = {"control": "a", "resolution": "unity_key_name", "bindings": []}
    def trial(hold, turn, valid=True):
        return {"requested_hold_s": hold, "valid": valid, "effect_magnitude": 0, "effect_delta": [0, 0, 0],
                "view_delta": turn, "execution": []}
    summary = summarize_action(action, [
        trial(0.05, {"yaw_deg": 7.5, "pitch_deg": 0.5}), trial(0.05, {"yaw_deg": 7.9, "pitch_deg": -0.5}),
        trial(0.05, {"yaw_deg": 90.0, "pitch_deg": 0.0}, valid=False),   # an invalid trial is never averaged
        trial(0.1, {"yaw_deg": 3.0, "pitch_deg": 1.0}), trial(0.1, None),   # one reading was not measured
        trial(0.2, None), trial(0.2, None),                                 # none was measured
    ])
    first, second, third = summary["holds"][:3]
    assert first["mean_view_turn"] == pytest.approx({"trials": 2, "yaw_deg": 7.7, "pitch_deg": 0.0, "spread_deg": 1.0})
    assert first["valid_trials"] == 2
    assert second["mean_view_turn"] == {"trials": 1, "yaw_deg": 3.0, "pitch_deg": 1.0, "spread_deg": 0.0}
    assert second["valid_trials"] == 2
    assert third["mean_view_turn"] is None and third["valid_trials"] == 2
    assert all(hold["mean_view_turn"] is None for hold in summary["holds"][3:])
    assert summarize_action(action, [])["holds"][0]["mean_view_turn"] is None
    HoldMeasurement.model_validate({"requested_hold_s": 0.05, "valid_trials": 2, "mean_delta": [0, 0, 0],
                                    "mean_magnitude": 0, "sample_variance": 0, "effective_in_all_repeats": False,
                                    "mean_view_turn": first["mean_view_turn"]})


def test_a_turn_past_half_a_circle_shows_as_scattered_repeats_not_as_a_clean_mean():
    # Right-stick holds of 1.2 s on FPSKit turned the view more than a full circle: the wrapped change of four
    # repeats was 37.4, 37.1, 72.3 and 62.5 degrees. The mean looks like a measurement; the spread says it is not.
    scattered = calibrate.summarize_view_turn([{"yaw_deg": yaw, "pitch_deg": 0.0} for yaw in (37.376, 37.112, 72.284, 62.524)])
    assert scattered["trials"] == 4 and scattered["yaw_deg"] == pytest.approx(52.324)
    assert scattered["spread_deg"] == pytest.approx(35.172)
    # Repeats of a turn that is read correctly agree, and a single trial cannot show a spread.
    steady = calibrate.summarize_view_turn([{"yaw_deg": 144.215, "pitch_deg": 0.0}, {"yaw_deg": 144.266, "pitch_deg": 0.0}])
    assert steady["spread_deg"] == pytest.approx(0.051)
    assert calibrate.summarize_view_turn([{"yaw_deg": 7.68, "pitch_deg": 0.0}])["spread_deg"] == 0.0
    # The larger of the two axes' ranges, and nothing for a control whose view was never read.
    assert calibrate.summarize_view_turn([{"yaw_deg": 0.0, "pitch_deg": -12.3}, {"yaw_deg": 0.5, "pitch_deg": -6.2}]
                                         )["spread_deg"] == pytest.approx(6.1)
    assert calibrate.summarize_view_turn([]) is None


class TurningBridge(FakeCalibrationBridge):
    """A bridge whose game view turns right by `turn` degrees on every act, wrapping at the +/-180 seam."""

    def __init__(self, turn=7.68, yaw=175.0, pitch=2.0, view=True):
        super().__init__()
        self.turn, self.yaw, self.pitch, self.reports_view = turn, yaw, pitch, view
        self.observed_views = 0

    def spin(self):
        self.yaw = (self.yaw + self.turn + 180.0) % 360.0 - 180.0

    async def act(self, **kwargs):
        records = await super().act(**kwargs)
        self.spin()
        return records

    async def act_with_inspections(self, **kwargs):
        result = await super().act_with_inspections(**kwargs)
        self.spin()
        return result

    async def observe_once(self):
        observation = await super().observe_once()
        self.observed_views += 1
        observation.frame = self.observed_views
        observation.view = SimpleNamespace(yaw_deg=self.yaw, pitch_deg=self.pitch) if self.reports_view else None
        return observation


def test_calibration_records_the_view_turn_of_every_trial_across_the_seam(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", (0.1, 0.6))
    client = TurningBridge()
    result = calibrate.asyncio.run(calibrate.measure(client, selected_entity()))
    assert result["status"] == "measured_with_gaps"
    assert result["plan"]["view_turn"]["yaw"].startswith("the change wrapped to [-180, 180)")
    trials = [t for t in result["trials"] if t["valid"] is True]
    assert len(trials) == calibrate.REPEATS + calibrate.FIELD_REPEATS
    for trial in trials:
        assert trial["view_delta"]["yaw_deg"] == pytest.approx(7.68)   # the view crossed +/-180 within the run
        assert trial["view_delta"]["pitch_deg"] == pytest.approx(0.0)
        assert trial["view_before"]["yaw_deg"] != trial["view_after"]["yaw_deg"]
        assert trial["view_before"]["seq"] == trial["observation_seq"]
    assert any(t["view_before"]["yaw_deg"] > 0 > t["view_after"]["yaw_deg"] for t in trials)
    summary = result["action_summaries"][0]
    assert [hold["mean_view_turn"]["trials"] for hold in summary["holds"][:2]] == [calibrate.REPEATS, calibrate.FIELD_REPEATS]
    assert all(hold["mean_view_turn"]["yaw_deg"] == pytest.approx(7.68) for hold in summary["holds"][:2])
    result["stop"] = {"protocol": "upa/1", "session": "s", "seq": 1, "game_s": None,
                      "wall_utc": "2026-10-27T00:00:00Z", "type": "stopped", "released": [], "release_verified": True}
    reset = reset_evidence(reload_trial(before=(0, 0, 3), after=(0, 0, 0), initial=(), changed=(), reloaded=()))
    record = CalibrationRecord.model_validate(combine(result, reset))
    assert record.action_summaries[0].holds[0].mean_view_turn.yaw_deg == pytest.approx(7.68)


def test_calibration_without_a_game_view_leaves_every_turn_unknown(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", (0.1, 0.6))
    result = calibrate.asyncio.run(calibrate.measure(TurningBridge(view=False), selected_entity()))
    assert result["status"] == "measured_with_gaps"          # the view is extra evidence, not a gate
    assert all(t["view_delta"] is None and t["view_before"] is None for t in result["trials"])
    assert all(hold["mean_view_turn"] is None for hold in result["action_summaries"][0]["holds"])


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


def monitor_event(first, last=None, message="m", count=None):
    last = first if last is None else last
    return {"type": "Error", "message": message, "stack": "s", "first_sequence": first, "last_sequence": last,
            "count": last - first + 1 if count is None else count}


def monitor(observed, events, limit=256, truncated=False, active=True):
    return {"active": active, "limit": limit, "truncated": truncated, "observed": observed, "events": events}


def test_error_window_counts_exactly_and_covers_only_its_own_errors():
    window = calibrate.error_window(monitor(7, [monitor_event(1, 2, "old"), monitor_event(3, 7, "new")]), 2)
    assert window["available"] is True
    assert (window["count"], window["sampled"], window["complete"]) == (5, 5, True)
    assert [kind["message"] for kind in window["kinds"]] == ["new"]
    # A run that straddles the window start contributes only the errors after it.
    straddling = calibrate.error_window(monitor(6, [monitor_event(1, 6, "run")]), 3)
    assert (straddling["count"], straddling["sampled"], straddling["complete"]) == (3, 3, True)
    assert straddling["kinds"][0]["occurrences_in_sample"] == 3
    quiet = calibrate.error_window(monitor(4, [monitor_event(1, 4)]), 4)
    assert (quiet["count"], quiet["sampled"], quiet["complete"], quiet["kinds"]) == (0, 0, True, [])


def test_error_window_states_truncation_instead_of_hiding_it():
    # 1,100 errors were counted but the monitor kept the newest 256 distinct events.
    events = [monitor_event(845 + i, message=f"m{i % 2}") for i in range(256)]
    window = calibrate.error_window(monitor(1100, events, truncated=True), 0)
    assert window["count"] == 1100 and window["sampled"] == 256 and window["complete"] is False
    assert "at least 256 of 1100" in window["note"] and "at most 256" in window["note"]
    assert window["kinds_total"] == 2
    # The same truncated monitor says nothing bad about a later window that its events fully cover.
    later = calibrate.error_window(monitor(1103, events[3:] + [monitor_event(1101), monitor_event(1102),
                                                                monitor_event(1103)], truncated=True), 1100)
    assert (later["count"], later["sampled"], later["complete"]) == (3, 3, True)


def test_error_window_sample_is_bounded_and_unmeasured_monitors_are_unknown():
    many = [monitor_event(i + 1, message="x" * 1000 + str(i)) for i in range(20)]
    window = calibrate.error_window(monitor(20, many), 0)
    assert window["kinds_total"] == 20 and len(window["kinds"]) == calibrate.ERROR_SAMPLE_KINDS
    assert all(len(kind["message"]) == calibrate.ERROR_TEXT_CHARS for kind in window["kinds"])
    for bad, previous in ((monitor(5, [], active=False), 0), (None, 0), (monitor(3, []), 5),
                          (monitor(5, [monitor_event(0)]), 0), (monitor(1, [monitor_event(1, 3)]), 0),
                          (monitor(5, []), None)):
        unknown = calibrate.error_window(bad, previous)
        assert unknown["available"] is False and unknown["count"] is None and unknown["complete"] is None
        assert unknown["reason"]


def completed_run(client, monkeypatch, **kwargs):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", (0.1, 0.6))
    return calibrate.asyncio.run(calibrate.measure(client, selected_entity(), **kwargs))


def test_errors_logged_while_paused_are_clock_evidence_and_not_a_dead_record(monkeypatch):
    client = FakeCalibrationBridge(flood={"pause": 1100, "step": 438, "resume": 25})
    result = completed_run(client, monkeypatch)
    assert result["status"] == "measured_with_gaps" and result["error"] is None
    assert all(trial["valid"] is True for trial in result["trials"])
    clock = result["clock"]
    assert clock["stepped_mode_supported"] is None
    assert "unknown" in clock["reason"] and "runtime_errors" in clock["reason"]
    errors = clock["runtime_errors"]
    windows = errors["windows"]
    assert list(windows) == list(calibrate.CLOCK_WINDOWS)
    assert [windows[name]["count"] for name in calibrate.CLOCK_WINDOWS] == [1100, 0, 438, 0, 25, 0]
    assert errors["total_count"] == 1563 and errors["errors_observed"] is True
    assert errors["sample_complete"] is False and "truncated" in errors["gap"]
    assert windows["at_pause"]["complete"] is False and windows["while_paused"]["complete"] is True
    assert windows["after_step"]["complete"] is False
    # The window after the flood is complete: the flood did not fill the sample for the readers after it.
    assert windows["resume"]["complete"] is True and windows["running"]["complete"] is True
    assert windows["running"]["count"] == 0, "no error while the clock runs again"
    assert all(window["meaning"] for window in windows.values())
    assert 0 < len(windows["at_pause"]["kinds"]) <= calibrate.ERROR_SAMPLE_KINDS
    assert result["runtime_errors"]["observed"] == 1563
    assert client.paused is False
    result["stop"] = {"protocol": "upa/1", "session": "s", "seq": 1, "game_s": None,
                      "wall_utc": "2026-10-27T00:00:00Z", "type": "stopped", "released": [],
                      "release_verified": True}
    reset = reset_evidence(reload_trial(before=(0, 0, 3), after=(0, 0, 0), initial=(), changed=(), reloaded=()))
    assert CalibrationRecord.model_validate(combine(result, reset)).status == "measured_with_gaps"


def test_a_quiet_clock_phase_reports_zero_errors_as_measured(monkeypatch):
    result = completed_run(FakeCalibrationBridge(), monkeypatch)
    errors = result["clock"]["runtime_errors"]
    assert result["status"] == "measured_with_gaps"
    assert errors["total_count"] == 0 and errors["errors_observed"] is False
    assert errors["sample_complete"] is True and errors["gap"] is None
    assert result["clock"]["reason"] == calibrate.UNKNOWN_STEPPED_REASON


@pytest.mark.parametrize("last_read", ["to_resume", "resume"])
@pytest.mark.parametrize("errors_per_window", [0, 2])
def test_missing_trailing_clock_reads_leave_the_total_unknown(last_read, errors_per_window):
    names = calibrate.CLOCK_READS[:calibrate.CLOCK_READS.index(last_read) + 1]
    reads = {}
    for index, name in enumerate(names):
        observed = index * errors_per_window
        events = [monitor_event(1, observed)] if observed else []
        reads[name] = {"runtime_errors": monitor(observed, events)}

    errors = calibrate.clock_error_evidence(reads)
    assert errors["total_count"] is None
    assert errors["errors_observed"] is (True if errors_per_window else None)
    assert errors["sample_complete"] is None
    assert "did not reach:" in errors["gap"] and "running" in errors["gap"]
    assert errors["windows"][last_read]["count"] == errors_per_window


def test_errors_logged_by_the_resume_land_in_the_resume_window_not_the_running_one(monkeypatch):
    client = FakeCalibrationBridge(flood={"resume": 40})
    windows = completed_run(client, monkeypatch)["clock"]["runtime_errors"]["windows"]
    assert windows["resume"]["count"] == 40 and windows["running"]["count"] == 0


def test_errors_logged_while_the_clock_runs_again_are_counted_in_the_running_window(monkeypatch):
    client = FakeCalibrationBridge()
    async def sleeping(seconds):
        if seconds == calibrate.RUNNING_OBSERVATION_S and not client.paused:
            client.log_error("fault while the clock runs", times=7)
    monkeypatch.setattr(calibrate.asyncio, "sleep", sleeping)
    clock = calibrate.asyncio.run(calibrate.measure_clock(client, "entity", "scene"))
    errors = clock["runtime_errors"]
    assert errors["windows"]["running"]["count"] == 7 and errors["windows"]["resume"]["count"] == 0
    assert errors["windows"]["running"]["kinds"][0]["message"] == "fault while the clock runs"
    assert errors["errors_observed"] is True and errors["total_count"] == 7
    assert "clock windows: running;" in clock["reason"]


def test_a_clock_step_that_fails_leaves_the_missing_windows_as_gaps_and_still_resumes(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    client = FakeCalibrationBridge(flood={"pause": 30})
    original_clock = client.clock
    async def fail_step(op, **kwargs):
        if op == "step":
            raise ValueError("step failed")
        await original_clock(op, **kwargs)
    client.clock = fail_step
    clock = calibrate.asyncio.run(calibrate.measure_clock(client, "entity", "scene"))
    errors = clock["runtime_errors"]
    assert client.paused is False and "step failed" in clock["error"]
    assert errors["windows"]["at_pause"]["count"] == 30 and errors["windows"]["while_paused"]["count"] == 0
    assert "after_step" not in errors["windows"]
    assert "did not reach: after_step" in errors["gap"]
    assert errors["windows"]["to_resume"]["available"] is False
    assert "after_step read that opens this window was not taken" in errors["windows"]["to_resume"]["reason"]
    assert errors["windows"]["resume"]["available"] is True and errors["windows"]["running"]["available"] is True
    assert errors["total_count"] is None and errors["errors_observed"] is True
    assert errors["sample_complete"] is None
    assert clock["stepped_mode_supported"] is None


def test_a_failed_read_before_the_resume_does_not_keep_the_clock_paused(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    client = FakeCalibrationBridge()
    original = calibrate._read_entity
    reads = []
    async def flaky(bridge, entity_id):
        reads.append(1)
        if len(reads) == 5:
            raise ValueError("the read before the resume failed")
        return await original(bridge, entity_id)
    monkeypatch.setattr(calibrate, "_read_entity", flaky)
    clock = calibrate.asyncio.run(calibrate.measure_clock(client, "entity", "scene"))
    assert client.paused is False
    assert "read before resume failed" in clock["error"]
    windows = clock["runtime_errors"]["windows"]
    assert "to_resume" not in windows and windows["resume"]["available"] is False
    assert windows["running"]["available"] is True


def test_a_monitor_that_goes_dark_in_the_clock_phase_is_a_gap_not_zero(monkeypatch):
    result = completed_run(FakeCalibrationBridge(flood={"monitor_off": True}), monkeypatch)
    errors = result["clock"]["runtime_errors"]
    assert result["status"] == "measured_with_gaps"
    assert errors["total_count"] is None and errors["errors_observed"] is None
    assert errors["sample_complete"] is None and "unavailable" in errors["gap"]
    assert all(window["count"] is None for window in errors["windows"].values())
    assert result["clock"]["stepped_mode_supported"] is None


def test_a_truncated_monitor_in_a_kept_phase_still_makes_the_run_inconclusive(monkeypatch):
    client = FakeCalibrationBridge(monitor_truncated=True)
    result = completed_run(client, monkeypatch)
    assert result["status"] == "inconclusive"
    assert "unavailable or truncated" in result["error"]
    assert client.calls == [] and result["trials"] == []


def test_an_unavailable_monitor_in_a_kept_phase_still_makes_the_run_inconclusive(monkeypatch):
    client = FakeCalibrationBridge()
    client.monitor_active = False
    result = completed_run(client, monkeypatch)
    assert result["status"] == "inconclusive" and "unavailable or truncated" in result["error"]
    assert result["clock"] is None


def test_errors_in_the_later_idle_window_are_not_clock_evidence(monkeypatch):
    client = FakeCalibrationBridge()
    original = calibrate._idle_window
    calls = []
    async def idle(bridge, scene_id):
        window = await original(bridge, scene_id)
        calls.append(1)
        if len(calls) == 2:
            bridge.log_error("logged with no input running")
        return window
    monkeypatch.setattr(calibrate, "_idle_window", idle)
    result = completed_run(client, monkeypatch)
    assert result["status"] == "inconclusive"
    assert "outside a completed action window" in result["error"]
    assert result["clock"] is None


def test_a_flood_in_the_clock_phase_cannot_hide_an_error_in_the_next_window():
    # After the flood the monitor is truncated, yet a window that begins after it still sees its own error.
    client = FakeCalibrationBridge()
    client.log_error("flood", times=600, kinds=2)
    assert client.monitor_truncated is True
    before = client.observed
    client.log_error("a different error")
    window = calibrate.error_window(client.monitor(), before)
    assert (window["count"], window["sampled"], window["complete"]) == (1, 1, True)
    assert window["kinds"][0]["message"] == "a different error"


def test_the_budget_scales_with_the_planned_controls_and_trials(monkeypatch):
    monkeypatch.setattr(calibrate, "HOLD_S", (0.1, 0.6))
    assert calibrate.trial_schedule() == [0.1] * calibrate.REPEATS + [0.6] * calibrate.FIELD_REPEATS
    one, two, many = (calibrate.measurement_budget(n) for n in (1, 2, 23))
    per_control = one["wall_budget_s"] - calibrate.FIXED_ALLOWANCE_S
    assert per_control == pytest.approx(len(calibrate.trial_schedule()) * calibrate.TRIAL_ALLOWANCE_S
                                        + sum(calibrate.trial_schedule()))
    assert two["wall_budget_s"] == pytest.approx(calibrate.FIXED_ALLOWANCE_S + 2 * per_control)
    assert many["planned_trials"] == 23 * len(calibrate.trial_schedule())
    assert many["wall_budget_s"] > 20 * per_control
    assert calibrate.measurement_budget(0)["wall_budget_s"] == calibrate.FIXED_ALLOWANCE_S


def test_a_calibration_of_many_controls_is_not_held_to_the_old_fixed_limit():
    assert calibrate.measurement_budget(23)["wall_budget_s"] > 300.0
    assert calibrate.measurement_budget(calibrate.MAX_ACTIONS)["wall_budget_s"] > \
        calibrate.measurement_budget(10)["wall_budget_s"]


def test_the_record_states_the_budget_it_used_and_the_time_it_took(monkeypatch):
    announced = []
    client = FakeCalibrationBridge()
    result = completed_run(client, monkeypatch, on_plan=lambda plan: announced.append((len(client.calls), plan)))
    budget = result["plan"]["budget"]
    assert result["plan"]["wall_budget_s"] == budget["wall_budget_s"]
    assert budget["planned_controls"] == 1
    assert budget["planned_trials"] == len(calibrate.trial_schedule())
    assert budget["trials_recorded"] == len(result["trials"]) == len(client.calls)
    assert budget["elapsed_s"] is not None and 0 <= budget["elapsed_s"] < budget["wall_budget_s"]
    # The plan is announced once, before the first trial, with the same numbers the record keeps.
    assert [(calls, plan["budget"]["wall_budget_s"]) for calls, plan in announced] == [(0, budget["wall_budget_s"])]


def test_exhausting_the_budget_is_reported_with_the_budget_that_applied(monkeypatch):
    real_sleep = calibrate.asyncio.sleep
    async def slow(_):
        await real_sleep(0.01)
    monkeypatch.setattr(calibrate.asyncio, "sleep", slow)
    monkeypatch.setattr(calibrate, "HOLD_S", (0.001, 0.002))
    monkeypatch.setattr(calibrate, "FIXED_ALLOWANCE_S", 0.0)
    monkeypatch.setattr(calibrate, "TRIAL_ALLOWANCE_S", 0.0)
    result = calibrate.asyncio.run(calibrate.measure(FakeCalibrationBridge(), selected_entity()))
    assert result["status"] == "inconclusive"
    assert "wall-clock budget of 0 s" in result["error"]
    assert result["plan"]["budget"]["elapsed_s"] > 0


def test_the_plan_is_published_whole_before_the_run_and_never_over_an_existing_file(tmp_path, monkeypatch):
    plan = {"budget": calibrate.measurement_budget(23)}
    path = tmp_path / "out" / "calibration.plan.json"
    calibrate.write_plan(path, plan)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1, "planned_controls": 23, "planned_trials": 276,
        "wall_budget_s": plan["budget"]["wall_budget_s"]}
    assert [p.name for p in path.parent.iterdir()] == ["calibration.plan.json"]
    with pytest.raises(FileExistsError):
        calibrate.write_plan(path, plan)
    assert [p.name for p in path.parent.iterdir()] == ["calibration.plan.json"]

    async def no_delay(_):
        return None
    client = FakeCalibrationBridge()
    async def connect(*args, **kwargs):
        return client
    async def stop():
        return SimpleNamespace(release_verified=True, model_dump=lambda **_: {"release_verified": True})
    client.aclose = stop
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)
    monkeypatch.setattr(calibrate, "HOLD_S", (0.1, 0.6))
    monkeypatch.setattr(calibrate.BridgeClient, "connect_from_project", connect)
    published = tmp_path / "run.plan.json"
    result = calibrate.asyncio.run(calibrate._run(tmp_path, selected_entity(), published))
    assert result["status"] == "measured_with_gaps"
    assert json.loads(published.read_text(encoding="utf-8"))["wall_budget_s"] == result["plan"]["wall_budget_s"]


def test_the_command_refuses_a_plan_file_inside_the_project_or_over_the_record(tmp_path):
    entity = tmp_path / "entity.json"
    entity.write_text("{}", encoding="utf-8")
    project = tmp_path / "game"
    project.mkdir()
    outside = tmp_path / "out"
    base = ["--project", str(project), "--entity", str(entity), "--output", str(outside / "calibration.json")]
    for plan in (project / "plan.json", outside / "calibration.json"):
        with pytest.raises(SystemExit) as raised:
            calibrate.main(base + ["--plan-output", str(plan)])
        assert raised.value.code == 2
