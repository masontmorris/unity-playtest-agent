"""Select authored values discovered before input."""
from collections.abc import Mapping
import math


def declared_configuration(discovery: Mapping, entity_object_id: str) -> dict | None:
    data = discovery.get("data", {})
    objects = {o["id"]: o for o in data.get("game_objects", [])}
    included = {entity_object_id}
    while True:
        children = {oid for oid, obj in objects.items() if obj.get("parent_id") in included}
        if children <= included:
            break
        included |= children
    values = []
    kinds = {"number", "integer", "boolean", "string", "enum", "vector2", "vector3", "vector4"}
    for row in data.get("fields", []):
        if row.get("object_id") not in included or row.get("serialized") is not True or row.get("hide_in_inspector") is not False:
            continue
        kind, state, value = row.get("value_kind"), row.get("value_state"), row.get("value")
        if kind not in kinds:
            if state == "present" or row.get("field_type_kind") not in {"primitive", "enum"}:
                continue
        if state != "present":
            value = None
        elif kind in {"number", "integer"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
        elif kind == "boolean" and not isinstance(value, bool):
            continue
        elif kind == "string" and (not isinstance(value, str) or len(value) > 256):
            continue
        elif kind.startswith("vector") and (not isinstance(value, list) or len(value) != int(kind[-1])
                or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value)):
            continue
        elif kind == "enum" and (not isinstance(value, (str, int)) or isinstance(value, bool)
                                 or isinstance(value, str) and len(value) > 256):
            continue
        obj = objects.get(row["object_id"], {})
        values.append({"source": row.get("declaring_type", "").split(".")[-1] + "." + row.get("name", ""),
                       "object": obj.get("path") or obj.get("name"), "value": value, "value_state": state})
    if not values:
        return None
    result = {"read": "authored values as preflight discovery read them, in play before any input", "values": values[:96]}
    if len(values) > 96:
        result["omitted"] = len(values) - 96
    return result
