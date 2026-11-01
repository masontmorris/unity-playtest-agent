"""A binding is one binding however its asset was reached, and unmeasured stays unknown."""

from __future__ import annotations

import copy

from playtest.preflight import bindings, cache
from playtest.preflight.bindings import (MEASURED_FIELDS, PROJECT_ASSET, SCENE_FIELD, binding_set, by_id,
                                         measured_flag, measured_text, normalize, unknown_fields)
from playtest.preflight.calibrate import discover_actions


ASSET = "Assets/Input/Bindings.inputactions"
ACTION = "0a0a0a0a-0000-0000-0000-000000000001"
BINDING = "0b0b0b0b-0000-0000-0000-000000000002"
OTHER_BINDING = "0c0c0c0c-0000-0000-0000-000000000003"
FIELD = "scene:Boot.unity@0::1::component:7::field:Example.InputHolder::m_Actions"
OBJECT = "scene:Boot.unity@0::1"


def scene_row(**over):
    """A binding reached through a scene component field, in the shape the bridge emits."""
    row = {"id": FIELD + "::binding:" + BINDING, "source_field_id": FIELD, "source_object_id": OBJECT,
           "source_scene_id": "scene:Boot.unity@0", "source_field_name": "m_Actions",
           "source_declaring_type": "Example.InputHolder", "source_input_kind": "InputActionAsset",
           "binding_scope": "map", "binding_index": 0,
           "asset_path": ASSET, "map_id": "0d0d0d0d-0000-0000-0000-000000000004", "map_name": "MapOne",
           "action_id": ACTION, "action_name": "ActionOne", "action_type": "Value",
           "expected_control_type": "Axis", "binding_id": BINDING, "binding_name": "",
           "binding_path": "<Keyboard>/q", "binding_effective_path": "<Keyboard>/q",
           "binding_is_composite": False, "binding_is_part_of_composite": False}
    row.update(over)
    return row


def asset_row(**over):
    """The same binding read from the project's asset, in the shape the updated collector emits."""
    row = {"id": BINDING, "asset_path": ASSET, "map_id": "0d0d0d0d-0000-0000-0000-000000000004",
           "map_name": "MapOne", "action_id": ACTION, "action_type": "Value",
           "expected_control_type": "Axis", "binding_id": BINDING, "binding_name": "",
           "binding_path": "<Keyboard>/q", "binding_effective_path": "<Keyboard>/q",
           "binding_is_composite": False, "binding_is_part_of_composite": False,
           "provenance": PROJECT_ASSET}
    row.update(over)
    return row


def legacy_asset_row(**over):
    """A project-asset row from a capture that predates the shared fields."""
    row = {"asset_path": "Assets/Input/Legacy.inputactions", "map_name": "MapOne",
           "map_id": "0e0e0e0e-0000-0000-0000-000000000005", "id": OTHER_BINDING, "name": "Part",
           "action": "ActionOne", "path": "1DAxis", "groups": "", "interactions": "",
           "processors": "", "is_composite": False, "is_part_of_composite": False}
    row.update(over)
    return row


def test_a_binding_reached_both_ways_is_one_binding():
    discovery = {"bindings": [scene_row()], "input_asset_bindings": [asset_row()]}
    records = binding_set(discovery)
    assert len(records) == 1
    kept = records[0]
    assert kept["provenance"] == SCENE_FIELD and kept["origin_section"] == "bindings"
    assert kept["provenance_all"] == [SCENE_FIELD, PROJECT_ASSET]
    assert kept["merged_ids"] == [BINDING]
    assert kept["binding_effective_path"] == "<Keyboard>/q"
    assert by_id(records) == {kept["id"]: kept, BINDING: kept}
    actions, unsupported = discover_actions(discovery)
    assert [(item["control"], [finding["binding_id"] for finding in item["bindings"]]) for item in actions] \
        == [("q", [kept["id"]])]
    assert unsupported == []
    # A descriptor names the binding's own ids and paths; it never claims a scene object that
    # did not reach it, and the recorded reach stays readable in the binding set.
    assert actions[0]["bindings"][0]["source_object_id"] == OBJECT


def test_a_project_asset_only_binding_is_never_presented_as_scene_referenced():
    discovery = {"bindings": [], "input_asset_bindings": [asset_row()]}
    records = binding_set(discovery)
    assert [record["provenance"] for record in records] == [PROJECT_ASSET]
    assert records[0]["origin_section"] == "input_asset_bindings"
    assert records[0]["provenance_all"] == [PROJECT_ASSET]
    assert records[0]["merged_ids"] == []
    actions, unsupported = discover_actions(discovery)
    assert [finding["source_object_id"] for finding in actions[0]["bindings"]] == [None]
    # Provenance is read from the binding itself: the project's asset reached it, no scene field did.
    assert [record["provenance"] for record in binding_set(discovery)] == [PROJECT_ASSET]
    assert unsupported == []


def test_two_scene_fields_that_reach_one_binding_stay_two_reaches():
    second = scene_row(id=FIELD + "::second::binding:" + BINDING, source_field_id=FIELD + "::second",
                       source_object_id=OBJECT + "::second")
    records = binding_set({"bindings": [scene_row(), second]})
    assert [record["source_object_id"] for record in records] == [OBJECT, OBJECT + "::second"]
    assert [record["provenance_all"] for record in records] == [[SCENE_FIELD], [SCENE_FIELD]]
    # One project reach folds into one scene reach, not into both.
    records = binding_set({"bindings": [scene_row(), second], "input_asset_bindings": [asset_row()]})
    assert [record["provenance_all"] for record in records] == [[SCENE_FIELD, PROJECT_ASSET], [SCENE_FIELD]]


