"""Authored fingerprints and cache replay fail closed on changed or ambiguous evidence."""

import copy
import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from playtest.contracts.bridge import InspectResult
from playtest.contracts.calibration import CalibrationRecord
from playtest.contracts.manifest import ReachRegion
from playtest.preflight import cache, cache_cli, calibrate, manifest, manifest_cli, packet, provider, stages, validate_cli
from playtest.roles import strict
from test_preflight_packet import cap, evidence


def project(path):
    for name in ("Assets", "Packages", "ProjectSettings"):
        (path / name).mkdir(parents=True, exist_ok=True)
    (path / "Assets" / "scene.unity").write_text("scene", encoding="utf-8")
    (path / "Assets" / "script.cs").write_text("script", encoding="utf-8")
    (path / "Assets" / "prefab.prefab").write_text("prefab", encoding="utf-8")
    (path / "ProjectSettings" / "InputManager.asset").write_text("input", encoding="utf-8")
    (path / "Packages" / "manifest.json").write_text('{"dependencies":{}}', encoding="utf-8")
    return path


def records():
    discovery, calibration = evidence()
    data = discovery["data"]
    data["scenes"] = [{"id": "scene", "path": "Assets/scene.unity"}]
    data["input_asset_actions"] = []
    data["il_complete"] = True
    data["il_scope"] = "direct_stores_in_loaded_assemblies_containing_authored_scripts"
    data["il_issues"] = []
    data["il_write_sets"] = [{"field_id": "Game:Game.Controller:speed", "direct_writers": []}]
    data["il_unlisted_writes"] = []
    data["components"] = [{"id": "component", "object_id": "entity", "scene_id": "scene",
                           "index": 2, "type_id": "Game:Game.Controller"}]
    data["fields"][0].update(component_id="component", object_id="entity",
                              declaration_id="Game:Game.Controller:speed", field_type_assembly="mscorlib")
    data["bindings"][0].update(source_field_id="field", binding_id="binding-guid",
                                binding_path="<Keyboard>/w", asset_path=None)
    for name in ("scenes", "components", "input_asset_actions", "fields", "bindings"):
        discovery["caps"][name] = cap(len(data[name]))
    for name in ("il_writes", "il_issues"):
        discovery["caps"][name] = cap(0)
    actions, _ = calibrate.discover_actions(data)
    calibration["actions"] = actions
    calibration["action_summaries"][0]["bindings"] = actions[0]["bindings"]
    calibration["action_summaries"][0]["holds"] = [
        {"requested_hold_s": 0.2, "valid_trials": 2, "mean_delta": [0, 0, 1],
         "mean_magnitude": 1, "sample_variance": 0, "effective_in_all_repeats": True}]
    inventory_data = {"unity_version": "6000.0.0", "input_handling": "both", "packages": [],
                      "scenes": [{"path": "Assets/scene.unity"}], "scripts": [{"path": "Assets/script.cs"}],
                      "input_assets": [], "legacy_axes": []}
    inventory = dict(discovery, stage="inventory", data=inventory_data,
                     caps={name: cap(len(inventory_data[name])) for name in cache._INVENTORY_CAPS})
    return (InspectResult.model_validate(inventory), InspectResult.model_validate(discovery),
            CalibrationRecord.model_validate(calibration))


def assembled(inventory, discovery, calibration):
    built = packet.build(inventory, discovery, calibration)
    answers = iter([
        {"stage_4": {"readers": [{"field_id": "field_00000", "alias": "speed", "why": "read"}],
                     "unobservable": []}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "move", "intent": "move", "hold_s": 0.2,
         "inputs": [{"control_id": "w", "x": None, "y": None}], "preconditions": []}],
         "skills": [], "uncontrollable": []}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [], "unjudgeable": []}},
    ])
    run = stages.run(built, lambda *_: stages.CallResult(json.dumps(next(answers)), None, {}), lambda _: None)
    return built, manifest.assemble(built, run)


