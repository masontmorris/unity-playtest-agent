"""Stage 7 keeps one selected action and its measured evidence distinct from guesses."""

import asyncio
import copy
import json
import math
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from playtest.contracts.calibration import CalibrationRecord
from playtest.contracts.manifest import PreflightManifest
from playtest.preflight import calibrate, verify


ENTITY_ID = "scene:Assets/Scenes/Main.unity@1::0"
SCENE_ID = "scene:Assets/Scenes/Main.unity@1"
SCENE_PATH = "Assets/Scenes/Main.unity"
ENTITY = {"id": ENTITY_ID, "scene_id": SCENE_ID, "scene_path": SCENE_PATH, "parent_id": None, "path": "Player"}


def cap(n=1):
    return {"limit": 100, "observed": n, "returned": n, "truncated": False}


def hold(requested=0.4, effective=True):
    return {"requested_hold_s": requested, "valid_trials": 2, "mean_delta": [0.0, 0.0, 1.0],
            "mean_magnitude": 1.0, "sample_variance": 0.0008,
            "effective_in_all_repeats": effective}


def summary(control="w", requested=0.4, effective=True):
    return {"control": control, "bindings": [], "resolution": "unity_key_name",
            "holds": [hold(requested, effective)], "lowest_tested_effective_requested_hold_s": requested,
            "shortest_observed_effective_hold_s": requested + 0.01,
            "minimum_hold_scope": "the first requested group with responses in both repetitions",
            "inverted_relative_to_entity_basis": False, "signed_basis_projections": [1.0],
            "dead_zone": None, "dead_zone_reason": "keyboard backend supplies binary input only"}


def action_dict(name="walk_forward", controls=("w",), requested=0.4, effective=True, axis=False, x=None, y=None):
    summaries = [summary(control, requested, effective) for control in controls]
    supported = len(controls) == 1 and not axis and requested <= 3600
    return {"name": name, "intent": "move the controlled entity", "inputs": [
                {"control_id": control, "x": x, "y": y} for control in controls],
            "hold_s": requested, "tap_hold_s": None, "preconditions": [], "calibrations": summaries,
            "axis": axis, "max_hold_s": None,
            "hold_effect": effective if len(controls) == 1 else None,
            "execution_supported": supported}


def manifest_data(action):
    return {
        "schema_version": 2, "packet_sha256": "0" * 64, "entity_id": ENTITY_ID, "entity": copy.deepcopy(ENTITY),
        "inventory_caps": {}, "discovery_caps": {}, "image": None, "readers": [], "actions": [action],
        "skills": [], "progress": [], "invariants": [],
        "gaps": {"scope": "model_report_relative_to_offered_and_selected_evidence",
                 "unobservable": ["no readable field was selected"], "uncontrollable": [], "unjudgeable": [],
                 "coverage": []},
        "selection": {"offered_fields": [], "unselected_field_ids": [], "unoffered_fields": [],
                      "offered_controls": [{"id": item["control_id"], "control_type": "Button"}
                                           for item in action["inputs"]],
                      "unoffered_controls": [], "unsupported_bindings": [],
                      "reader_cap": 16, "reader_cap_reached": False, "action_cap": 14,
                      "action_cap_reached": False, "skill_cap": 6, "skill_cap_reached": False,
                      "gap_caps_reached": {"unobservable": False, "uncontrollable": False, "unjudgeable": False}},
        "reset_bindings": None, "reset_bindings_reason": "no callable reset was discovered",
    }


def manifest(action=None):
    return PreflightManifest.model_validate(manifest_data(action or action_dict()))


def calibration_trial(magnitude=1.0, control="w", requested=0.4, forward=(0.0, 0.0, 1.0),
                      right=(1.0, 0.0, 0.0)):
    return {"control": control, "requested_hold_s": requested, "valid": True,
            "effect_delta": [0.0, 0.0, magnitude], "effect_magnitude": magnitude,
            "before": {"basis_state": "present", "world_forward": list(forward), "world_right": list(right)},
            "execution": [{"applied_duration_s": requested + 0.01, "status": "completed",
                           "release_verified": True}]}


# Calibration's three reload trials restored the fixture field every time.
RELOAD_STABLE = {"id": "field", "trials_compared": 3, "changed_before_reload": 1,
                 "restored_after_change": 1, "matches_initial_after_reload": 3}


