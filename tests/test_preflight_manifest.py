"""Manifest assembly keeps model choices and validates their evidence."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from playtest.contracts.manifest import PreflightManifest
from playtest.preflight import manifest, stages
from test_preflight_packet import evidence, make


def assembled():
    snapshot, calibration = evidence()
    packet = make(snapshot, calibration)
    answers = [
        {"stage_4": {"readers": [{"field_id": "field_00000", "alias": "speed", "why": "observe"}],
                     "unobservable": ["missing state"]}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "move", "intent": "move",
         "hold_s": 0.2, "inputs": [{"control_id": "w", "x": None, "y": None}],
         "preconditions": []}], "skills": [], "uncontrollable": ["missing input"]}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [],
         "unjudgeable": ["missing objective"]}},
    ]
    replies = iter(answers)
    result = stages.run(packet,
                        lambda *_: stages.CallResult(json.dumps(next(replies)), None, {"usage": None}),
                        lambda _: None)
    return packet, result, manifest.assemble(packet, result)


def test_manifest_preserves_selections_gaps_and_unknown_evidence():
    _, _, result = assembled()
    assert [r.alias for r in result.readers] == ["speed"]
    assert result.readers[0].kind == "number" and result.readers[0].freshness is None
    assert result.readers[0].selection_id == "field_00000" and result.readers[0].field_id == "field"
    assert result.readers[0].freshness_reason
    assert [a.name for a in result.actions] == ["move"]
    assert result.actions[0].inputs[0].control_id == "w"
    assert result.actions[0].hold_s == 0.2 and result.actions[0].max_hold_s is None
    assert result.actions[0].hold_effect is None
    assert any("no established effective response" in gap for gap in result.gaps.coverage)
    assert result.gaps.unobservable == ["missing state"]
    assert result.gaps.uncontrollable == ["missing input"]
    assert result.gaps.unjudgeable == ["missing objective"]
    assert result.gaps.scope == "model_report_relative_to_offered_and_selected_evidence"
    assert len(result.selection.offered_fields) == 1
    assert result.selection.unselected_field_ids == []
    assert not result.selection.reader_cap_reached and not result.selection.action_cap_reached
    assert manifest.validate(result) == []
    assert any("not installed or verified" in gap for gap in result.gaps.coverage)


def test_partial_chain_and_wrong_descriptor_are_not_manifest_success():
    packet, result, assembled_manifest = assembled()
    partial = stages.RunResult(result.readers, stages.StageResult(None, ()), result.progress)
    with pytest.raises(ValueError, match="all three stages"):
        manifest.assemble(packet, partial)
    invalid = assembled_manifest.model_dump(mode="json")
    invalid["readers"][0]["descriptor"]["id"] = "other"
    parsed = PreflightManifest.model_validate(invalid)
    assert "descriptor does not match" in manifest.validate(parsed)[0]
    assert any("differs from the packet" in problem for problem in manifest.validate_evidence(parsed, packet))


def test_unknown_reference_fails_the_manifest_contract():
    _, _, value = assembled()
    invalid = copy.deepcopy(value.model_dump(mode="json"))
    invalid["progress"] = [{"field": "missing", "direction": "up", "why": "test"}]
    with pytest.raises(ValidationError, match="reference selected reader aliases"):
        PreflightManifest.model_validate(invalid)


def test_invariant_semantic_errors_are_detected_after_schema_validation():
    _, _, value = assembled()
    data = value.model_dump(mode="json")
    data["invariants"] = [{"kind": "range", "name": "bad_range", "source": "source", "catches": "bug",
                           "field": "speed", "direction": None, "min": 5, "max": 2,
                           "when": None, "then": None, "unless": None}]
    parsed = PreflightManifest.model_validate(data)
    assert any("above max" in item for item in manifest.validate(parsed))


def test_evidence_validation_rejects_modified_caps_and_coverage():
    packet, _, value = assembled()
    data = value.model_dump(mode="json")
    data["discovery_caps"]["fields"]["limit"] += 1
    data["gaps"]["coverage"] = []
    problems = manifest.validate_evidence(PreflightManifest.model_validate(data), packet)
    assert "inspection coverage differs from the packet" in problems
    assert "coverage gaps differ from the packet and selections" in problems


def test_freshness_cannot_be_fabricated_in_the_contract():
    _, _, value = assembled()
    data = value.model_dump(mode="json")
    data["readers"][0]["freshness"] = {"classification": "observed"}
    with pytest.raises(ValidationError):
        PreflightManifest.model_validate(data)


def test_selection_cap_flag_cannot_be_fabricated():
    _, _, value = assembled()
    data = value.model_dump(mode="json")
    data["selection"]["reader_cap_reached"] = True
    with pytest.raises(ValidationError, match="selection cap facts"):
        PreflightManifest.model_validate(data)


def chord_manifest():
    from playtest.preflight.calibrate import discover_actions
    snapshot, calibration = evidence()
    new_binding = dict(snapshot["data"]["bindings"][0], id="other", binding_effective_path="<Keyboard>/a")
    snapshot["data"]["bindings"].append(new_binding)
    snapshot["caps"]["bindings"] = {"limit": 100, "observed": 2, "returned": 2, "truncated": False}
    actions, _ = discover_actions(snapshot["data"])
    original = calibration["action_summaries"][0]
    calibration["actions"] = actions
    calibration["action_cap"] = {"limit": 100, "observed": 2, "returned": 2, "truncated": False}
    calibration["action_summaries"] = [dict(original, control=a["control"], bindings=a["bindings"],
                                             resolution=a["resolution"]) for a in actions]
    calibration["trials"] = [{"control": a["control"], "valid": True} for a in actions]
    packet = make(snapshot, calibration)
    inputs = [{"control_id": key, "x": None, "y": None} for key in ("w", "a")]
    answers = [
        {"stage_4": {"readers": [{"field_id": "field_00000", "alias": "speed", "why": "read"}],
                     "unobservable": []}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "chord", "intent": "act",
         "inputs": inputs, "hold_s": 0.2, "preconditions": []}], "skills": [{"name": "sequence",
         "purpose": "bounded sequence", "steps": [{"inputs": inputs, "hold_s": 0.2, "wait_s": 0}],
         "max_s": 1}], "uncontrollable": []}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [], "unjudgeable": []}},
    ]
    replies = iter(answers)
    result = stages.run(packet, lambda *_: stages.CallResult(json.dumps(next(replies)), None, {}), lambda _: None)
    value = manifest.assemble(packet, result)
    return packet, value


def test_chords_and_skills_survive_the_manifest_without_claiming_execution():
    packet, value = chord_manifest()
    assert [i.control_id for i in value.actions[0].inputs] == ["w", "a"]
    assert [i.control_id for i in value.skills[0].steps[0].inputs] == ["w", "a"]
    assert not value.actions[0].execution_supported and not value.skills[0].execution_supported
    assert value.actions[0].hold_effect is None
    assert manifest.validate_evidence(value, packet) == []


def test_tap_uses_tested_requested_hold_not_observed_duration():
    snapshot, calibration = evidence()
    summary = calibration["action_summaries"][0]
    summary["lowest_tested_effective_requested_hold_s"] = 0.1
    summary["shortest_observed_effective_hold_s"] = 0.13
    summary["holds"] = [{"requested_hold_s": 0.1, "valid_trials": 2, "mean_delta": [0, 0, 1],
                         "mean_magnitude": 1, "sample_variance": 0, "effective_in_all_repeats": True}]
    packet = make(snapshot, calibration)
    answers = [
        {"stage_4": {"readers": [{"field_id": "field_00000", "alias": "speed", "why": "read"}],
                     "unobservable": []}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "tap", "intent": "act", "hold_s": None,
         "inputs": [{"control_id": "w", "x": None, "y": None}], "preconditions": []}],
         "skills": [], "uncontrollable": []}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [], "unjudgeable": []}},
    ]
    replies = iter(answers)
    result = stages.run(packet, lambda *_: stages.CallResult(json.dumps(next(replies)), None, {}), lambda _: None)
    action = manifest.assemble(packet, result).actions[0]
    assert action.tap_hold_s == 0.1 and action.hold_effect is True


def empty_assembled():
    import jsonschema
    from playtest.roles import preflight
    snapshot, calibration = evidence()
    snapshot["data"]["fields"] = []
    snapshot["data"]["bindings"] = []
    for key in ("fields", "bindings"):
        snapshot["caps"][key] = {"limit": 100, "observed": 0, "returned": 0, "truncated": False}
    calibration.update(status="inconclusive", error="no controls discovered", actions=[], action_summaries=[],
                       trials=[], freshness=None, clock=None, reset=None,
                       action_cap={"limit": 100, "observed": 0, "returned": 0, "truncated": False})
    packet = make(snapshot, calibration)
    schema = preflight.schema(packet.discovery)
    assert schema["$defs"]["ReadersPart"]["properties"]["readers"]["maxItems"] == 0
    assert schema["$defs"]["ActionsPart"]["properties"]["actions"]["maxItems"] == 0
    answers = [
        {"stage_4": {"readers": [], "unobservable": ["no readable fields"]}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [], "skills": [], "uncontrollable": ["no offered controls"]}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [], "unjudgeable": ["no readers"]}},
    ]
    for answer in answers:
        jsonschema.Draft202012Validator(schema).validate(answer)
    invalid = copy.deepcopy(answers[0])
    invalid["stage_4"]["readers"] = [{"field_id": "__no_field_available__", "alias": "x", "why": "test"}]
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(invalid))
    replies = iter(answers)
    result = stages.run(packet, lambda *_: stages.CallResult(json.dumps(next(replies)), None, {}), lambda _: None)
    return packet, manifest.assemble(packet, result)


def test_no_readers_or_controls_are_explicit_gaps_not_fabricated_choices():
    packet, value = empty_assembled()
    assert value.readers == [] and value.actions == [] and value.skills == []
    assert "no discovered field was offered as a reader" in value.gaps.coverage
    assert "no measured control was offered" in value.gaps.coverage
    assert manifest.validate_evidence(value, packet) == []


def execution_evidence(control="w", hold=0.2, release=True, duration=None, device="Keyboard", x=None, y=None,
                       start=1000.0, action="move", declaration_hold=None):
    value = hold if duration is None else duration
    declaration = {"backend": "input_system",
                   "inputs": [{"device": device, "control": control, "x": x, "y": y}],
                   "default_lease_s": hold if declaration_hold is None else declaration_hold}
    return {
        "action": action, "session": "s-1", "status": "completed", "release_verified": release,
        "declaration": declaration,
        "applied_duration_s": value,
        "input_start_wall_s": start, "input_end_wall_s": start + value,
        "receipts": [{
            "step_index": 0, "device": device, "control": control, "x": x, "y": y,
            "input_start_wall_s": start, "input_end_wall_s": start + value,
            "applied_duration_s": value, "applied_delta_x": 64.0 if device == "Mouse" else None,
            "applied_delta_y": 0.0 if device == "Mouse" else None,
            "release_verified": release,
        }],
        "wall_utc": "2026-01-01T00:00:00Z",
    }


def test_execution_support_requires_evidence_for_the_exact_declared_hold():
    snapshot, calibration = evidence()
    calibration["execution_evidence"] = [execution_evidence(hold=0.2, duration=0.201)]
    packet = make(snapshot, calibration)
    answers = [
        {"stage_4": {"readers": [{"field_id": "field_00000", "alias": "speed", "why": "read"}],
                     "unobservable": []}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "move", "intent": "move", "hold_s": 0.2,
         "inputs": [{"control_id": "w", "x": None, "y": None}], "preconditions": []}],
         "skills": [], "uncontrollable": []}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [], "unjudgeable": []}},
    ]
    replies = iter(answers)
    result = stages.run(packet, lambda *_: stages.CallResult(json.dumps(next(replies)), None, {}), lambda _: None)
    value = manifest.assemble(packet, result)
    assert value.actions[0].execution_supported is True
    assert value.actions[0].execution_evidence.receipts[0].control == "w"

    changed = calibration["execution_evidence"][0]["receipts"][0]
    changed["applied_duration_s"] = 0.6
    packet = make(snapshot, calibration)
    replies = iter(answers)
    result = stages.run(packet, lambda *_: stages.CallResult(json.dumps(next(replies)), None, {}), lambda _: None)
    value = manifest.assemble(packet, result)
    assert value.actions[0].execution_supported is False and value.actions[0].execution_evidence is None


def assembled_action_with_evidence(calibration_evidence):
    snapshot, calibration = evidence()
    calibration["execution_evidence"] = calibration_evidence
    packet = make(snapshot, calibration)
    answers = [
        {"stage_4": {"readers": [{"field_id": "field_00000", "alias": "speed", "why": "read"}],
                     "unobservable": []}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "move", "intent": "move", "hold_s": 0.2,
         "inputs": [{"control_id": "w", "x": None, "y": None}], "preconditions": []}],
         "skills": [], "uncontrollable": []}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [], "unjudgeable": []}},
    ]
    replies = iter(answers)
    result = stages.run(packet, lambda *_: stages.CallResult(json.dumps(next(replies)), None, {}), lambda _: None)
    return manifest.assemble(packet, result)


def test_evidence_declaration_must_match_exactly_before_measurement_tolerance():
    # The receipt duration is a valid measured application of a 0.2 s declaration, but the
    # recorded declaration says 0.21 s. A measured hold tolerance must not rewrite it.
    value = assembled_action_with_evidence([execution_evidence(
        hold=0.2, duration=0.201, declaration_hold=0.21)])
    assert value.actions[0].execution_supported is False
    assert value.actions[0].execution_evidence is None


def test_evidence_label_need_not_repeat_the_manifest_action_name():
    # The physical declaration matches even though the calibration harness label differs.
    value = assembled_action_with_evidence([execution_evidence(
        hold=0.2, duration=0.201, action="preflight_key_w")])
    assert value.actions[0].execution_supported is True
    assert value.actions[0].execution_evidence.action == "preflight_key_w"


def test_a_chord_is_not_supported_by_its_individual_key_evidence():
    snapshot, calibration = evidence()
    calibration["execution_evidence"] = [execution_evidence(control="w")]
    packet = make(snapshot, calibration)
    answers = [
        {"stage_4": {"readers": [{"field_id": "field_00000", "alias": "speed", "why": "read"}],
                     "unobservable": []}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "chord", "intent": "act", "hold_s": 0.2,
         "inputs": [{"control_id": "w", "x": None, "y": None}, {"control_id": "a", "x": None, "y": None}],
         "preconditions": []}], "skills": [], "uncontrollable": []}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [], "unjudgeable": []}},
    ]
    # Add the second offered control and a matching summary so the declaration passes reference checks.
    snapshot["data"]["bindings"].append(dict(snapshot["data"]["bindings"][0], id="other",
                                             binding_effective_path="<Keyboard>/a"))
    snapshot["caps"]["bindings"] = {"limit": 100, "observed": 2, "returned": 2, "truncated": False}
    from playtest.preflight.calibrate import discover_actions
    actions, _ = discover_actions(snapshot["data"])
    calibration["actions"] = actions
    calibration["action_cap"] = {"limit": 100, "observed": 2, "returned": 2, "truncated": False}
    original = calibration["action_summaries"][0]
    calibration["action_summaries"] = [dict(original, control=a["control"], bindings=a["bindings"],
                                             resolution=a["resolution"], device=a["device"],
                                             control_name=a["control_name"], control_type=a["control_type"],
                                             declared_x=a["x"], declared_y=a["y"]) for a in actions]
    calibration["trials"] = [{"control": a["control"], "valid": True} for a in actions]
    packet = make(snapshot, calibration)
    replies = iter(answers)
    result = stages.run(packet, lambda *_: stages.CallResult(json.dumps(next(replies)), None, {}), lambda _: None)
    value = manifest.assemble(packet, result)
    assert [i.control_id for i in value.actions[0].inputs] == ["w", "a"]
    assert value.actions[0].execution_supported is False
    assert value.actions[0].execution_evidence is None


def test_skill_support_requires_ordered_release_verified_step_receipts():
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "preflight_manifest" / "valid-chord-skill.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    skill = data["skills"][0]
    skill["steps"] = [
        {"inputs": [{"control_id": "w", "device": "Keyboard", "control": "w", "x": None, "y": None}],
         "hold_s": 0.2, "tap_hold_s": None, "wait_s": 0.1},
        {"inputs": [{"control_id": "a", "device": "Keyboard", "control": "a", "x": None, "y": None}],
         "hold_s": 0.2, "tap_hold_s": None, "wait_s": 0.0},
    ]
    skill["max_s"] = 1.0
    skill["calibrations"] = [data["actions"][0]["calibrations"][0], data["actions"][0]["calibrations"][1]]
    skill["execution_evidence"] = {
        "action": "sequence", "session": "s-1", "status": "completed", "release_verified": True,
        "declaration": {"backend": "input_system", "steps": [
            {"inputs": [{"device": "Keyboard", "control": "w", "x": None, "y": None}],
             "hold_s": 0.2, "wait_s": 0.1},
            {"inputs": [{"device": "Keyboard", "control": "a", "x": None, "y": None}],
             "hold_s": 0.2, "wait_s": 0.0}], "max_s": 1.0},
        "applied_duration_s": 0.5, "input_start_wall_s": 1000.0, "input_end_wall_s": 1000.5,
        "receipts": [
            {"step_index": 0, "device": "Keyboard", "control": "w", "x": None, "y": None,
             "input_start_wall_s": 1000.0, "input_end_wall_s": 1000.2, "applied_duration_s": 0.2,
             "applied_delta_x": None, "applied_delta_y": None, "release_verified": True},
            {"step_index": 1, "device": "Keyboard", "control": "a", "x": None, "y": None,
             "input_start_wall_s": 1000.3, "input_end_wall_s": 1000.5, "applied_duration_s": 0.2,
             "applied_delta_x": None, "applied_delta_y": None, "release_verified": True},
        ],
        "wall_utc": "2026-01-01T00:00:00Z"}
    skill["execution_supported"] = True
    value = PreflightManifest.model_validate(data)
    assert value.skills[0].execution_supported is True

    changed_wait = copy.deepcopy(data)
    changed_wait["skills"][0]["execution_evidence"]["declaration"]["steps"][0]["wait_s"] = 0.05
    with pytest.raises(ValidationError, match="wait is 0.05"):
        PreflightManifest.model_validate(changed_wait)
    changed_max = copy.deepcopy(data)
    changed_max["skills"][0]["execution_evidence"]["declaration"]["max_s"] = 1.5
    with pytest.raises(ValidationError, match="max_s is 1.5"):
        PreflightManifest.model_validate(changed_max)

    data["skills"][0]["execution_evidence"]["receipts"] = list(reversed(
        data["skills"][0]["execution_evidence"]["receipts"]))
    with pytest.raises(ValidationError, match="step_index"):
        PreflightManifest.model_validate(data)