def shifted(discovery):
    data = discovery.model_dump(mode="json")
    for section in ("game_objects", "components", "fields", "bindings"):
        for row in data["data"][section]:
            for key, value in list(row.items()):
                if key in {"id", "scene_id", "object_id", "component_id", "source_object_id", "source_field_id"} and isinstance(value, str):
                    row[key] = "new-" + value
    data["data"]["scenes"][0]["id"] = "new-scene"
    return InspectResult.model_validate(data)


def test_project_fingerprint_covers_authored_files_but_not_unity_runtime(tmp_path):
    root = project(tmp_path / "game")
    first = cache.capture_authored(root)
    (root / "Temp").mkdir()
    (root / "Temp" / "state").write_text("changed", encoding="utf-8")
    assert cache.capture_authored(root).sha256 == first.sha256
    for name in ("scene.unity", "script.cs", "prefab.prefab"):
        path = root / "Assets" / name
        original = path.read_bytes()
        path.write_bytes(original + b"x")
        assert cache.capture_authored(root).sha256 != first.sha256
        path.write_bytes(original)


def test_local_package_contents_are_hashed_without_the_absolute_path(tmp_path):
    fingerprints = []
    for name in ("one", "two"):
        root = project(tmp_path / name / "game")
        package = tmp_path / name / "local"
        package.mkdir()
        (package / "package.json").write_text('{"name":"example"}', encoding="utf-8")
        (package / "source.cs").write_text("same", encoding="utf-8")
        (root / "Packages" / "manifest.json").write_text(
            json.dumps({"dependencies": {"example": "file:" + package.as_posix()}}), encoding="utf-8")
        fingerprints.append(cache.capture_authored(root).sha256)
    assert fingerprints[0] == fingerprints[1]
    (tmp_path / "two" / "local" / "source.cs").write_text("changed", encoding="utf-8")
    assert cache.capture_authored(tmp_path / "two" / "game").sha256 != fingerprints[0]


def test_identity_ignores_runtime_ids_and_values_but_includes_limits_and_model(tmp_path):
    inventory, discovery, _ = records()
    authored = cache.capture_authored(project(tmp_path / "game"))
    first = cache.fingerprint(authored, inventory, discovery, model=cache.provider.CONTRIBUTOR_MODEL)
    moved = shifted(discovery)
    moved.data["fields"][0]["value"] = "different runtime state"
    assert cache.fingerprint(authored, inventory, moved, model=cache.provider.CONTRIBUTOR_MODEL) == first
    changed = moved.model_copy(deep=True)
    changed.caps["fields"] = changed.caps["fields"].model_copy(update={"limit": 101})
    assert cache.fingerprint(authored, inventory, changed, model=cache.provider.CONTRIBUTOR_MODEL) != first
    assert cache.fingerprint(authored, inventory, moved, model=cache.provider.meta.MODEL) != first
    changed = moved.model_copy(deep=True)
    changed.data["bindings"][0]["binding_path"] = "<Keyboard>/a"
    assert cache.fingerprint(authored, inventory, changed, model=cache.provider.CONTRIBUTOR_MODEL) != first


@pytest.mark.parametrize("problem", ["unknown", "truncated", "il_truncated", "il_unrecorded", "il_count",
                                     "count", "error"])
def test_incomplete_or_inconsistent_discovery_cannot_be_fingerprinted(tmp_path, problem):
    inventory, discovery, _ = records()
    authored = cache.capture_authored(project(tmp_path / "game"))
    data = discovery.model_dump(mode="json")
    if problem == "unknown":
        data["caps"]["fields"].update(observed=None, truncated=None)
    elif problem == "truncated":
        data["caps"]["fields"].update(observed=2, truncated=True)
    elif problem == "il_truncated":
        data["caps"]["il_issues"].update(observed=None, truncated=True)
    elif problem == "il_unrecorded":
        data["data"]["il_complete"] = False
    elif problem == "il_count":
        data["data"]["il_write_sets"][0]["direct_writers"].append({"method": "m", "il_offset": 1, "constructor": False})
    elif problem == "count":
        data["data"]["fields"] = []
    else:
        data["errors"] = ["il_type:Game:Game.Controller: TypeLoadException"]
    with pytest.raises(ValueError):
        cache.fingerprint(authored, inventory, InspectResult.model_validate(data), model=cache.provider.CONTRIBUTOR_MODEL)