def calibration_record(action=None, trials=None, *, status="measured_with_gaps", entity=None):
    chosen = action or action_dict()
    data = {
        "schema_version": 1, "status": status, "error": None, "entity_id": ENTITY_ID,
        "entity": copy.deepcopy(entity or ENTITY), "coordinate_frame": "Unity world space",
        "units": "Unity world units", "physical_unit": None,
        "plan": {"effect_tolerance": 0.03, "idle_tolerance": 0.01},
        "action_cap": cap(1), "actions": [], "unsupported_bindings": [], "discovery_coverage": {},
        "idle_fields": [], "input_field_segments": [],
        "trials": list(trials if trials is not None else [calibration_trial(0.98), calibration_trial(1.02)]),
        "action_summaries": chosen["calibrations"],
        "freshness": {"field": {"classification": "unknown"}}, "clock": {},
        "reset": {"scene_reload": {"fields": [RELOAD_STABLE]}},
        "runtime_errors": None,
        "stop": {"protocol": "upa/0", "session": "s", "seq": 1, "game_s": None,
                 "wall_utc": "2026-01-01T00:00:00Z", "type": "stopped", "released": [],
                 "release_verified": True},
    }
    if status == "inconclusive":
        data.update(stop=None, trials=[], freshness=None, clock=None, reset=None)
    return CalibrationRecord.model_validate(data)


def selected(action=None, trials=None):
    return verify.select_action(manifest(action), calibration_record(action, trials), "walk_forward")


class FakeBridge:
    def __init__(self, *, effect=(0.0, 0.0, 1.0), reject=False, release_verified=True,
                 runtime_errors_after=False, forward=(0.0, 0.0, 1.0), right=(1.0, 0.0, 0.0),
                 field_value=7, hold_overrun_s=0.0):
        self.hold_overrun_s = hold_overrun_s
        self.t = 0.0
        self.position = [0.0, 0.0, 0.0]
        self.effect = list(effect)
        self.reject = reject
        self.release_verified = release_verified
        self.runtime_errors_after = runtime_errors_after
        self.forward = list(forward)
        self.right = list(right)
        self.error_events = []
        self.closed = False
        self.act_calls = []
        self.observed = False
        self.fields = [{"id": "field", "scene_id": SCENE_ID, "value_state": "present",
                        "value_kind": "integer", "value": field_value, "is_null": False,
                        "value_truncated": False, "read_error": None}]

    def capsule(self, n=1):
        return SimpleNamespace(truncated=False, observed=n, limit=100, returned=n,
                               model_dump=lambda **_: cap(n))

    def stamp(self):
        return datetime.fromtimestamp(1000 + self.t, timezone.utc)

    async def inspect(self, stage):
        self.t += 0.1
        wall = self.stamp()
        if stage == "discovery":
            caps = {name: self.capsule() for name in
                    ("game_objects", "components", "bindings", "input_asset_bindings")}
            return SimpleNamespace(stage=stage, errors=[], wall_utc=wall, caps=caps,
                                   data={"game_objects": [copy.deepcopy(ENTITY)], "components": [],
                                         "bindings": [], "input_asset_bindings": []})
        if stage == "transforms":
            return SimpleNamespace(
                stage=stage, errors=[], wall_utc=wall, caps={"game_objects": self.capsule()},
                data={"frame": 10, "real_time_s": self.t, "game_time_s": self.t,
                      "time_unit": "seconds", "position_space": "world",
                      "position_unit": "unity_world_unit", "position_order": ["x", "y", "z"],
                      "runtime_errors": {"active": True, "truncated": False,
                                         "observed": len(self.error_events),
                                         "events": list(self.error_events)},
                      "game_objects": [{"id": ENTITY_ID, "parent_id": None, "position": list(self.position),
                                        "world_forward": list(self.forward), "world_right": list(self.right),
                                        "basis_state": "present"}]})
        assert stage == "fields"
        return SimpleNamespace(
            stage=stage, errors=[], wall_utc=wall,
            caps={"game_objects": self.capsule(), "fields": self.capsule()},
            data={"frame": 10, "real_time_s": self.t, "game_time_s": self.t, "time_unit": "seconds",
                  "sample_wall_s": 1000 + self.t,
                  "keyboard_state": {"active": True, "keys": [], "real_time_s": self.t,
                                     "wall_s": 1000 + self.t, "update_type": "dynamic", "error": None},
                  "fields": [copy.deepcopy(field) for field in self.fields]})

    async def observe_once(self):
        self.observed = True
        return SimpleNamespace(seq=1, paused=False, game_s=self.t,
                               model_dump=lambda **_: {"paused": False, "game_s": self.t})

    def record(self, status, release_verified, **extra):
        payload = {"status": status, "release_verified": release_verified, **extra}
        return SimpleNamespace(status=SimpleNamespace(value=status), release_verified=release_verified,
                               input_start_wall_s=1000 + self.t,
                               input_end_wall_s=1000 + self.t + 0.4,
                               model_dump=lambda **_: payload)

    async def act(self, **kwargs):
        self.act_calls.append(kwargs)
        if self.reject:
            return [self.record("rejected", None, reject_reason="unknown_action")]
        self.position = list(self.effect)
        if self.runtime_errors_after:
            self.error_events.append({"last_sequence": len(self.error_events) + 1,
                                      "message": "observed error"})
        return [self.record("completed", self.release_verified,
                            applied_duration_s=kwargs["lease_s"] + 0.01 + self.hold_overrun_s)]

    async def aclose(self):
        self.closed = True
        return SimpleNamespace(release_verified=True, model_dump=lambda **_: {"release_verified": True})


