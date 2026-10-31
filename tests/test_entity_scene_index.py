from __future__ import annotations

from playtest.preflight.calibrate import _same_object_other_scene_index

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