def test_recorded_il_issues_are_part_of_the_key_rather_than_refused(tmp_path):
    """Indirect field addresses always leave a real game's IL scan incomplete; that is deterministic."""
    inventory, discovery, _ = records()
    authored = cache.capture_authored(project(tmp_path / "game"))
    data = discovery.model_dump(mode="json")
    data["data"]["il_complete"] = False
    data["data"]["il_issues"] = [
        {"kind": "IndirectWrite", "method": "Game:Game.Controller:Update()", "field_id": "Game:Game.Controller:ray",
         "il_offset": 7, "reason": "potential indirect write"},
        {"kind": "MissingMethodBody", "method": "Game:Game.Controller:Native()", "field_id": None,
         "il_offset": None, "reason": "no body"}]
    for name in ("il_issues", "il_writes"):
        data["caps"][name] = {"limit": 100, "observed": None,
                              "returned": len(data["data"]["il_issues"]) if name == "il_issues" else 0,
                              "truncated": None}
    uncertain = InspectResult.model_validate(data)
    model = cache.provider.CONTRIBUTOR_MODEL
    first = cache.fingerprint(authored, inventory, uncertain, model=model)
    assert first != cache.fingerprint(authored, inventory, discovery, model=model)
    reordered = uncertain.model_copy(deep=True)
    reordered.data["il_issues"].reverse()
    assert cache.fingerprint(authored, inventory, reordered, model=model) == first
    changed = uncertain.model_copy(deep=True)
    changed.data["il_issues"][0]["il_offset"] = 8
    assert cache.fingerprint(authored, inventory, changed, model=model) != first


def test_readers_and_entity_remap_only_when_the_frozen_descriptors_are_unique():
    inventory, original, calibration = records()
    _, value = assembled(inventory, original, calibration)
    current = shifted(original)
    assert cache.resolve_readers(value, original, current) == {"speed": "new-field"}
    assert cache.resolve_entity(value, original, current) == "new-entity"
    duplicate = current.model_copy(deep=True)
    duplicate.data["game_objects"].append(dict(duplicate.data["game_objects"][0], id="duplicate"))
    duplicate.caps["game_objects"] = type(duplicate.caps["game_objects"]).model_validate(cap(2))
    with pytest.raises(ValueError, match="uniquely"):
        cache.resolve_readers(value, original, duplicate)
    with pytest.raises(ValueError, match="uniquely"):
        cache.resolve_entity(value, original, duplicate)
    changed = current.model_copy(deep=True)
    changed.data["fields"][0].update(value_state="null", value_kind="null")
    with pytest.raises(ValueError, match="different current kind"):
        cache.resolve_readers(value, original, changed)
    changed.data["fields"][0].update(value_state="present", value_kind="unsupported")
    with pytest.raises(ValueError, match="unreadable"):
        cache.resolve_readers(value, original, changed)


def test_a_reader_that_was_null_resolves_only_while_it_stays_null():
    inventory, original, calibration = records()
    original.data["fields"][0].update(value_state="null", value_kind="null", value=None)
    _, value = assembled(inventory, original, calibration)
    assert value.readers[0].kind is None
    current = shifted(original)
    assert cache.resolve_readers(value, original, current) == {"speed": "new-field"}
    current.data["fields"][0].update(value_state="present", value_kind="number", value=1.5)
    with pytest.raises(ValueError, match="different current kind"):
        cache.resolve_readers(value, original, current)


