from playtest.runner.declared import declared_configuration, object_configuration


def test_selection_and_cap():
    base = {"object_id": "child", "serialized": True, "hide_in_inspector": False,
            "declaring_type": "Ns.Type", "name": "size", "value_kind": "number",
            "value_state": "present", "value": 2}
    rows = [base, {**base, "serialized": False}, {**base, "hide_in_inspector": True},
            {**base, "value_kind": "object_reference"},
            {**base, "value_state": "unknown", "value": 17}]
    data = {"data": {"game_objects": [{"id": "root"}, {"id": "child", "parent_id": "root", "path": "root/child"}], "fields": rows}}
    result = declared_configuration(data, "root")
    assert [v["value"] for v in result["values"]] == [2, None]
    assert result["values"][1]["value_state"] == "unknown"
    data["data"]["fields"] = [base] * 100
    result = declared_configuration(data, "root")
    assert len(result["values"]) == 96 and result["omitted"] == 4


def test_other_objects_values_are_their_own_in_the_order_given_without_engine_fields():
    row = {"serialized": True, "hide_in_inspector": False, "value_kind": "number", "value_state": "present"}
    data = {"data": {"game_objects": [{"id": "npc", "path": "Wraith"}, {"id": "npc.0", "parent_id": "npc"},
                                      {"id": "coin", "path": "Coin"}],
                     "fields": [{**row, "object_id": "coin", "declaring_type": "Game.Collectible", "name": "value",
                                 "value": 1},
                                {**row, "object_id": "npc", "declaring_type": "Game.TalkableNPC",
                                 "name": "interactDistance", "value": 5.0},
                                {**row, "object_id": "npc", "declaring_type": "UnityEngine.Light", "name": "range",
                                 "value": 9.0},
                                {**row, "object_id": "npc.0", "declaring_type": "Game.Sprite", "name": "size",
                                 "value": 2.0}]}}
    result = object_configuration(data, ["npc", "coin"], skip_types=("UnityEngine",))
    assert [(v["object_id"], v["source"], v["value"]) for v in result["values"]] == [
        ("npc", "TalkableNPC.interactDistance", 5.0), ("coin", "Collectible.value", 1)]
    assert object_configuration(data, []) is None


def test_a_value_many_objects_share_is_said_once_with_how_many_more_carry_it():
    row = {"serialized": True, "hide_in_inspector": False, "value_kind": "number", "value_state": "present",
           "declaring_type": "Game.Collectible", "name": "coinValue", "value": 1}
    data = {"data": {"game_objects": [{"id": f"c{n}", "path": f"Coin {n}"} for n in range(3)],
                     "fields": [{**row, "object_id": f"c{n}"} for n in range(3)]}}
    (only,) = object_configuration(data, ["c0", "c1", "c2"])["values"]
    assert (only["object_id"], only["also_on"]) == ("c0", 2)
