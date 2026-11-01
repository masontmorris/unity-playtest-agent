from playtest.preflight.scene_selection import select_scene


def measured(path):
    return {"scene_path": path, "measurement": {"status": "identified", "entity_id": "body",
            "entity": {"scene_path": path}}}


def test_first_scene_with_movement_remains_first():
    result = select_scene([measured("Assets/a.unity"), measured("Assets/b.unity")])
    assert result["scene_path"] == "Assets/a.unity"
    assert len(result["candidates"]) == 2


def test_selects_measured_later_scene_and_retains_unknown():
    result = select_scene([{"scene_path": "Assets/a.unity", "measurement": {"status": "inconclusive", "error": "no response"}}, measured("Assets/b.unity")])
    assert result["scene_path"] == "Assets/b.unity"
    assert result["candidates"][0]["reason"] == "no response"


def test_no_evidence_or_wrong_scene_is_unknown():
    row = measured("Assets/a.unity")
    row["measurement"]["entity"]["scene_path"] = "Assets/b.unity"
    assert select_scene([row])["status"] == "unknown"
    assert select_scene([])["scene_path"] is None