def test_selected_control_must_keep_its_original_binding():
    inventory, original, calibration = records()
    built, value = assembled(inventory, original, calibration)
    assert cache.resolve_controls(value, original, original, built) == {"w": "w"}
    changed = original.model_copy(deep=True)
    changed.data["bindings"][0]["binding_id"] = "different-guid"
    with pytest.raises(ValueError, match="bindings changed"):
        cache.resolve_controls(value, original, changed, built)


def test_replay_preserves_choices_and_uses_current_field_ids_and_calibration():
    inventory, original, calibration = records()
    old_packet, value = assembled(inventory, original, calibration)
    current = shifted(original)
    fresh = calibration.model_dump(mode="json")
    fresh["entity_id"] = "new-entity"
    fresh["entity"] = current.data["game_objects"][0]
    fresh["actions"], _ = calibrate.discover_actions(current.data)
    fresh["action_summaries"][0]["bindings"] = fresh["actions"][0]["bindings"]
    fresh["action_summaries"][0]["holds"][0]["mean_delta"] = [0, 0, 2]
    fresh["action_summaries"][0]["holds"][0]["mean_magnitude"] = 2
    current_packet = packet.build(inventory, current, CalibrationRecord.model_validate(fresh))
    replayed, evidence = cache.replay_selections(value, original, current, current_packet)
    assert evidence == {"entity_id": "new-entity", "reader_ids": {"speed": "new-field"},
                        "controls": {"w": "w"}, "model_calls": 0}
    assert replayed.readers[0].alias == "speed" and replayed.readers[0].field_id == "new-field"
    assert replayed.actions[0].name == value.actions[0].name
    assert replayed.actions[0].calibrations[0].holds[0].mean_delta == (0, 0, 2)
    assert replayed.packet_sha256 != hashlib.sha256(old_packet.text.encode("utf-8")).hexdigest()
    assert manifest.validate_evidence(replayed, current_packet) == []


def test_replay_carries_operator_reach_regions_and_never_treats_them_as_model_output():
    """A reach region is an operator's assertion: replay keeps it, and comparison ignores it."""
    inventory, original, calibration = records()
    built, value = assembled(inventory, original, calibration)
    assert value.reach_regions == []
    declared = value.model_copy(update={"reach_regions": [ReachRegion.model_validate({
        "region_id": "entry_a", "label": "Entry A", "purpose": "An opaque test volume", "frame": "world",
        "scene": "scene", "bounds": {"min": [-1.0, 0.0, 2.0], "max": [1.0, 2.0, 4.0]},
        "pose": "probe_anchor", "author": "test operator", "source": "synthetic declaration for tests",
        "verification": "operator_asserted"})]})
    replayed, evidence = cache.replay_selections(declared, original, original, built)
    assert evidence["model_calls"] == 0
    assert replayed == declared and replayed.reach_regions == declared.reach_regions
    assert manifest.validate_evidence(replayed, built) == []
    stripped = manifest._without_proof(declared.model_dump(mode="json"))
    assert "reach_regions" not in stripped
    assert stripped == manifest._without_proof(value.model_dump(mode="json"))


