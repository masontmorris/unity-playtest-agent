"""Adapt real-shaped reflection and calibration records without inventing controls."""

import copy
import json

import pytest

from playtest.contracts.bridge import InspectResult
from playtest.contracts.calibration import CalibrationRecord
from playtest.preflight.calibrate import discover_actions
from playtest.preflight.packet import Screenshot, build
from playtest.roles.checks import value_kind
from playtest.roles.preflight import StageAnswer
from playtest.roles import preflight


def cap(n):
    return {"limit": 100, "observed": n, "returned": n, "truncated": False}


def evidence():
    binding = {"id": "binding", "action_id": "action", "binding_effective_path": "<Keyboard>/w",
               "binding_name": "up", "source_object_id": "entity", "binding_is_composite": False}
    entity = {"id": "entity", "scene_id": "scene", "scene_path": "Assets/scene", "path": "root"}
    data = {"game_objects": [entity], "components": [],
            "fields": [{"id": "field", "field_type": "System.Single", "scene_id": "scene",
                        "value_state": "present", "value_kind": "number",
                        "value": "private-runtime-value", "enum_name": "private-name"}],
            "bindings": [binding], "input_asset_bindings": [],
            "source_excerpts": [{"path": "Assets/Test.cs", "text": "example"}]}
    caps = {key: cap(len(data[key])) for key in
            ("game_objects", "components", "fields", "bindings", "input_asset_bindings")}
    caps.update({"source_files": cap(1), "source_characters": cap(7)})
    snapshot = {"protocol": "upa/1", "session": "s", "seq": 1, "game_s": 1.0,
                "wall_utc": "2026-01-01T00:00:00Z", "type": "inspection", "stage": "discovery",
                "in_reply_to": 0, "data": data, "caps": caps, "errors": []}
    actions, unsupported = discover_actions(data)
    summary = {"control": "w", "bindings": actions[0]["bindings"], "resolution": "unity_key_name", "holds": [],
               "lowest_tested_effective_requested_hold_s": None, "shortest_observed_effective_hold_s": None,
               "minimum_hold_scope": "unknown", "inverted_relative_to_entity_basis": None,
               "signed_basis_projections": [], "dead_zone": None, "dead_zone_reason": "not measured"}
    record = {"schema_version": 1, "status": "measured_with_gaps", "error": None,
              "entity_id": "entity", "entity": entity, "coordinate_frame": "world", "units": "units",
              "physical_unit": None, "plan": {}, "action_cap": cap(1), "actions": actions,
              "unsupported_bindings": unsupported, "discovery_coverage": {}, "idle_fields": [],
              "input_field_segments": [], "trials": [{"control": "w", "valid": True}],
              "action_summaries": [summary], "freshness": {"field": {"classification": "unknown"}},
              "clock": {}, "reset": {}, "runtime_errors": None,
              "stop": {"protocol": "upa/1", "session": "s", "seq": 2, "game_s": 1.0,
                       "wall_utc": "2026-01-01T00:00:00Z", "type": "stopped", "released": [],
                       "release_verified": True}}
    return snapshot, record


def make(snapshot, record):
    inventory = copy.deepcopy(snapshot)
    inventory.update(stage="inventory", data={}, caps={}, errors=[])
    return build(InspectResult.model_validate(inventory), InspectResult.model_validate(snapshot),
                 CalibrationRecord.model_validate(record))


def test_real_field_names_and_physical_control_are_offered_deterministically():
    snapshot, record = evidence()
    packet = make(snapshot, record)
    assert packet.text == make(snapshot, record).text
    assert packet.discovery.field_types == {"field_00000": "System.Single"}
    assert packet.field_bindings == {"field_00000": "field"}
    assert packet.discovery.control_types == {"w": "Button"}
    data = json.loads(packet.text)
    assert data["calibration"]["freshness"]["id_matches_unverified"][0]["classification"] == "unknown"
    assert data["calibration"]["physical_unit"] is None
    assert data["image"] is None and data["image_unavailable_reason"]
    assert "private-runtime-value" not in packet.text and "private-name" not in packet.text
    assert data["offered_fields"][0]["same_scene_as_entity"] is True