def reload_evidence(name="walk_forward", control="w", lease=0.4, scene_path=SCENE_PATH):
    return {"method": "scene_reload", "reloaded": True, "scene_path": scene_path,
            "action": {"name": name, "control": control, "default_lease_s": lease},
            "requested_at_utc": "2026-01-01T00:00:10Z", "completed_at_utc": "2026-01-01T00:00:12Z",
            "game_time_s": 12.0, "real_time_s": 12.0, "frame": 120}


async def run_measure(fake, chosen, *, post_position=(0.0, 0.0, 0.0), post_fields=None,
                      evidence=None, reload=True):
    called = {"reload": 0}

    async def reload_callback():
        called["reload"] += 1
        fake.position = list(post_position)
        if post_fields is not None:
            fake.fields = copy.deepcopy(post_fields)
        entity = calibrate._entity_snapshot(await fake.inspect("transforms"), ENTITY_ID)
        fields = calibrate._field_snapshot(await fake.inspect("fields"), SCENE_ID)
        return {"entity": entity, "fields": fields, "evidence": evidence or reload_evidence(), "stop": None}

    report = await verify.measure(fake, chosen, reload=reload_callback if reload else None)
    return report, called


def no_sleep(monkeypatch):
    async def no_delay(_):
        return None
    monkeypatch.setattr(calibrate.asyncio, "sleep", no_delay)


def test_selection_requires_the_named_action_and_its_matching_measured_hold():
    chosen = selected()
    assert chosen.action.name == "walk_forward"
    assert chosen.control == "w" and chosen.requested_hold_s == 0.4
    assert chosen.scene_id == SCENE_ID and len(chosen.trials) == 2

    with pytest.raises(ValueError, match="not exactly one manifest action"):
        verify.select_action(manifest(), calibration_record(), "other")
    with pytest.raises(ValueError, match="exactly one button"):
        value = action_dict(controls=("w", "a"))
        verify.select_action(manifest(value), calibration_record(value), "walk_forward")
    with pytest.raises(ValueError, match="axis binding"):
        value = action_dict(axis=True)
        verify.select_action(manifest(value), calibration_record(value), "walk_forward")
    with pytest.raises(ValueError, match="analog parameters"):
        value = action_dict(x=0.0)
        verify.select_action(manifest(value), calibration_record(value), "walk_forward")
    with pytest.raises(ValueError, match="entity details differ"):
        verify.select_action(manifest(), calibration_record(entity=dict(ENTITY, id="other")), "walk_forward")


def test_selection_rejects_mismatched_or_unmeasured_calibration():
    value = action_dict(effective=False)
    with pytest.raises(ValueError, match="repeated effective calibration"):
        verify.select_action(manifest(value), calibration_record(value), "walk_forward")

    value = action_dict()
    changed = calibration_record(value)
    data = changed.model_dump(mode="json")
    data["action_summaries"][0]["holds"][0]["sample_variance"] = 0.0
    with pytest.raises(ValueError, match="differs from the calibration record"):
        verify.select_action(manifest(value), CalibrationRecord.model_validate(data), "walk_forward")

    with pytest.raises(ValueError, match="no unique hold"):
        value = action_dict(requested=0.2)
        value["hold_s"] = 0.4
        value["hold_effect"] = None
        verify.select_action(manifest(value), calibration_record(value), "walk_forward")

    with pytest.raises(ValueError, match="not a completed measurement"):
        verify.select_action(manifest(), calibration_record(status="inconclusive"), "walk_forward")


