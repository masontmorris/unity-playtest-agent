"""What the project's own scripts reference outranks fields nothing measured; pooled fields say how many they are.

Calibration cannot trigger a rare event, so the text a script writes about one is never measured and,
once the provider's field id limit forces a choice, was left to discovery order. These tests pin the
structural rule that replaces that: a field of the project's own that holds a live reference to a
component or an object in the scene makes that target's text and authored values worth offering. They
also pin the instance count an offered field carries when several components share its declaration.
Every name here is synthetic, and the rule is shown to read none.
"""

import inspect
import json

import pytest

from playtest.preflight import manifest, packet as pack, stages
from playtest.roles import preflight
from test_name_blind import BANNED, words
from test_preflight_offer_budget import cap, evidence, limits_for, make, plan, tokens_for

KIND_TYPES = {"string": "System.String", "number": "System.Single", "integer": "System.Int32",
              "boolean": "System.Boolean", "enum": "Some.Enum", "vector3": "UnityEngine.Vector3"}


def field(ident, assembly="Framework", *, component="c", owner="o", kind="number", serialized=True,
          state="present", field_type=None, value=None, declaration=None):
    """One discovered field row, with only the keys the offer rule can read."""
    row = {"id": ident, "declaring_assembly": assembly, "component_id": component, "object_id": owner,
           "scene_id": "scene", "value_state": state, "value_kind": kind, "serialized": serialized,
           "value": value, "field_type": field_type or KIND_TYPES.get(kind, "Some.Type")}
    if declaration is not None:
        row["declaration_id"] = declaration
    return row


def holder(ident, kind, **target):
    """A field declared by the project's own assembly that holds a reference to `target`."""
    return field(ident, "Game", kind="object_reference", value={"kind": kind, **target})


def rows_of(*rows):
    return {row["id"]: row for row in rows}


def crowded(rows, size):
    """`rows` plus enough unreferenced numbers that an enum of `size` must leave something out."""
    return rows_of(*rows, *(field(f"zz_filler_{index:04d}", component=f"filler{index}")
                            for index in range(size + 1)))


def readable_by_tier(rows, size=8, **kwargs):
    result = plan(rows=rows, readable=tokens_for(rows), limits=limits_for(size), **kwargs)
    return result, {tier: info["readable"] for tier, info in result.summary["tiers"].items()}


def scene(fields):
    """A packet fixture whose discovery holds exactly `fields`, with the project's assembly named Game."""
    snapshot, record = evidence(field_count=1)
    snapshot["data"]["fields"] = list(fields)
    snapshot["caps"]["fields"] = cap(len(fields))
    record["freshness"] = {}
    return snapshot, record


def test_text_on_a_component_the_projects_own_field_references_is_offered_before_the_rest():
    rows = rows_of(field("a_elsewhere", kind="string", component="elsewhere"),
                   field("b_shown", kind="string", component="shown"),
                   holder("c_holder", "component", component_id="shown", object_id="owner"))
    result = plan(rows=rows, readable=tokens_for(rows), limits=limits_for(2))
    assert list(result.bindings.values()) == ["c_holder", "b_shown"]
    assert [(ident, tier) for ident, tier, _ in result.dropped] == [("a_elsewhere", pack.TIER_REST)]
    tiers = result.summary["tiers"]
    assert tiers[pack.TIER_REFERENCED_TEXT]["readable"] == tiers[pack.TIER_REFERENCED_TEXT]["offered"] == 1
    assert "references in the scene" in tiers[pack.TIER_REFERENCED_TEXT]["basis"]
    assert result.summary["script_references"]["components"] == 1