def test_supplied_image_is_identified_by_hash_and_capture_provenance():
    snapshot, record = evidence()
    inventory = copy.deepcopy(snapshot)
    inventory.update(stage="inventory", data={}, caps={})
    image = Screenshot(b"\x89PNG\r\n\x1a\nimage bytes", "image/png", "capture from the running game")
    packet = build(InspectResult.model_validate(inventory), InspectResult.model_validate(snapshot),
                   CalibrationRecord.model_validate(record), image=image)
    descriptor = json.loads(packet.text)["image"]
    assert descriptor == image.descriptor()
    assert descriptor["bytes"] == len(image.data) and descriptor["provenance"] == image.provenance


def test_long_editor_ids_are_bijectively_bound_to_short_schema_ids():
    snapshot, record = evidence()
    fields = [{"id": f"scene/{'long-object-path-' * 10}{i}", "field_type": "System.Single",
               "value_state": "present", "value_kind": "number"}
              for i in range(312)]
    snapshot["data"]["fields"] = fields
    snapshot["caps"]["fields"] = {"limit": 500, "observed": len(fields), "returned": len(fields), "truncated": False}
    packet = make(snapshot, record)
    preflight.schema(packet.discovery)
    assert len(packet.field_bindings) == len(fields)
    assert set(packet.field_bindings.values()) == {item["id"] for item in fields}
    assert all(len(token) < 20 for token in packet.field_bindings)


def test_enum_kind_comes_from_editor_descriptor_not_type_name():
    snapshot, record = evidence()
    snapshot["data"]["fields"][0].update(field_type="Example.Mode", field_type_kind="enum", value_kind="enum")
    packet = make(snapshot, record)
    assert packet.discovery.field_types["field_00000"] == "Example.Mode"
    assert packet.discovery.field_kinds["field_00000"] == "number"
    answer = StageAnswer.model_validate({"stage_4": None, "stage_5": None, "stage_6": {
        "progress": [{"field": "mode", "direction": "up", "why": "tracks state"}],
        "invariants": [], "unjudgeable": []}})
    assert preflight.review(answer, "progress", packet.discovery, readers={"mode": "field_00000"}) == []


def test_unreadable_field_is_reported_but_not_offered_and_null_is_unknown():
    snapshot, record = evidence()
    snapshot["data"]["fields"].extend([
        {"id": "unreadable", "field_type": "UnityEngine.Vector3", "value_state": "unsupported", "value_kind": "unsupported"},
        {"id": "unknown", "field_type": "System.Single", "value_state": "null", "value_kind": "null"},
    ])
    snapshot["caps"]["fields"] = cap(3)
    packet = make(snapshot, record)
    assert "unreadable" not in packet.field_bindings.values()
    token = next(k for k, v in packet.field_bindings.items() if v == "unknown")
    assert packet.discovery.field_kinds[token] is None
    unknown_check = StageAnswer.model_validate({"stage_4": None, "stage_5": None, "stage_6": {
        "progress": [{"field": "unknown", "direction": "up", "why": "test"}],
        "invariants": [], "unjudgeable": []}})
    assert any("kind is unknown" in problem for problem in
               preflight.review(unknown_check, "progress", packet.discovery, readers={"unknown": token}))
    assert any(item["source_field_id"] == "unreadable" for item in json.loads(packet.text)["unoffered_fields"])
    rejected = StageAnswer.model_validate({"stage_4": {"readers": [{"field_id": "field_00002",
        "alias": "invalid", "why": "test"}], "unobservable": []}, "stage_5": None, "stage_6": None})
    assert any("not a discovered field" in problem for problem in preflight.review(rejected, "readers", packet.discovery))


@pytest.mark.parametrize(("name", "kind"), [
    ("System.Single", "number"), ("System.UInt32", "number"), ("System.Boolean", "bool"),
    ("System.String", "string"), ("UnityEngine.Vector3", "vector"), ("Game.Single", "other"),
])
def test_reflection_type_names_are_not_guessed_by_suffix(name, kind):
    assert value_kind(name) == kind


def test_truncated_fields_and_different_bindings_fail_closed():
    snapshot, record = evidence()
    snapshot["caps"]["fields"] = {"limit": 1, "observed": 2, "returned": 1, "truncated": True}
    with pytest.raises(ValueError, match="incomplete fields"):
        make(snapshot, record)
    snapshot, record = evidence()
    snapshot["data"]["bindings"][0]["binding_effective_path"] = "<Keyboard>/a"
    with pytest.raises(ValueError, match="differ from calibration"):
        make(snapshot, record)