def test_tolerance_is_calibration_spread_plus_predeclared_measurement_tolerance():
    chosen = selected()
    tolerance = verify.effect_tolerance(chosen.hold, chosen.calibration.plan)
    assert tolerance["calibration_spread"] == pytest.approx(math.sqrt(0.0008))
    assert tolerance["measurement_tolerance"] == verify.MEASUREMENT_TOLERANCE
    assert tolerance["effect_tolerance"] == pytest.approx(math.sqrt(0.0008) + verify.MEASUREMENT_TOLERANCE)
    assert tolerance["formula"].startswith("sqrt(calibration sample variance)")


def test_measure_executes_one_action_and_verifies_reload_without_claiming_full_state(monkeypatch):
    no_sleep(monkeypatch)
    chosen = selected()
    fake = FakeBridge()
    report, called = asyncio.run(run_measure(fake, chosen))

    assert report["status"] == "verified", report.get("error") or report["status_reason"]
    assert called["reload"] == 1
    assert len(fake.act_calls) == 1
    call = fake.act_calls[0]
    assert call["action"] == "walk_forward" and call["lease_s"] == 0.4 and call["based_on_seq"] == 1
    assert report["effect"]["comparison"] == "matched"
    assert report["effect"]["magnitude_within_tolerance"] is True
    assert report["effect"]["direction_cosine"] == pytest.approx(1.0)
    assert report["reload"]["entity_position"]["restored"] is True
    assert report["reload"]["fields_outcome"] == "matched"
    assert report["reload"]["full_state_restored"] is None
    assert report["reload"]["host_matches_action"] is True
    assert report["save_load"]["works"] is None and report["checkpoint"]["works"] is None
    attempt = report["execution"]["attempts"][0]
    assert attempt["terminal_status"] == "completed" and attempt["release_verified"] is True
    assert attempt["valid"] is True
    assert report["is_s1_turn"] is False and report["model_calls"] == 0
    for key in ("started_wall_utc", "action_requested_wall_utc", "reload_requested_wall_utc",
                "reload_completed_wall_utc", "finished_wall_utc"):
        assert isinstance(report["timestamps"][key], str)
    assert report["pre_action"]["entity"]["wall_utc"].endswith("+00:00")
    assert report["pre_action"]["fields"]["wall_utc"].endswith("+00:00")


def test_missing_reload_is_inconclusive_not_verified(monkeypatch):
    no_sleep(monkeypatch)
    report, called = asyncio.run(run_measure(FakeBridge(), selected(), reload=False))
    assert called["reload"] == 0
    assert report["reload"]["requested"] is False
    assert report["effect"]["comparison"] == "matched"
    assert report["status"] == "inconclusive"


def test_basis_mismatch_leaves_the_world_vector_comparison_inconclusive(monkeypatch):
    no_sleep(monkeypatch)
    fake = FakeBridge(forward=(1.0, 0.0, 0.0), right=(0.0, 0.0, 1.0))
    report, called = asyncio.run(run_measure(fake, selected()))
    assert called["reload"] == 1
    assert report["effect"]["basis"]["compatible"] is False
    assert report["effect"]["comparison"] == "inconclusive"
    assert report["effect"]["vector_distance"] is None
    assert report["effect"]["magnitude_within_tolerance"] is True
    assert report["status"] == "inconclusive"


def test_wrong_magnitude_is_different_and_is_not_retried(monkeypatch):
    no_sleep(monkeypatch)
    fake = FakeBridge(effect=(0.0, 0.0, 3.0))
    report, _ = asyncio.run(run_measure(fake, selected()))
    assert report["effect"]["comparison"] == "different"
    assert report["effect"]["magnitude_within_tolerance"] is False
    assert report["status"] == "different"
    assert len(fake.act_calls) == 1
    assert len(report["execution"]["attempts"]) == 1


def test_rejected_action_is_recorded_once_and_never_replaced(monkeypatch):
    no_sleep(monkeypatch)
    fake = FakeBridge(reject=True)
    report, called = asyncio.run(run_measure(fake, selected()))
    assert called["reload"] == 0
    assert len(fake.act_calls) == 1
    assert report["status"] == "inconclusive"
    attempt = report["execution"]["attempts"][0]
    assert attempt["terminal_status"] == "rejected"
    assert attempt["valid"] is False
    assert "not retried or replaced" in attempt["reason"]
    assert report["effect"] is None


