"""Offer every readable field, or fill the provider's enum budget in tiers when they do not fit."""

import copy
import hashlib
import json

import pytest

from playtest.contracts.bridge import InspectResult
from playtest.contracts.calibration import CalibrationRecord
from playtest.preflight import packet as pack
from playtest.preflight.calibrate import discover_actions
from playtest.preflight.packet import build
from playtest.roles import preflight, strict


def cap(n):
    return {"limit": 100000, "observed": n, "returned": n, "truncated": False}


def evidence(field_count=1):
    """A minimal compatible inventory, discovery and calibration record; all names are synthetic."""
    binding = {"id": "binding", "action_id": "action", "binding_effective_path": "<Keyboard>/w",
               "binding_name": "up", "source_object_id": "entity", "binding_is_composite": False}
    entity = {"id": "entity", "scene_id": "scene", "scene_path": "Assets/scene", "path": "root"}
    fields = [{"id": f"field:{i:04d}", "field_type": "System.Single", "scene_id": "scene",
               "declaring_assembly": "Game", "value_state": "present", "value_kind": "number"}
              for i in range(field_count)]
    data = {"game_objects": [entity], "components": [], "fields": fields, "bindings": [binding],
            "input_asset_bindings": [], "il_scope": "direct_stores_in_assemblies_with_authored_scripts",
            "il_complete": False, "il_write_sets": [{"field_id": "Game:Type:field", "direct_writers": []}],
            "source_excerpts": [{"path": "Assets/Test.cs", "text": "example"}]}
    caps = {key: cap(len(data[key])) for key in
            ("game_objects", "components", "fields", "bindings", "input_asset_bindings")}
    caps.update({"source_files": cap(1), "source_characters": cap(7),
                 "il_assemblies": cap(1), "il_types": cap(1), "il_fields": cap(1)})
    snapshot = {"protocol": "upa/1", "session": "s", "seq": 1, "game_s": 1.0,
                "wall_utc": "2026-01-01T00:00:00Z", "type": "inspection", "stage": "discovery",
                "in_reply_to": 0, "data": data, "caps": caps, "errors": []}
    actions, unsupported = discover_actions(data)
    summary = {"control": "w", "bindings": actions[0]["bindings"], "resolution": "unity_key_name",
               "holds": [], "lowest_tested_effective_requested_hold_s": None,
               "shortest_observed_effective_hold_s": None, "minimum_hold_scope": "unknown",
               "inverted_relative_to_entity_basis": None, "signed_basis_projections": [],
               "dead_zone": None, "dead_zone_reason": "not measured"}
    record = {"schema_version": 1, "status": "measured_with_gaps", "error": None,
              "entity_id": "entity", "entity": entity, "coordinate_frame": "world", "units": "units",
              "physical_unit": None, "plan": {}, "action_cap": cap(1), "actions": actions,
              "unsupported_bindings": unsupported, "discovery_coverage": {}, "idle_fields": [],
              "input_field_segments": [], "trials": [{"control": "w", "valid": True}],
              "action_summaries": [summary], "freshness": {"field:0000": {"classification": "idle"}},
              "clock": {}, "reset": {}, "runtime_errors": None,
              "stop": {"protocol": "upa/1", "session": "s", "seq": 2, "game_s": 1.0,
                       "wall_utc": "2026-01-01T00:00:00Z", "type": "stopped", "released": [],
                       "release_verified": True}}
    return snapshot, record


def inventory_of(snapshot, scripts=("Assets/Game/Thing.cs",), packages=()):
    """The survey side: which authored scripts and packages the project has."""
    inventory = copy.deepcopy(snapshot)
    inventory.update(stage="inventory", data={
        "scripts": [{"path": path} for path in scripts],
        "packages": [dict(row) for row in packages]}, caps={"scripts": cap(len(scripts))}, errors=[])
    inventory["caps"]["packages"] = cap(len(packages))
    return inventory


def make(snapshot, record, inventory=None):
    inventory = inventory if inventory is not None else inventory_of(snapshot)
    return build(InspectResult.model_validate(inventory), InspectResult.model_validate(snapshot),
                 CalibrationRecord.model_validate(record))