def test_replay_keeps_unsupported_and_unmeasured_declarations_exactly():
    """A cache hit reuses the whole selection; it neither refuses nor drops what the bridge cannot run."""
    inventory, original, calibration = records()
    built = packet.build(inventory, original, calibration)
    stage_5 = {"actions": [
        {"name": "move", "intent": "move", "hold_s": 0.2, "inputs": [{"control_id": "w", "x": None, "y": None}],
         "preconditions": []},
        {"name": "nudge", "intent": "unmeasured hold", "hold_s": 0.3,
         "inputs": [{"control_id": "w", "x": None, "y": None}], "preconditions": []},
        {"name": "tap", "intent": "no measured minimum hold", "hold_s": None,
         "inputs": [{"control_id": "w", "x": None, "y": None}], "preconditions": []}],
        "skills": [{"name": "step_twice", "purpose": "repeat", "max_s": 2.0, "steps": [
            {"hold_s": 0.2, "inputs": [{"control_id": "w", "x": None, "y": None}], "wait_s": 0.1},
            {"hold_s": 0.2, "inputs": [{"control_id": "w", "x": None, "y": None}], "wait_s": 0.0}]}],
        "uncontrollable": []}
    answers = iter([
        {"stage_4": {"readers": [{"field_id": "field_00000", "alias": "speed", "why": "read"}],
                     "unobservable": []}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": stage_5, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [], "unjudgeable": []}},
    ])
    run = stages.run(built, lambda *_: stages.CallResult(json.dumps(next(answers)), None, {}), lambda _: None)
    value = manifest.assemble(built, run)
    support = {a.name: (a.execution_supported, a.hold_effect) for a in value.actions}
    # Calibration has no execution receipts, so no declaration may be called executable.
    assert support == {"move": (False, True), "nudge": (False, None), "tap": (False, None)}
    assert [s.execution_supported for s in value.skills] == [False]
    replayed, evidence = cache.replay_selections(value, original, original, built)
    assert replayed == value and evidence["model_calls"] == 0


def test_an_offered_axis_without_an_executable_action_is_an_incomplete_result():
    inventory, original, calibration = records()
    _, value = assembled(inventory, original, calibration)
    assert cache.undriven_axes(value) == []
    data = value.model_dump(mode="json")
    data["selection"]["offered_controls"].append({"id": "stick", "control_type": "Stick"})
    assert cache.undriven_axes(type(value).model_validate(data)) == ["stick"]
    # An offered axis that an executable action drives is complete.
    data["selection"]["offered_controls"][0]["control_type"] = "Axis"
    assert cache.undriven_axes(type(value).model_validate(data)) == ["stick"]
    data["actions"] = []
    data["gaps"]["uncontrollable"] = ["no action was composed"]
    assert cache.undriven_axes(type(value).model_validate(data)) == ["stick", "w"]


def cached_source(tmp_path):
    inventory, discovery, calibration = records()
    original = packet.build(inventory, discovery, calibration)
    answers = iter([
        {"stage_4": {"readers": [{"field_id": "field_00000", "alias": "speed", "why": "read"}],
                     "unobservable": []}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "move", "intent": "move", "hold_s": 0.2,
         "inputs": [{"control_id": "w", "x": None, "y": None}], "preconditions": []}],
         "skills": [], "uncontrollable": []}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [], "unjudgeable": []}},
    ])
    model = provider.CONTRIBUTOR_MODEL
    rows, reservations = [], []
    def call(messages, schema):
        request = provider.build_request(original, messages, schema, model=model)
        reservations.append({"kind": "reservation", "request": len(reservations) + 1,
            "model": model, "request_sha256": hashlib.sha256(
            strict.canonical(request).encode("utf-8")).hexdigest()})
        return stages.CallResult(json.dumps(next(answers)), None, {"model": model, "resolved_model": model})
    run = stages.run(original, call, lambda attempt: rows.append(
        {"kind": "attempt", "request": len(reservations), **json.loads(json.dumps(asdict(attempt)))}))
    value = manifest.assemble(original, run)
    files = cache.capture_authored(project(tmp_path / "game")).files
    before_time = datetime(2025, 12, 31, tzinfo=timezone.utc)
    after_time = datetime(2026, 1, 2, tzinfo=timezone.utc)
    before = cache.AuthoredSnapshot(files, before_time, before_time)
    after = cache.AuthoredSnapshot(files, after_time, after_time)
    entry = cache.create_entry(before, after, inventory, discovery, calibration, None, value,
                               rows, reservations, model=model)
    return entry, after, inventory, discovery, calibration, value


