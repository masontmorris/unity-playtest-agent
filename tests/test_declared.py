from playtest.runner.declared import declared_configuration


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