def limits_for(cap_size, *, big_enum=1000, big_enum_chars=15000):
    """Limits whose field token cap is `cap_size`, using the renderer's own reserve for one control."""
    reserved = pack.field_token_budget({"w": "Button"}, strict.MUSE_LIMITS)[1]
    return strict.Limits(enum_values=cap_size + reserved, big_enum=big_enum,
                         big_enum_chars=big_enum_chars)


def scan_caps(complete):
    """The three IL coverage caps the merged scan reports."""
    return {name: cap(1) if complete else
            {"limit": 1, "observed": 2, "returned": 1, "truncated": True} for name in pack.SCAN_COVERAGE}


def plan(*, rows, readable, evidence_rows=None, project=("Game",), il=True, scripts=("Assets/Game/Thing.cs",),
         packages=(), limits=None, scan_complete=True):
    """Call the offer rule with synthetic rows, a script list and explicit limits."""
    discovery = {"il_scope": "direct_stores_in_assemblies_with_authored_scripts",
                 "il_write_sets": [{"field_id": f"{name}:Type:field"} for name in project]} if il else {}
    inventory = {"scripts": [{"path": path} for path in scripts], "packages": list(packages)}
    caps = {"scripts": cap(len(scripts)), "packages": cap(len(packages))}
    scan = scan_caps(scan_complete) if il else {}
    return pack.offer_fields(fields=rows, bindings=readable, discovery=discovery, inventory=inventory,
                             caps=caps, scan_caps=scan, evidence=evidence_rows or {},
                             control_types={"w": "Button"},
                             limits=limits if limits is not None else limits_for(2))


def rows_for(assemblies):
    """Field rows from (id, assembly) pairs, keeping the given discovery order."""
    return {ident: {"id": ident, "declaring_assembly": assembly} for ident, assembly in assemblies}


def tokens_for(rows):
    """One token per row, in the given discovery order."""
    return {f"field_{index:05d}": ident for index, ident in enumerate(rows)}


def test_a_packet_inside_the_limits_is_unchanged_and_offers_every_readable_field():
    snapshot, record = evidence(field_count=6)
    packet = make(snapshot, record)
    data = json.loads(packet.text)
    assert "offer_budget" not in data and "offer_note" not in data
    assert len(packet.field_bindings) == 6
    assert set(packet.field_bindings) == set(packet.discovery.field_types)
    assert [row["id"] for row in data["offered_fields"]] == list(packet.field_bindings)
    assert len(data["unoffered_fields"]) == 0
    preflight.schema(packet.discovery)


def test_an_unreadable_field_keeps_its_existing_unoffered_shape():
    snapshot, record = evidence(field_count=3)
    snapshot["data"]["fields"][0].update(value_state="unsupported", value_kind="unsupported")
    data = json.loads(make(snapshot, record).text)
    assert "offer_budget" not in data
    assert data["unoffered_fields"] == [{"source_field_id": "field:0000", "value_state": "unsupported",
                                         "value_kind": "unsupported"}]