def subsequent(inventory, discovery, calibration):
    now = datetime(2026, 1, 3, tzinfo=timezone.utc)
    inventory = inventory.model_copy(update={"wall_utc": now})
    discovery = discovery.model_copy(update={"wall_utc": now})
    calibration = calibration.model_copy(update={"stop": calibration.stop.model_copy(
        update={"session": "fresh-calibration", "wall_utc": now})})
    return inventory, discovery, calibration


def test_validated_source_can_replay_with_no_model_call(tmp_path):
    entry, authored, inventory, discovery, calibration, value = cached_source(tmp_path)
    inventory, discovery, calibration = subsequent(inventory, discovery, calibration)
    replayed, result = cache.replay_entry(entry, authored, inventory, discovery, calibration, None,
                                          model=provider.CONTRIBUTOR_MODEL)
    assert replayed == value
    assert result["model_calls"] == 0 and result["stage7_verified"] is False


def test_changed_fingerprint_and_tampered_source_cannot_hit(tmp_path):
    entry, authored, inventory, discovery, calibration, _ = cached_source(tmp_path)
    inventory, discovery, calibration = subsequent(inventory, discovery, calibration)
    files = dict(authored.files)
    files["Assets/scene.unity"] = "0" * 64
    with pytest.raises(ValueError, match="fingerprint changed"):
        cache.replay_entry(entry, cache.AuthoredSnapshot(files), inventory, discovery, calibration, None,
                           model=provider.CONTRIBUTOR_MODEL)
    corrupted = copy.deepcopy(entry)
    corrupted["source"]["manifest"]["actions"][0]["name"] = "invented"
    corrupted["source_sha256"] = cache._digest(corrupted["source"])
    with pytest.raises(ValueError, match="ledger is invalid"):
        cache.replay_entry(corrupted, authored, inventory, discovery, calibration, None,
                           model=provider.CONTRIBUTOR_MODEL)


def test_cache_hit_requires_new_inspections_whose_descriptors_still_resolve(tmp_path):
    entry, authored, old_inventory, old_discovery, old_calibration, _ = cached_source(tmp_path)
    inventory, discovery, calibration = subsequent(old_inventory, old_discovery, old_calibration)
    model = provider.CONTRIBUTOR_MODEL
    with pytest.raises(ValueError, match="new calibration"):
        cache.replay_entry(entry, authored, inventory, discovery, old_calibration, None, model=model)
    with pytest.raises(ValueError, match="new inventory and discovery"):
        cache.replay_entry(entry, authored, inventory, old_discovery, calibration, None, model=model)
    changed = discovery.model_copy(deep=True)
    changed.data["fields"][0].update(value_state="present", value_kind="unsupported")
    with pytest.raises(ValueError, match="unreadable"):
        cache.replay_entry(entry, authored, inventory, changed, calibration, None, model=model)
    duplicated = discovery.model_copy(deep=True)
    duplicated.data["game_objects"].append(dict(duplicated.data["game_objects"][0], id="duplicate"))
    duplicated.caps["game_objects"] = type(duplicated.caps["game_objects"]).model_validate(cap(2))
    with pytest.raises(ValueError, match="uniquely"):
        cache.replay_entry(entry, authored, inventory, duplicated, calibration, None, model=model)


def test_sealing_requires_project_snapshots_to_bracket_the_source_run(tmp_path):
    entry, authored, inventory, discovery, calibration, value = cached_source(tmp_path)
    late = datetime(2026, 1, 3, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="bracket"):
        cache.create_entry(cache.AuthoredSnapshot(authored.files, late, late),
                           cache.AuthoredSnapshot(authored.files, late, late), inventory, discovery,
                           calibration, None, value, entry["source"]["rows"], entry["source"]["reservations"],
                           model=provider.CONTRIBUTOR_MODEL)