def test_missing_entity_and_no_valid_trials_do_not_become_measurements():
    snapshot, record = evidence()
    altered = copy.deepcopy(snapshot)
    altered["data"]["game_objects"][0]["path"] = "different"
    with pytest.raises(ValueError, match="entity no longer resolves"):
        make(altered, record)
    altered = copy.deepcopy(record)
    altered["entity"]["id"] = "other"
    with pytest.raises(ValueError, match="entity no longer resolves"):
        make(snapshot, altered)
    record["trials"][0]["valid"] = False
    # The calibration contract itself rejects a measured record without any valid trials.
    with pytest.raises(ValueError, match="needs action, field, clock and reset evidence"):
        make(snapshot, record)


def test_missing_source_coverage_is_not_silent():
    snapshot, record = evidence()
    del snapshot["caps"]["source_files"]
    with pytest.raises(ValueError, match="source_files coverage"):
        make(snapshot, record)


def test_bridge_review_preserves_unsupported_model_choices():
    snapshot, record = evidence()
    packet = make(snapshot, record)
    action = {"name": "press", "intent": "act", "hold_s": None,
              "inputs": [{"control_id": "w", "x": None, "y": None}], "preconditions": []}
    skill = {"name": "sequence", "purpose": "two steps", "max_s": 2,
             "steps": [{"inputs": action["inputs"], "hold_s": 1, "wait_s": 0}]}
    answer = StageAnswer.model_validate({"stage_4": None, "stage_5": {"actions": [action], "skills": [skill],
                                       "uncontrollable": []}, "stage_6": None})
    problems = packet.review_bridge(answer, "actions")
    assert problems == []
    assert answer.stage_5.actions[0].hold_s is None
    assert answer.stage_5.skills[0].name == "sequence"

    two = copy.deepcopy(action)
    two["hold_s"] = 3601
    two["inputs"].append(copy.deepcopy(two["inputs"][0]))
    compound = StageAnswer.model_validate({"stage_4": None, "stage_5": {"actions": [two], "skills": [],
                                         "uncontrollable": []}, "stage_6": None})
    problems = packet.review_bridge(compound, "actions")
    assert problems == []


def test_pointer_and_gamepad_controls_are_offered_with_real_types_and_declared_values():
    snapshot, record = evidence()
    snapshot["data"]["bindings"].append(
        {"id": "mouse", "action_id": "look", "binding_effective_path": "<Mouse>/delta",
         "binding_name": "", "source_object_id": "entity", "binding_is_composite": False})
    snapshot["data"]["bindings"].append(
        {"id": "stick", "action_id": "move", "binding_effective_path": "<Gamepad>/leftStick",
         "binding_name": "", "source_object_id": "entity", "binding_is_composite": False})
    snapshot["caps"]["bindings"] = cap(3)
    actions, unsupported = discover_actions(snapshot["data"])
    record["actions"] = actions
    record["action_cap"] = cap(3)
    record["action_summaries"] = [{
        "control": action["control"], "bindings": action["bindings"], "resolution": action["resolution"],
        "device": action["device"], "control_name": action["control_name"],
        "control_type": action["control_type"], "declared_x": action["x"], "declared_y": action["y"],
        "holds": [], "lowest_tested_effective_requested_hold_s": None,
        "shortest_observed_effective_hold_s": None, "minimum_hold_scope": "unknown",
        "inverted_relative_to_entity_basis": None, "signed_basis_projections": [],
        "dead_zone": None, "dead_zone_reason": "not measured"} for action in actions]
    record["trials"] = [{"control": action["control"], "valid": True} for action in actions]
    packet = make(snapshot, record)
    data = json.loads(packet.text)
    by_id = {row["id"]: row for row in data["offered_controls"]}
    assert by_id["mouse_delta"]["control_type"] == "Delta"
    assert by_id["mouse_delta"]["declared_x"] == 1.0 and by_id["mouse_delta"]["declared_y"] == 0.0
    assert by_id["gamepad_leftstick"]["control_type"] == "Stick"
    assert by_id["gamepad_leftstick"]["device"] == "Gamepad"
    assert packet.discovery.control_types["mouse_delta"] == "Delta"
    assert packet.discovery.control_types["gamepad_leftstick"] == "Stick"
    assert "64" in data["capability_note"] and "relative impulse" in data["capability_note"]
    assert "absolute pointer" in data["capability_note"]