def test_over_the_limits_the_stage_four_schema_still_renders_and_nothing_is_lost():
    snapshot, record = evidence(field_count=1100)
    packet = make(snapshot, record)
    preflight.schema(packet.discovery)  # the same check that refused before
    data = json.loads(packet.text)
    budget = data["offer_budget"]
    assert budget["applied"] is True
    assert budget["readable_fields"] == 1100
    assert budget["offered_fields"] == len(packet.field_bindings) == sum(
        tier["offered"] for tier in budget["tiers"].values())
    assert budget["offered_fields"] <= budget["field_token_cap"]
    assert budget["limits"]["enum_values"] == strict.MUSE_LIMITS.enum_values
    dropped = [row for row in data["unoffered_fields"] if row["reason"] == pack.OFFER_BUDGET_REASON]
    assert len(dropped) == 1  # one compact summary row, not one row per field
    assert dropped[0]["count"] == budget["unoffered_fields"] == 1100 - budget["offered_fields"]
    assert sum(dropped[0]["by_tier"].values()) == dropped[0]["count"]
    assert sum(dropped[0]["by_declaring_assembly"].values()) == dropped[0]["count"]
    # Every field that left the packet is in the sidecar, in discovery order, and the packet
    # records the sidecar's hash and row count.
    sidecar = json.loads(packet.sidecar)
    offered_ids = set(packet.field_bindings.values())
    assert [row["source_field_id"] for row in sidecar["fields"]] == [
        row["id"] for row in snapshot["data"]["fields"] if row["id"] not in offered_ids]
    assert len(sidecar["fields"]) == data["sidecar"]["field_rows"] == data["sidecar"]["rows"]
    assert data["sidecar"]["object_rows"] == 0
    assert data["sidecar"]["sha256"] == hashlib.sha256(packet.sidecar.encode("utf-8")).hexdigest()
    assert len({row["source_field_id"] for row in sidecar["fields"]} | offered_ids) == 1100
    # The sidecar keeps the tier, basis and read state that explained each field.
    budget_rows = [row for row in sidecar["fields"] if row.get("reason") == pack.OFFER_BUDGET_REASON]
    assert len(budget_rows) == budget["unoffered_fields"]
    assert all(row["tier"] in pack.OFFER_TIERS and row["basis"] for row in budget_rows)
    assert all(row["descriptor"]["id"] == row["source_field_id"] for row in sidecar["fields"])


def test_the_tiers_are_filled_in_order_and_a_partly_fitting_tier_is_cut_deterministically():
    rows = rows_for([("project:a", "Game"), ("other:a", "Framework"), ("project:b", "Game"),
                     ("other:b", "Framework"), ("other:c", "Framework")])
    readable = tokens_for(rows)
    result = plan(rows=rows, readable=readable)
    assert result.summary is not None
    assert result.summary["field_token_cap"] == 2
    assert list(result.bindings.values()) == ["project:a", "project:b"]
    assert result.summary["cut_tier"] == pack.TIER_REST
    assert result.summary["tiers"][pack.TIER_PROJECT]["readable"] == 2
    assert result.summary["tiers"][pack.TIER_PROJECT]["offered"] == 2
    assert result.summary["tiers"][pack.TIER_PROJECT]["unoffered"] == 0
    assert result.summary["tiers"][pack.TIER_REST]["unoffered"] == 3
    assert [ident for ident, _, _ in result.dropped] == ["other:a", "other:b", "other:c"]
    again = plan(rows=rows, readable=readable)
    assert (again.bindings, again.dropped, again.summary) == (result.bindings, result.dropped, result.summary)


def test_a_measured_changing_framework_field_outranks_an_unchanged_one():
    rows = rows_for([("framework:quiet", "Framework"), ("framework:moving", "Framework")])
    readable = tokens_for(rows)
    result = plan(rows=rows, readable=readable, limits=limits_for(1),
                  evidence_rows={"framework:moving": {"classification": "input"}})
    assert list(result.bindings.values()) == ["framework:moving"]
    assert result.summary["tiers"][pack.TIER_MEASURED]["offered"] == 1
    assert result.summary["cut_tier"] == pack.TIER_REST
    assert [(ident, tier) for ident, tier, _ in result.dropped] == [("framework:quiet", pack.TIER_REST)]
    assert "no measured change" in result.dropped[0][2]


@pytest.mark.parametrize(("classification", "counted"), [
    ("both", True), ("idle", True), ("input", True),
    ("no_change_observed", False), ("unknown", False), (None, False)])
def test_only_an_observed_change_is_measured_evidence(classification, counted):
    assert pack.measured_change({"classification": classification}) is counted
    assert pack.measured_change(None) is False
    assert pack.measured_change("input") is False
    assert pack.measured_change({}) is False


def test_per_window_change_classes_count_when_the_overall_class_is_missing():
    assert pack.measured_change({"idle": {"classification": "observed_change"}}) is True
    assert pack.measured_change({"input": {"classification": "observed_continuous"}}) is True
    assert pack.measured_change({"idle": {"classification": "no_change_observed"},
                                 "input": {"classification": "unknown"}}) is False