def test_cli_cache_hit_and_validation_make_no_model_calls(tmp_path, monkeypatch):
    entry, _, inventory, discovery, calibration, _ = cached_source(tmp_path)
    inventory, discovery, calibration = subsequent(inventory, discovery, calibration)
    directory = tmp_path / "cache"
    cache.write_entry(directory, entry)
    captured = cache.capture_authored(tmp_path / "game")
    before_time = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    before = cache.AuthoredSnapshot(captured.files, before_time, before_time)
    before_path = tmp_path / "current-before.json"
    before_path.write_text(json.dumps(cache._snapshot_data(before)), encoding="utf-8")
    inputs = {}
    for name, value in (("inventory", inventory), ("discovery", discovery), ("calibration", calibration)):
        path = tmp_path / (name + ".json")
        path.write_text(value.model_dump_json(), encoding="utf-8")
        inputs[name] = path
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    output, ledger = tmp_path / "result.json", tmp_path / "ledger.jsonl"
    monkeypatch.setattr(manifest_cli.provider, "Muse", lambda *_, **__: pytest.fail("unexpected model call"))
    args = ["--project", str(tmp_path / "game"), "--inventory", str(inputs["inventory"]),
            "--discovery", str(inputs["discovery"]), "--calibration", str(inputs["calibration"]),
            "--screenshot", str(image), "--screenshot-mime", "image/png", "--screenshot-provenance", "fixture",
            "--output", str(output), "--ledger", str(ledger), "--model", provider.CONTRIBUTOR_MODEL,
            "--cache-dir", str(directory), "--current-before", str(before_path)]
    assert manifest_cli.main(args) == 0
    event = json.loads(ledger.read_text(encoding="utf-8"))
    assert event["kind"] == "cache_hit" and event["model_calls"] == 0
    assert event["fingerprint"] == entry["fingerprint"]["sha256"]
    check = ["--manifest", str(output), "--ledger", str(ledger), "--inventory", str(inputs["inventory"]),
             "--discovery", str(inputs["discovery"]), "--calibration", str(inputs["calibration"]),
             "--screenshot", str(image), "--expected-model", provider.CONTRIBUTOR_MODEL,
             "--project", str(tmp_path / "game"), "--cache-dir", str(directory)]
    assert validate_cli.main(check) == 0
    event["reader_ids"]["speed"] = "tampered"
    ledger.write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert validate_cli.main(check) == 1


def test_cli_cache_miss_never_spends_without_a_budget(tmp_path, monkeypatch):
    entry, _, inventory, discovery, calibration, _ = cached_source(tmp_path)
    inventory, discovery, calibration = subsequent(inventory, discovery, calibration)
    directory = tmp_path / "cache"
    cache.write_entry(directory, entry)
    snapshot = cache.capture_authored(tmp_path / "game")
    before_path = tmp_path / "current-before.json"
    before_path.write_text(json.dumps(cache._snapshot_data(snapshot)), encoding="utf-8")
    (tmp_path / "game" / "Assets" / "scene.unity").write_text("changed", encoding="utf-8")
    inputs = {}
    for name, value in (("inventory", inventory), ("discovery", discovery), ("calibration", calibration)):
        inputs[name] = tmp_path / (name + ".json")
        inputs[name].write_text(value.model_dump_json(), encoding="utf-8")
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    monkeypatch.setattr(manifest_cli.provider, "Muse", lambda *_, **__: pytest.fail("unexpected model call"))
    args = ["--project", str(tmp_path / "game"), "--inventory", str(inputs["inventory"]),
            "--discovery", str(inputs["discovery"]), "--calibration", str(inputs["calibration"]),
            "--screenshot", str(image), "--screenshot-mime", "image/png", "--screenshot-provenance", "fixture",
            "--output", str(tmp_path / "result.json"), "--ledger", str(tmp_path / "ledger.jsonl"),
            "--model", provider.CONTRIBUTOR_MODEL, "--cache-dir", str(directory),
            "--current-before", str(before_path)]
    assert manifest_cli.main(args) == 1
    assert not (tmp_path / "ledger.jsonl").exists()