def test_an_older_row_without_the_shared_fields_is_unknown_and_not_dropped():
    legacy = legacy_asset_row()
    discovery = {"bindings": [], "input_asset_bindings": [legacy]}
    records = binding_set(discovery)
    assert len(records) == 1, "an older row is evidence and is kept"
    record = records[0]
    assert record["provenance"] == PROJECT_ASSET
    assert record["identity"] is None, "a row that named no binding id is never merged on a guess"
    assert set(MEASURED_FIELDS) <= set(record["unknown"])
    for name in MEASURED_FIELDS:
        assert (measured_flag if name.startswith("binding_is") else measured_text)(record, name) is None
    # The old field names are left as they were: nothing is aliased into a shared field.
    assert record["path"] == "1DAxis" and measured_text(record, "binding_effective_path") is None
    assert record["is_composite"] is False and measured_flag(record, "binding_is_composite") is None
    actions, unsupported = discover_actions(discovery)
    assert actions == [], "an unknown path is not pressed"
    assert len(unsupported) == 1
    assert unsupported[0]["path"] is None and unsupported[0]["unmeasured"]
    assert "not measured" in unsupported[0]["reason"]
    assert unsupported[0]["provenance"] == PROJECT_ASSET


def test_a_scene_only_discovery_presses_and_identifies_the_same_bindings_as_before():
    """The scene-only shape is the one FPSKit captures have: same controls, same cache identity."""
    rows = [scene_row(),
            scene_row(id=FIELD + "::other::binding:" + OTHER_BINDING, binding_id=OTHER_BINDING,
                      binding_path="<Keyboard>/space", binding_effective_path="<Keyboard>/space",
                      expected_control_type="Button", action_type="Button", binding_index=1),
            scene_row(id=FIELD + "::composite", binding_id="", binding_path="2DVector",
                      binding_effective_path="2DVector", binding_is_composite=True, binding_index=2),
            scene_row(id=FIELD + "::pointer", binding_id="0f0f0f0f-0000-0000-0000-000000000006",
                      binding_path="<Mouse>/position", binding_effective_path="<Mouse>/position",
                      expected_control_type="Vector2", binding_index=3)]
    discovery = {"bindings": rows, "input_asset_bindings": []}
    # The pre-change construction: every scene row, labelled by its own section.
    before = [{"section": "bindings", **{key: row.get(key) for key in cache._BINDING_KEYS}} for row in rows]
    after = [{"section": record["origin_section"], **{key: record.get(key) for key in cache._BINDING_KEYS}}
             for record in binding_set(discovery)]
    assert after == before
    assert cache._digest(after) == cache._digest(before)
    actions, unsupported = discover_actions(discovery)
    assert {item["control"]: [finding["binding_id"] for finding in item["bindings"]] for item in actions} == {
        "q": [rows[0]["id"]], "space": [rows[1]["id"]]}
    assert [item["path"] for item in unsupported] == ["<Mouse>/position"]
    assert unsupported[0]["provenance"] == SCENE_FIELD and unsupported[0]["unmeasured"] == []


def test_the_bindings_half_of_the_cache_key_is_unchanged_for_a_scene_only_discovery(tmp_path):
    """Any edit to this module changes the policy digest by design; a dedup would change this half."""
    from test_preflight_cache import project, records

    inventory, discovery, _ = records()
    assert discovery.data["input_asset_bindings"] == []
    before = sorted([{"section": "bindings", **{key: row.get(key) for key in cache._BINDING_KEYS}}
                     for row in discovery.data["bindings"]], key=cache._canonical)
    identity = cache.fingerprint(cache.capture_authored(project(tmp_path / "game")), inventory, discovery,
                                 model=cache.provider.CONTRIBUTOR_MODEL)["identity"]
    assert identity["bindings_sha256"] == cache._digest(before)


def test_normalize_keeps_measured_values_and_does_not_blend_an_unmeasured_field():
    row = asset_row(expected_control_type="", action_type=None)
    original = copy.deepcopy(row)
    record = normalize(row, PROJECT_ASSET)
    assert record["expected_control_type"] == "" and record["action_type"] is None
    assert "expected_control_type" in record["unknown"] and "action_type" in record["unknown"]
    assert set(unknown_fields(record)) == {"expected_control_type", "action_type"}
    assert row == original, "normalize reads its row and does not mutate it"


def test_a_row_without_a_binding_id_has_no_identity_while_a_guid_is_case_folded():
    assert bindings.identity({"binding_id": ""}) is None
    assert bindings.identity({"action_id": ACTION, "binding_effective_path": "<Keyboard>/q"}) is None
    assert (bindings.identity({"binding_id": BINDING.upper(), "asset_path": ASSET, "action_id": ACTION})
            == bindings.identity({"binding_id": BINDING, "asset_path": ASSET.replace("/", "\\"),
                                  "action_id": ACTION}))
    assert (bindings.identity({"binding_id": BINDING, "asset_path": ASSET, "action_id": ACTION})
            != bindings.identity({"binding_id": BINDING, "asset_path": "Assets/Input/Other.inputactions",
                                  "action_id": ACTION}))
