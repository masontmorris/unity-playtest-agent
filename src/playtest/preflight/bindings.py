"""Scene-reached and project-asset-reached input bindings as one binding set.

Discovery reports input bindings in two sections with two shapes: `bindings` for a binding
reached through a scene component field, and `input_asset_bindings` for a binding read from
an Input System asset in the project, including an asset that only code loads. A binding is
the same binding however its asset was reached, so the consumers of both sections read this
one normalized set instead of walking the sections themselves.

Normalization keeps the values a row carried: nothing is rewritten, aliased across from a
similarly named field, or filled in from the other section. A field the row did not measure
stays absent or empty and its name is listed in the row's `unknown`, so a reader can tell a
measured `False` from no measurement at all. An older record that predates the shared fields
therefore reads as unknown: it stays in the set, is never pressed on a guess, and is never
reported as evidence it did not carry.

A binding is de-duplicated by what the rows say about the binding itself: its binding id
together with the asset path it was authored in, or with its action id when no asset path was
measured. A row that named no binding id has no identity and is never merged. When the same
binding is reached both ways the scene-field row is the preferred one: its measured values are
kept and the other reach is recorded in `provenance_all` and `merged_ids`, so a binding is
never presented as scene-referenced when only the project's asset reached it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from typing import Any


SCENE_FIELD = "scene_field"
PROJECT_ASSET = "project_asset"
# The section each reach is reported in, in preference order: the scene field first.
SECTIONS: tuple[tuple[str, str], ...] = (("bindings", SCENE_FIELD),
                                        ("input_asset_bindings", PROJECT_ASSET))
SECTION_OF = {provenance: section for section, provenance in SECTIONS}
# The fields that decide which physical control a binding drives and how it is composed.
MEASURED_FIELDS = ("binding_id", "action_id", "action_type", "expected_control_type",
                   "binding_effective_path", "binding_is_composite", "binding_is_part_of_composite")
FLAG_FIELDS = frozenset({"binding_is_composite", "binding_is_part_of_composite"})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def measured_text(row: Mapping[str, Any], name: str) -> str | None:
    """The text a row measured for `name`, or None when it measured nothing there."""
    value = row.get(name)
    return value if isinstance(value, str) and value.strip() else None


def measured_flag(row: Mapping[str, Any], name: str) -> bool | None:
    """The flag a row measured for `name`, or None: a flag is never inferred from a value."""
    value = row.get(name)
    return value if isinstance(value, bool) else None


def _measured(row: Mapping[str, Any], name: str) -> Any:
    return measured_flag(row, name) if name in FLAG_FIELDS else measured_text(row, name)


def unknown_fields(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The names of the binding fields this row carries no measured value for."""
    return tuple(name for name in MEASURED_FIELDS if _measured(row, name) is None)


def identity(row: Mapping[str, Any]) -> str | None:
    """The binding a row is, from what it carried, or None when it named no binding id.

    The asset path and the action id belong to it when they were measured, so two reaches are
    merged only when everything they said about the binding agrees.
    """
    binding_id = measured_text(row, "binding_id")
    if binding_id is None:
        return None
    asset = measured_text(row, "asset_path")
    action = measured_text(row, "action_id")
    return _canonical({"binding_id": binding_id.lower(),
                       "asset_path": asset.replace("\\", "/") if asset is not None else None,
                       "action_id": action.lower() if action is not None else None})


def normalize(row: Mapping[str, Any], provenance: str) -> dict[str, Any]:
    """One discovery row with its reach made explicit; every measured value is left as it was."""
    if provenance not in SECTION_OF:
        raise ValueError(f"binding provenance {provenance!r} is not a discovery section")
    if not isinstance(row, Mapping):
        raise ValueError("a discovery binding row is not an object")
    record = dict(row)
    record["origin_section"] = SECTION_OF[provenance]
    record["provenance"] = provenance
    record["provenance_all"] = [provenance]
    record["merged_ids"] = []
    record["identity"] = identity(record)
    record["unknown"] = list(unknown_fields(record))
    return record


def _rows(discovery: Mapping[str, Any]) -> Iterator[tuple[str, Mapping[str, Any]]]:
    for section, provenance in SECTIONS:
        rows = discovery.get(section)
        if rows is None:
            continue
        if not isinstance(rows, list):
            raise ValueError(f"discovery's {section} is not a list of rows")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"discovery's {section} holds a row that is not an object")
            yield provenance, row


def binding_set(discovery: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every discovered binding once, however its asset was reached.

    A project-asset row that is the same binding as a scene-field row is folded into that row,
    the preferred one, and the folded reach stays named in `provenance_all`. Two scene fields
    that reach one binding are two facts about it, so they are never folded into each other.
    """
    records = [normalize(row, provenance) for provenance, row in _rows(discovery)]
    scene_reaches: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record["provenance"] == SCENE_FIELD and record["identity"] is not None:
            scene_reaches.setdefault(record["identity"], []).append(record)
    result: list[dict[str, Any]] = []
    for record in records:
        key = record["identity"] if record["provenance"] == PROJECT_ASSET else None
        owner = next((scene for scene in scene_reaches.get(key, [])
                      if PROJECT_ASSET not in scene["provenance_all"]), None) if key else None
        if owner is None:
            result.append(record)
            continue
        owner["provenance_all"].append(PROJECT_ASSET)
        if isinstance(record.get("id"), str) and record["id"]:
            owner["merged_ids"].append(record["id"])
    return result


def by_id(records: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index a binding set by every id it was reached under, the preferred row included."""
    index: dict[str, dict[str, Any]] = {}
    for record in records:
        ids = [record.get("id"), *(record.get("merged_ids") or [])]
        for ident in ids:
            if isinstance(ident, str) and ident:
                index[ident] = record  # type: ignore[assignment]
    return index