def test_the_rule_reads_ids_and_kinds_never_a_name():
    # The unreferenced fields are named the way a word list would reward; the referenced one is opaque.
    rows = rows_of(field("score_text", kind="string", component="c1"),
                   field("coin_counter", kind="string", component="c2"),
                   field("q7", kind="string", component="c3"),
                   holder("h", "component", component_id="c3", object_id="o3"))
    result = plan(rows=rows, readable=tokens_for(rows), limits=limits_for(2))
    assert list(result.bindings.values()) == ["h", "q7"]
    assert {ident for ident, _, _ in result.dropped} == {"score_text", "coin_counter"}
    source = "\n".join(inspect.getsource(function) for function in (
        pack.script_references, pack.script_referenced, pack.text_field, pack.authored_value_field,
        pack.declaration_instances))
    assert not [line for line in source.splitlines() if words(line) & BANNED]
    assert pack.text_field({"name": "anything", "field_type": "System.String"}) is True
    assert pack.authored_value_field({"name": "score", "serialized": False, "value_kind": "number"}) is False


def test_the_tiers_are_taken_project_then_text_then_measured_then_values_then_the_rest():
    rows = rows_of(field("a_rest"),
                   field("b_value", component="shown"),
                   field("c_measured", component="elsewhere"),
                   field("d_text", kind="string", component="shown"),
                   holder("e_holder", "component", component_id="shown", object_id="owner"))
    result = plan(rows=rows, readable=tokens_for(rows), limits=limits_for(4),
                  evidence_rows={"c_measured": {"classification": "input"}})
    # The offer is filled in tier order, not discovery order.
    assert list(result.bindings.values()) == ["e_holder", "d_text", "c_measured", "b_value"]
    assert [(ident, tier) for ident, tier, _ in result.dropped] == [("a_rest", pack.TIER_REST)]
    assert pack.OFFER_TIERS == (pack.TIER_PROJECT, pack.TIER_REFERENCED_TEXT, pack.TIER_MEASURED,
                                pack.TIER_REFERENCED_VALUE, pack.TIER_REST)
    assert list(pack.OFFER_TIERS) == sorted(pack.OFFER_TIERS)  # the names sort in the order they are filled
    assert pack.TIER_PROJECT == "tier_1_project_assembly" and pack.TIER_REST == "tier_3_other"


def test_measured_change_outranks_an_authored_value_but_not_the_text_of_a_referenced_object():
    rows = rows_of(field("a_value", component="shown"),
                   field("b_text", kind="string", component="shown"),
                   holder("c_holder", "component", component_id="shown", object_id="owner"))
    result, counts = readable_by_tier(
        crowded(list(rows.values()), 8),
        evidence_rows={"a_value": {"classification": "both"}, "b_text": {"classification": "both"}})
    assert counts[pack.TIER_REFERENCED_TEXT] == 1 and counts[pack.TIER_MEASURED] == 1
    assert counts[pack.TIER_REFERENCED_VALUE] == 0
    assert [tier for ident, tier, _ in result.dropped if ident in rows] == []


@pytest.mark.parametrize(("label", "rows", "kwargs"), [
    ("a reference held by a framework field",
     [field("holds", kind="object_reference", value={"kind": "component", "component_id": "shown"}),
      field("text", kind="string", component="shown")], {}),
    ("an asset target", [holder("holds", "asset_object", name="x"), field("text", kind="string", component="shown")], {}),
    ("an unresolved target",
     [holder("holds", "unresolved", name="x"), field("text", kind="string", component="shown")], {}),
    ("a destroyed target",
     [field("holds", "Game", kind="object_reference", state="destroyed"),
      field("text", kind="string", component="shown")], {}),
    ("a component target with no id", [holder("holds", "component"), field("text", kind="string", component="shown")], {}),
    ("a different component", [holder("holds", "component", component_id="other"),
                               field("text", kind="string", component="shown")], {}),
    ("no evidenced project assembly",
     [holder("holds", "component", component_id="shown"), field("text", kind="string", component="shown")],
     {"il": False}),
])
def test_only_a_live_scene_reference_held_by_the_projects_own_field_counts(label, rows, kwargs):
    _, counts = readable_by_tier(crowded(rows, 8), **kwargs)
    assert counts[pack.TIER_REFERENCED_TEXT] == counts[pack.TIER_REFERENCED_VALUE] == 0, label