def test_unverified_release_is_invalid_and_skips_the_reload(monkeypatch):
    no_sleep(monkeypatch)
    fake = FakeBridge(release_verified=False)
    report, called = asyncio.run(run_measure(fake, selected()))
    assert called["reload"] == 0
    assert report["execution"]["attempts"][0]["release_verified"] is False
    assert report["execution"]["attempts"][0]["valid"] is False
    assert report["status"] == "inconclusive"


def test_runtime_errors_are_preserved_and_make_the_effect_inconclusive(monkeypatch):
    no_sleep(monkeypatch)
    report, called = asyncio.run(run_measure(FakeBridge(runtime_errors_after=True), selected()))
    assert called["reload"] == 1
    assert report["execution"]["runtime_errors"]["new_during_action"][0]["message"] == "observed error"
    assert report["effect"]["comparison"] == "inconclusive"
    assert report["execution"]["attempts"][0]["valid"] is False
    assert report["status"] == "inconclusive"


def test_reload_mismatch_is_different_and_unknown_fields_stay_unknown(monkeypatch):
    no_sleep(monkeypatch)
    unknown = [{"id": "field", "scene_id": SCENE_ID, "value_state": "unsupported",
                "value_kind": "unsupported", "value": None, "is_null": None,
                "value_truncated": None, "read_error": None}]
    report, _ = asyncio.run(run_measure(FakeBridge(), selected(), post_position=(0.5, 0.0, 0.0),
                                        post_fields=unknown))
    assert report["reload"]["entity_position"]["restored"] is False
    assert report["reload"]["fields"][0]["matches_pre_action"] is None
    assert report["reload"]["fields_outcome"] is None
    assert report["reload"]["full_state_restored"] is None
    assert report["status"] == "different"

    report, _ = asyncio.run(run_measure(FakeBridge(), selected(), post_fields=unknown))
    assert report["reload"]["entity_position"]["restored"] is True
    assert report["reload"]["fields"][0]["matches_pre_action"] is None
    assert report["reload"]["fields_outcome"] is None
    assert report["status"] == "verified"


def test_unknown_reload_position_and_missing_fields_stay_unknown():
    result = verify.compare_reload(
        {"position": [0.0, 0.0, 0.0], "frame": 1, "real_time_s": 1.0, "game_time_s": 1.0,
         "wall_utc": "2026-01-01T00:00:00Z"},
        {"position": [0.0, 0.0, 1.0], "frame": 2, "real_time_s": 2.0, "game_time_s": 2.0,
         "wall_utc": "2026-01-01T00:00:01Z"},
        {"position": None, "frame": 3, "real_time_s": 3.0, "game_time_s": 3.0,
         "wall_utc": "2026-01-01T00:00:02Z"},
        {"fields": []}, {"fields": [{"id": "new", "value_state": "present", "value_kind": "integer",
                                     "value": 1, "is_null": False, "value_truncated": False,
                                     "read_error": None}]})
    assert result["entity_position"]["after_reload_position"] is None
    assert result["entity_position"]["restored"] is None
    assert result["fields"][0]["matches_pre_action"] is None
    assert result["fields_outcome"] is None
    assert result["full_state_restored"] is None


def test_host_catalog_must_match_the_manifest_action(monkeypatch):
    no_sleep(monkeypatch)
    bad = reload_evidence(control="a")
    report, _ = asyncio.run(run_measure(FakeBridge(), selected(), evidence=bad))
    assert "Editor host action name or physical control" in report["error"]
    assert report["status"] == "inconclusive"

    bad = reload_evidence(lease=0.2)
    report, _ = asyncio.run(run_measure(FakeBridge(), selected(), evidence=bad))
    assert "Editor host lease" in report["error"]
    assert report["status"] == "inconclusive"


def test_cli_refuses_output_inside_the_game_and_existing_handshake_files(tmp_path):
    project = tmp_path / "game"
    project.mkdir()
    base = ["--project", str(project), "--manifest", str(tmp_path / "manifest.json"),
            "--calibration", str(tmp_path / "calibration.json"), "--action", "walk_forward"]
    with pytest.raises(SystemExit) as inside:
        verify.main(base + ["--output", str(project / "report.json"),
                            "--reload-request", str(tmp_path / "request.json"),
                            "--reload-done", str(tmp_path / "done.json")])
    assert inside.value.code == 2

    output = tmp_path / "report.json"
    output.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as existing:
        verify.main(base + ["--output", str(output),
                            "--reload-request", str(tmp_path / "request.json"),
                            "--reload-done", str(tmp_path / "done.json")])
    assert existing.value.code == 2