def test_an_assembly_with_unknown_build_provenance_is_never_tier_one():
    rows = rows_for([("project:a", "Game"), ("other:a", "Framework")])
    readable = tokens_for(rows)
    without_scan = plan(rows=rows, readable=readable, il=False, limits=limits_for(1))
    assert without_scan.summary["project_assemblies"] == []
    assert without_scan.summary["tiers"][pack.TIER_PROJECT]["readable"] == 0
    assert "build provenance is unknown" in without_scan.summary["tiers"][pack.TIER_REST]["basis"]
    outside = plan(rows=rows, readable=readable,
                   scripts=("Assets/Game/Thing.cs", "Packages/downloaded.thing/Script.cs"),
                   packages=[{"name": "downloaded.thing", "source": "Registry",
                              "asset_path": "Packages/downloaded.thing"}], limits=limits_for(1))
    assert outside.summary["project_assemblies"] == []
    assert outside.summary["tiers"][pack.TIER_PROJECT]["readable"] == 0
    own = plan(rows=rows, readable=readable, scripts=("Packages/vendored.game/Script.cs",),
               packages=[{"name": "vendored.game", "source": "Embedded",
                          "asset_path": "Packages/vendored.game"}], limits=limits_for(1))
    assert own.summary["project_assemblies"] == ["Game"]
    assert own.summary["tiers"][pack.TIER_PROJECT]["readable"] == 1


def test_provenance_needs_a_complete_survey_of_scripts_and_packages():
    discovery = {"il_scope": "scope", "il_write_sets": [{"field_id": "Game:Type:field"}]}
    scripts = {"scripts": [{"path": "Assets/Game/Thing.cs"}]}
    truncated = {"scripts": {"limit": 1, "observed": 3, "returned": 1, "truncated": True}}
    assert pack.project_assemblies(discovery, scripts, truncated)[0] == frozenset()
    assert pack.project_assemblies(discovery, {"scripts": [{"path": ""}]}, {"scripts": cap(1)})[0] == frozenset()
    assert pack.project_assemblies(discovery, {"scripts": []}, {})[0] == frozenset()
    # A truncated package list cannot attribute a script outside Assets/ either.
    inventory = {"scripts": [{"path": "Packages/p/Script.cs"}],
                 "packages": [{"name": "p", "source": "Local", "asset_path": "Packages/p"}]}
    caps = {"scripts": cap(1), "packages": {"limit": 1, "observed": 2, "returned": 1, "truncated": True}}
    assert pack.project_assemblies(discovery, inventory, caps)[0] == frozenset()
    assert pack.project_assemblies({}, {}, {})[0] == frozenset()


def test_an_incomplete_il_scan_still_names_its_own_assemblies_but_claims_no_others():
    rows = rows_for([("project:a", "Game"), ("other:a", "Framework")])
    readable = tokens_for(rows)
    partial = plan(rows=rows, readable=readable, scan_complete=False, limits=limits_for(1))
    assert partial.summary["project_assemblies"] == ["Game"]
    assert partial.summary["assembly_scan_complete"] is False
    assert partial.summary["tiers"][pack.TIER_PROJECT]["readable"] == 1
    assert partial.summary["tiers"][pack.TIER_REST]["basis"].startswith(
        "the declaring assembly's build provenance is unknown")
    whole = plan(rows=rows, readable=readable, limits=limits_for(1))
    assert whole.summary["assembly_scan_complete"] is True
    assert whole.summary["tiers"][pack.TIER_REST]["basis"].startswith(
        "declared outside the project's assemblies")
    # The completeness flag is reported by the provenance function itself.
    discovery = {"il_scope": "scope", "il_write_sets": [{"field_id": "Game:Type:field"}]}
    inventory = {"scripts": [{"path": "Assets/Game/Thing.cs"}]}
    assert pack.project_assemblies(discovery, inventory, caps={"scripts": cap(1)})[3] is False
    assert pack.project_assemblies(discovery, inventory, caps={"scripts": cap(1)},
                                   scan_caps=scan_caps(True))[3] is True
    assert pack.project_assemblies(discovery, inventory, caps={"scripts": cap(1)},
                                   scan_caps=scan_caps(False))[3] is False