def test_a_reference_to_an_object_covers_every_component_on_that_object():
    rows = [holder("holds", "scene_object", object_id="panel"),
            field("a_first", kind="string", component="c1", owner="panel"),
            field("b_second", kind="string", component="c2", owner="panel"),
            field("c_beside", kind="string", component="c3", owner="beside")]
    result, counts = readable_by_tier(crowded(rows, 8))
    assert counts[pack.TIER_REFERENCED_TEXT] == 2  # the string beside the object is not one of them
    assert result.summary["script_references"]["objects"] == 1
    assert list(result.bindings.values())[:3] == ["holds", "a_first", "b_second"]


def test_a_component_reference_covers_that_component_not_the_rest_of_its_object():
    rows = [holder("holds", "component", component_id="c1", object_id="panel"),
            field("a_named", kind="string", component="c1", owner="panel"),
            field("b_sibling", kind="string", component="c2", owner="panel")]
    result, counts = readable_by_tier(crowded(rows, 8))
    assert counts[pack.TIER_REFERENCED_TEXT] == 1  # the sibling on the same object is not referenced
    assert list(result.bindings.values())[:2] == ["holds", "a_named"]


def test_text_is_a_string_even_when_it_was_null_and_values_are_authored_numbers_and_flags_only():
    rows = [holder("holds", "component", component_id="shown", object_id="owner"),
            field("t1", kind="string", component="shown"),
            field("t2", kind="null", state="null", field_type="System.String", component="shown"),
            field("v1", kind="number", component="shown"),
            field("v2", kind="integer", component="shown"),
            field("v3", kind="boolean", component="shown"),
            field("x1", kind="number", serialized=False, component="shown"),   # a cache, not authored
            field("x2", kind="enum", component="shown"),
            field("x3", kind="vector3", component="shown"),
            field("x4", kind="null", state="null", field_type="Some.Type", component="shown")]
    _, counts = readable_by_tier(crowded(rows, 16), size=16)
    assert counts[pack.TIER_REFERENCED_TEXT] == 2
    assert counts[pack.TIER_REFERENCED_VALUE] == 3


def test_authored_values_take_at_most_a_share_of_the_enum_and_the_rest_fall_back_to_the_last_tier():
    size = 16
    rows = [holder("holds", "component", component_id="shown", object_id="owner"),
            *(field(f"v{index:02d}", component="shown") for index in range(9))]
    result, counts = readable_by_tier(crowded(rows, size), size=size)
    share = size * pack.REFERENCED_VALUE_PERCENT // 100
    assert share == 4 and counts[pack.TIER_REFERENCED_VALUE] == share
    assert result.summary["script_references"] == {"components": 1, "objects": 0, "authored_value_cap": share,
                                                  "authored_values_over_cap": 9 - share}
    # The first ones in discovery order keep the tier; the others are the rest, offered or not as any are.
    offered = list(result.bindings.values())
    assert offered[1:1 + share] == [f"v{index:02d}" for index in range(share)]
    assert {tier for ident, tier, _ in result.dropped if ident.startswith("v")} <= {pack.TIER_REST}


def test_a_tier_with_nothing_in_it_is_reported_empty():
    rows = rows_of(field("a"), field("b"), field("c"))
    result = plan(rows=rows, readable=tokens_for(rows), limits=limits_for(2))
    for tier in (pack.TIER_REFERENCED_TEXT, pack.TIER_REFERENCED_VALUE):
        assert result.summary["tiers"][tier]["readable"] == 0 and result.summary["tiers"][tier]["offered"] == 0
    assert result.summary["script_references"]["components"] == result.summary["script_references"]["objects"] == 0