def test_snapshot_command_writes_only_outside_project(tmp_path):
    root = project(tmp_path / "game")
    assert cache_cli.main(["snapshot", "--project", str(root), "--output", str(root / "snapshot.json")]) == 1
    output = tmp_path / "snapshot.json"
    assert cache_cli.main(["snapshot", "--project", str(root), "--output", str(output)]) == 0
    assert cache._snapshot_from_data(json.loads(output.read_text(encoding="utf-8"))).files == cache.capture_authored(root).files
    assert cache_cli.main(["snapshot", "--project", str(root), "--output", str(output)]) == 1


def test_store_command_checks_original_evidence_and_refuses_overwrite(tmp_path):
    entry, _, _, _, _, _ = cached_source(tmp_path)
    source = entry["source"]
    paths = {}
    for name in ("before", "after", "inventory", "discovery", "calibration", "manifest"):
        paths[name] = tmp_path / (name + ".json")
        paths[name].write_text(json.dumps(source[name]), encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(json.dumps(item) for pair in zip(source["reservations"], source["rows"])
                                for item in pair) + "\n", encoding="utf-8")
    source_time = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    for path in (paths["manifest"], ledger):
        os.utime(path, (source_time, source_time))
    directory = tmp_path / "cache"
    args = ["store", "--project", str(tmp_path / "game"), "--before", str(paths["before"]),
            "--after", str(paths["after"]), "--inventory", str(paths["inventory"]),
            "--discovery", str(paths["discovery"]), "--calibration", str(paths["calibration"]),
            "--manifest", str(paths["manifest"]), "--ledger", str(ledger), "--cache-dir", str(directory),
            "--model", provider.CONTRIBUTOR_MODEL]
    assert cache_cli.main(args) == 0
    assert cache.read_entry(directory, entry["fingerprint"]["sha256"]) == entry
    assert cache_cli.main(args) == 1


def test_cached_selection_does_not_inherit_execution_proof():
    inventory, original, calibration = records()
    data = calibration.model_dump(mode="json")
    data["execution_evidence"] = [{
        "action": "move", "session": "s-1", "status": "completed", "release_verified": True,
        "declaration": {"backend": "input_system",
                        "inputs": [{"device": "Keyboard", "control": "w", "x": None, "y": None}],
                        "default_lease_s": 0.2},
        "applied_duration_s": 0.201, "input_start_wall_s": 1000.0, "input_end_wall_s": 1000.201,
        "receipts": [{"step_index": 0, "device": "Keyboard", "control": "w", "x": None, "y": None,
                      "input_start_wall_s": 1000.0, "input_end_wall_s": 1000.201,
                      "applied_duration_s": 0.201, "applied_delta_x": None, "applied_delta_y": None,
                      "release_verified": True}],
        "wall_utc": "2026-01-01T00:00:00Z"}]
    with_evidence = CalibrationRecord.model_validate(data)
    built = packet.build(inventory, original, with_evidence)
    answers = iter([
        {"stage_4": {"readers": [{"field_id": "field_00000", "alias": "speed", "why": "read"}],
                     "unobservable": []}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "move", "intent": "move", "hold_s": 0.2,
         "inputs": [{"control_id": "w", "x": None, "y": None}], "preconditions": []}],
         "skills": [], "uncontrollable": []}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [], "unjudgeable": []}},
    ])
    run = stages.run(built, lambda *_: stages.CallResult(json.dumps(next(answers)), None, {}), lambda _: None)
    value = manifest.assemble(built, run)
    assert value.actions[0].execution_supported is True

    fresh = calibration.model_copy(update={"execution_evidence": []})
    fresh_packet = packet.build(inventory, original, fresh)
    replayed, _ = cache.replay_selections(value, original, original, fresh_packet)
    assert replayed.actions[0].name == "move"
    assert replayed.actions[0].execution_supported is False
    assert replayed.actions[0].execution_evidence is None