def test_every_readable_field_left_out_is_recorded_with_its_reason_tier_and_basis():
    snapshot, record = evidence(field_count=1100)
    snapshot["data"]["fields"][0].update(value_state="unsupported", value_kind="unsupported")
    packet = make(snapshot, record)
    data = json.loads(packet.text)
    # One summary row per reason, never one row per field, and every field's own record is in the
    # sidecar with the same reason.
    summary = {row["reason"]: row for row in data["unoffered_fields"]}
    assert set(summary) == {pack.NOT_READABLE_REASON, pack.OFFER_BUDGET_REASON}
    assert summary[pack.NOT_READABLE_REASON]["count"] == 1
    assert summary[pack.NOT_READABLE_REASON]["by_value_state"] == {"unsupported": 1}
    sidecar = json.loads(packet.sidecar)["fields"]
    rows = {row["source_field_id"]: row for row in sidecar}
    assert len(rows) == len(sidecar)  # one record per field, never two
    assert rows["field:0000"].get("reason") is None  # unreadable, so not an offer-budget record
    readable = {row["source_field_id"] for row in sidecar} - {"field:0000"}
    dropped = {ident for ident, row in rows.items() if row.get("reason") == pack.OFFER_BUDGET_REASON}
    assert dropped == readable - set(packet.field_bindings.values())
    assert all(rows[ident]["tier"] in pack.OFFER_TIERS and rows[ident]["basis"] for ident in dropped)
    assert set(rows) == (readable - set(packet.field_bindings.values())) | {"field:0000"}
    assert summary[pack.OFFER_BUDGET_REASON]["count"] == len(dropped)


def test_a_measured_field_that_the_budget_dropped_keeps_its_tier():
    rows = rows_for([("project:a", "Game")] + [(f"other:{i}", "Framework") for i in range(4)])
    readable = tokens_for(rows)
    result = plan(rows=rows, readable=readable, evidence_rows={"other:3": {"classification": "both"}})
    assert list(result.bindings.values()) == ["project:a", "other:3"]
    dropped = {ident: (tier, basis) for ident, tier, basis in result.dropped}
    assert sorted(dropped) == ["other:0", "other:1", "other:2"]
    assert {tier for tier, _ in dropped.values()} == {pack.TIER_REST}


def test_the_budget_counts_the_other_enums_in_the_schema():
    limits = strict.MUSE_LIMITS
    cap_size, reserved = pack.field_token_budget({"w": "Button", "a": "Button"}, limits)
    assert reserved >= 2 and cap_size == limits.enum_values - reserved
    probe = preflight.schema(preflight.Discovery({"field_00000": ""}, {"w": "Button", "a": "Button"}),
                             limits=None)
    assert strict.stats(probe).enum_values_definitions == reserved + 1
    # Without a measured control the renderer still needs one enum value for its sentinel.
    assert pack.field_token_budget({}, limits) == pack.field_token_budget(
        {"__no_control_available__": "Button"}, limits)


def test_the_provider_limit_is_applied_at_the_real_boundary():
    below = strict.MUSE_LIMITS.enum_values - 100
    packet = make(*evidence(field_count=below))
    assert "offer_budget" not in json.loads(packet.text)
    assert len(packet.field_bindings) == below
    # One more readable field than the old code could render: the budget now applies instead.
    snapshot, record = evidence(field_count=strict.MUSE_LIMITS.enum_values + 1)
    packet = make(snapshot, record)
    every_token = preflight.Discovery({token: "System.Single" for token in
                                       (f"field_{index:05d}"
                                        for index in range(strict.MUSE_LIMITS.enum_values + 1))},
                                      {"w": "Button"})
    with pytest.raises(ValueError, match="strict subset"):
        preflight.schema(every_token)
    preflight.schema(packet.discovery)
    assert len(packet.field_bindings) == json.loads(packet.text)["offer_budget"]["offered_fields"]
    assert len(packet.field_bindings) < strict.MUSE_LIMITS.enum_values + 1