def test_a_built_packet_offers_the_text_of_a_referenced_component_that_the_old_order_dropped():
    fillers = [field(f"b_{index:04d}", component=f"filler{index}", declaration=f"Framework:Filler:n{index}")
               for index in range(1100)]
    shown = field("z_last_in_order", kind="string", component="shown")  # sorts after every filler
    snapshot, record = scene([holder("a_holder", "component", component_id="shown", object_id="owner"),
                              *fillers, shown])
    packet = make(snapshot, record)
    data = json.loads(packet.text)
    budget = data["offer_budget"]
    assert "z_last_in_order" in packet.field_bindings.values()
    assert budget["tiers"][pack.TIER_REFERENCED_TEXT] | {"basis": ""} == {
        "basis": "", "readable": 1, "offered": 1, "unoffered": 0}
    assert len(packet.field_bindings) == budget["offered_fields"] <= budget["field_token_cap"]
    assert pack.estimated_prompt_tokens(len(packet.text)) <= pack.CONTEXT_LIMIT_TOKENS
    sidecar = json.loads(packet.sidecar)
    assert "z_last_in_order" not in {row["source_field_id"] for row in sidecar["fields"]}
    assert len(sidecar["fields"]) == budget["unoffered_fields"] > 0
    preflight.schema(packet.discovery)  # the stage-four schema still renders inside the provider limits


def test_many_referenced_components_cannot_take_more_than_their_share_of_the_offer():
    fields = []
    for index in range(300):
        fields.append(holder(f"a_holder_{index:03d}", "component", component_id=f"shown{index}", object_id="o"))
        fields.append(field(f"b_text_{index:03d}", kind="string", component=f"shown{index}"))
        fields += [field(f"c_value_{index:03d}_{n}", component=f"shown{index}") for n in range(5)]
    fields += [field(f"d_rest_{index:04d}", component=f"rest{index}") for index in range(200)]
    packet = make(*scene(fields))
    data = json.loads(packet.text)
    budget = data["offer_budget"]
    tiers = budget["tiers"]
    assert budget["offered_fields"] == len(packet.field_bindings) <= budget["field_token_cap"]
    assert tiers[pack.TIER_PROJECT]["offered"] == tiers[pack.TIER_REFERENCED_TEXT]["offered"] == 300
    assert tiers[pack.TIER_REFERENCED_VALUE]["readable"] == budget["field_token_cap"] * pack.REFERENCED_VALUE_PERCENT // 100
    assert budget["script_references"]["authored_values_over_cap"] == 1500 - tiers[pack.TIER_REFERENCED_VALUE]["readable"]
    assert tiers[pack.TIER_REST]["offered"] > 0  # the share leaves room for fields nothing ranked
    assert pack.estimated_prompt_tokens(len(packet.text)) <= pack.CONTEXT_LIMIT_TOKENS
    preflight.schema(packet.discovery)


def test_a_packet_whose_fields_fit_is_unchanged_by_the_referenced_tiers():
    rows = [holder("a_holder", "component", component_id="shown", object_id="owner"),
            field("b_text", kind="string", component="shown"), field("c_other", component="elsewhere")]
    data = json.loads(make(*scene(rows)).text)
    assert "offer_budget" not in data and "instances_note" not in data
    assert [row["id"] for row in data["offered_fields"]] == ["field_00000", "field_00001", "field_00002"]


def test_declaration_instances_counts_the_components_that_carry_a_declaration():
    rows = rows_of(field("a", component="c1", declaration="X:Pooled:n"), field("b", component="c2", declaration="X:Pooled:n"),
                   field("c", component="c3", declaration="X:Pooled:n"), field("d", component="c1", declaration="X:Single:n"),
                   field("e", component="c1"),                               # no declaration id: not counted
                   {"id": "f", "declaration_id": "X:Loose:n"}, {"id": "g", "declaration_id": "X:Loose:n"})
    assert pack.declaration_instances(rows) == {"X:Pooled:n": 3, "X:Single:n": 1, "X:Loose:n": 2}
    # The same component carrying the declaration twice is still one instance.
    twice = rows_of(field("a", component="c1", declaration="X:Y:n"), field("b", component="c1", declaration="X:Y:n"))
    assert pack.declaration_instances(twice) == {"X:Y:n": 1}


