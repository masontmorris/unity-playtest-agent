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
    return _selected(data, included, objects)


def object_configuration(discovery: Mapping, object_ids, *, skip_types: tuple[str, ...] = (),
                         limit: int = 96) -> dict | None:
    """The authored values on other scene objects (each object itself, not its children), in the order given: what
    a situation near one of them declares, such as an NPC's interaction range. `skip_types` leaves out the fields
    declared by types with those prefixes (an engine's)."""
    data = discovery.get("data", {})
    objects = {o["id"]: o for o in data.get("game_objects", [])}
    order = {oid: n for n, oid in enumerate(object_ids)}
    rows = sorted((row for row in data.get("fields", []) if row.get("object_id") in order
                   and not (row.get("declaring_type") or "").startswith(skip_types)),
                  key=lambda row: order[row["object_id"]])
    result = _selected({**data, "fields": rows}, set(order), objects, limit=len(rows) or 1, ids=True)
    if result is None:
        return None
    # The same script with the same value on many objects (every coin's value) is said once, with how many carry it,
    # so a value only one object declares is not crowded out.
    grouped: dict[tuple, dict] = {}
    for value in result["values"]:
        key = (value["source"], repr(value["value"]), value["value_state"])
        if key in grouped:
            grouped[key]["also_on"] = grouped[key].get("also_on", 0) + 1
        else:
            grouped[key] = dict(value)
    values = list(grouped.values())
    out = {"read": result["read"], "values": values[:limit]}
    if len(values) > limit:
        out["omitted"] = len(values) - limit
    return out


def _selected(data: Mapping, included: set, objects: Mapping, *, limit: int = 96, ids: bool = False) -> dict | None:
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
                       "object": obj.get("path") or obj.get("name"), "value": value, "value_state": state,
                       **({"object_id": row["object_id"]} if ids else {})})
    if not values:
        return None
    result = {"read": "authored values as preflight discovery read them, in play before any input",
              "values": values[:limit]}
    if len(values) > limit:
        result["omitted"] = len(values) - limit
    return result
