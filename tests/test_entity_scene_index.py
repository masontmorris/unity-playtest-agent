from __future__ import annotations

from playtest.contracts.scene_ids import unique_by_scene_free_id as _same_object_other_scene_index

ENTITY = "scene:Assets/Level.unity@1::5"


def row(row_id: str) -> dict:
    return {"id": row_id}


def test_the_same_object_under_another_loaded_scene_index_is_found_when_unique():
    rows = {"scene:Assets/Level.unity@0::5": row("scene:Assets/Level.unity@0::5"),
            "scene:Assets/Level.unity@0::6": row("scene:Assets/Level.unity@0::6")}
    assert _same_object_other_scene_index(rows, ENTITY)["id"] == "scene:Assets/Level.unity@0::5"


def test_several_or_no_candidates_leave_the_entity_missing():
    two = {"scene:Assets/Level.unity@0::5": row("a"), "scene:Assets/Level.unity@2::5": row("b")}
    assert _same_object_other_scene_index(two, ENTITY) is None
    other_path = {"scene:Assets/Other.unity@0::5": row("c")}
    assert _same_object_other_scene_index(other_path, ENTITY) is None
    other_object = {"scene:Assets/Level.unity@0::5.1": row("d")}
    assert _same_object_other_scene_index(other_object, ENTITY) is None
    assert _same_object_other_scene_index({}, "not-a-scene-id") is None


def test_field_ids_lose_only_the_loaded_scene_index():
    from playtest.contracts.scene_ids import scene_free_index, without_scene_index

    field = "scene:Assets/Level.unity@1::5::component:3::field:Game::Mover::speed"
    assert without_scene_index(field) == "scene:Assets/Level.unity::5::component:3::field:Game::Mover::speed"
    assert without_scene_index("scene:Assets/Level.unity@0") == "scene:Assets/Level.unity"
    assert without_scene_index("asset:Assets/a@2.asset") == "asset:Assets/a@2.asset"
    assert without_scene_index(None) is None
    live = [{"id": field.replace("@1", "@0")}, {"id": "scene:Assets/Level.unity@0::6"}]
    assert scene_free_index(live)[without_scene_index(field)]["id"] == field.replace("@1", "@0")
    twice = scene_free_index([{"id": field.replace("@1", "@0")}, {"id": field.replace("@1", "@2")}])
    assert twice[without_scene_index(field)] is None