def pooled_scene():
    fields = [field(f"a_pooled_{index}", "Game", component=f"pool{index}", declaration="Game:Pool:count")
              for index in range(3)]
    fields += [field("b_single", "Game", component="solo", declaration="Game:Solo:count"),
               field("c_undeclared", "Game", component="other"),
               field("d_hidden", "Game", component="pool0", declaration="Game:Pool:hidden", state="unsupported",
                     kind="unsupported")]
    return scene(fields)


def test_an_offered_field_carried_by_several_components_says_how_many_and_a_single_one_does_not():
    snapshot, record = pooled_scene()
    packet = make(snapshot, record)
    data = json.loads(packet.text)
    entries = {row["source_field_id"]: row for row in data["offered_fields"]}
    assert [entries[f"a_pooled_{index}"][pack.INSTANCE_KEY] for index in range(3)] == [3, 3, 3]
    assert pack.INSTANCE_KEY not in entries["b_single"] and pack.INSTANCE_KEY not in entries["c_undeclared"]
    assert data["instances_note"] == pack.INSTANCES_NOTE
    assert set(entries["a_pooled_0"]) == {"id", "source_field_id", "field_type", "same_scene_as_entity", "instances"}
    # The discovery row stays the Editor's own descriptor: the count is not written into it.
    rows = {row["id"]: row for row in data["discovery"]["fields"]}
    assert all(pack.INSTANCE_KEY not in row for row in rows.values())
    raw = {row["id"]: {key: value for key, value in row.items() if key not in ("value", "enum_name")}
           for row in snapshot["data"]["fields"]}
    assert rows == raw
    assert make(snapshot, record).text == packet.text  # deterministic


def test_the_count_is_taken_over_every_discovered_row_not_only_the_offered_ones():
    snapshot, record = pooled_scene()
    for row in snapshot["data"]["fields"][1:3]:
        row.update(value_state="unsupported", value_kind="unsupported")  # two of the three are unreadable
    data = json.loads(make(snapshot, record).text)
    entries = {row["source_field_id"]: row for row in data["offered_fields"]}
    assert entries["a_pooled_0"][pack.INSTANCE_KEY] == 3 and "a_pooled_1" not in entries


def test_the_count_reaches_the_manifest_without_touching_the_reader_descriptor():
    snapshot, record = pooled_scene()
    packet = make(snapshot, record)
    answers = [
        {"stage_4": {"readers": [{"field_id": "field_00001", "alias": "pooled", "why": "observe"}],
                     "unobservable": ["missing state"]}, "stage_5": None, "stage_6": None},
        {"stage_4": None, "stage_5": {"actions": [{"name": "move", "intent": "move", "hold_s": 0.2,
         "inputs": [{"control_id": "w", "x": None, "y": None}], "preconditions": []}], "skills": [],
         "uncontrollable": ["missing input"]}, "stage_6": None},
        {"stage_4": None, "stage_5": None, "stage_6": {"progress": [], "invariants": [],
         "unjudgeable": ["missing objective"]}},
    ]
    replies = iter(answers)
    result = stages.run(packet, lambda *_: stages.CallResult(json.dumps(next(replies)), None, {"usage": None}),
                        lambda _: None)
    built = manifest.assemble(packet, result)
    reader = built.readers[0]
    offered = {row["id"]: row for row in built.selection.offered_fields}
    assert offered[reader.selection_id][pack.INSTANCE_KEY] == 3
    # A saved manifest's reader descriptor is the Editor's row verbatim, which a later inspection is
    # compared against (see preflight.cache.resolve_readers); the count is not part of it.
    raw = next(row for row in snapshot["data"]["fields"] if row["id"] == reader.field_id)
    assert reader.descriptor == {key: value for key, value in raw.items() if key not in ("value", "enum_name")}
    assert manifest.validate(built) == [] and manifest.validate_evidence(built, packet) == []