def test_measure_is_testable_without_a_live_editor(monkeypatch):
    no_sleep(monkeypatch)
    report, _ = asyncio.run(run_measure(FakeBridge(), selected()))
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize(("row", "expected"), [
    (None, None),
    ({"trials_compared": 3, "matches_initial_after_reload": 3}, True),
    ({"trials_compared": 2, "matches_initial_after_reload": 2}, True),
    ({"trials_compared": 3, "matches_initial_after_reload": 2}, False),
    ({"trials_compared": 1, "matches_initial_after_reload": 0}, False),
    ({"trials_compared": 1, "matches_initial_after_reload": 1}, None),
    ({"trials_compared": 0, "matches_initial_after_reload": 0}, None),
    ({"trials_compared": None, "matches_initial_after_reload": None}, None),
    ({"trials_compared": 3, "matches_initial_after_reload": True}, None),
])
def test_reload_stability_needs_every_repeated_calibration_reload_to_restore(row, expected):
    assert verify.reload_stable(row) is expected


def test_only_reload_stable_fields_decide_the_reload_outcome():
    """A frame counter that no reload restores is reported, not counted; a stable field decides."""
    def sample(value, field_id):
        return {"id": field_id, "value_state": "present", "value_kind": "integer", "value": value,
                "is_null": False, "value_truncated": False, "read_error": None}
    start = {"position": [0.0, 0.0, 0.0], "frame": 1, "real_time_s": 1.0, "game_time_s": 1.0,
             "wall_utc": "2026-01-01T00:00:00Z"}
    moved = dict(start, position=[0.0, 0.0, 1.0])
    calibrated = {"stable": {"id": "stable", "trials_compared": 3, "matches_initial_after_reload": 3},
                  "counter": {"id": "counter", "trials_compared": 3, "matches_initial_after_reload": 0}}
    before = {"fields": [sample(5, "stable"), sample(100, "counter"), sample(1, "unmeasured")]}
    after = {"fields": [sample(5, "stable"), sample(260, "counter"), sample(2, "unmeasured")]}
    result = verify.compare_reload(start, moved, start, before, after, calibrated)
    assert result["fields_outcome"] == "matched" and result["fields_compared"] == 1
    assert result["uncounted_comparable"] == {"not_reload_stable_in_calibration": 1,
                                              "no_repeated_calibration_evidence": 1, "differed": 2}
    rows = {row["id"]: row for row in result["fields"]}
    assert rows["counter"]["matches_pre_action"] is False and rows["counter"]["counted"] is False
    assert rows["counter"]["calibrated_reload_stable"] is False
    assert rows["unmeasured"]["calibrated_reload_stable"] is None and rows["unmeasured"]["calibration"] is None
    after["fields"][0]["value"] = 6
    assert verify.compare_reload(start, moved, start, before, after, calibrated)["fields_outcome"] == "different"
    assert verify.compare_reload(start, moved, start, before, after)["fields_outcome"] is None


@pytest.mark.parametrize(("overrun", "comparison", "status"), [
    (0.0, "matched", "verified"),
    (0.009, "matched", "verified"),
    (0.02, "inconclusive", "inconclusive"),
    (-0.02, "inconclusive", "inconclusive"),
])
def test_an_unreproduced_hold_cannot_confirm_or_refute_the_calibrated_effect(monkeypatch, overrun, comparison,
                                                                            status):
    """A bridge hold outside the calibration trials' applied holds is a different input."""
    no_sleep(monkeypatch)
    report, _ = asyncio.run(run_measure(FakeBridge(hold_overrun_s=overrun), selected()))
    assert report["effect"]["comparison"] == comparison
    assert report["effect"]["input_reproduction"]["reproduced"] is (comparison == "matched")
    assert report["effect"]["magnitude_difference"] is not None
    assert report["status"] == status
    assert len(report["execution"]["attempts"]) == 1


def test_a_far_effect_with_an_unreproduced_hold_is_inconclusive_not_different(monkeypatch):
    no_sleep(monkeypatch)
    report, _ = asyncio.run(run_measure(FakeBridge(effect=(0.0, 0.0, 1.5), hold_overrun_s=0.05), selected()))
    assert report["effect"]["magnitude_within_tolerance"] is False
    assert report["effect"]["comparison"] == "inconclusive"
    assert "not reproduced" in report["effect"]["reason"]


def test_an_unreported_applied_hold_leaves_the_effect_unknown():
    chosen = selected()
    trial = {"execution": [{"status": "completed"}]}
    assert verify.input_reproduction(chosen, trial)["reproduced"] is None
